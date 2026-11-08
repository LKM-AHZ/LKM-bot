"""Storage and retrieval for long-term memory.

The store is a small SQLite engine on top of the core database helper: rows keep
their vector as a packed float blob and scoring happens in Python, which keeps
the memory layer dependency-free and fast enough for the few thousand memories a
chat produces. Scalar search modes (``time``) and aggregate statistics are
pushed down to SQL.

When ``a_memorix`` is enabled the adapter asks the upstream engine instead, so
this module remains the local fallback rather than the only implementation.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from typing import Any, Iterable, Sequence

import numpy as np
from sqlalchemy import delete, desc, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import col

from bot import logger
from bot.core.db import BaseDatabase
from bot.life.common import CursorStoreMixin, ensure_tables
from bot.life.memory.models import LifeMemory, LifeMemoryCursor

_ASCII_TOKEN = re.compile(r"[a-z0-9_]{2,}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

# Words that carry no retrieval signal in Chinese or English.
_STOP_TOKENS = frozenset(
    {
        "the",
        "and",
        "for",
        "you",
        "are",
        "was",
        "with",
        "this",
        "that",
        "什么",
        "怎么",
        "这个",
        "那个",
        "我们",
        "你们",
        "他们",
        "就是",
        "一个",
        "可以",
        "现在",
    }
)


def tokenize(text: str) -> list[str]:
    """Split text into ASCII words and CJK bigrams.

    Args:
        text: Raw text to tokenize.

    Returns:
        Unique lowercase tokens in first-seen order.
    """
    tokens: list[str] = []

    def _add(token: str) -> None:
        if len(token) >= 2 and token not in _STOP_TOKENS and token not in tokens:
            tokens.append(token)

    lowered = text.lower()
    for match in _ASCII_TOKEN.findall(lowered):
        _add(match)
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            _add(run)
            continue
        for index in range(len(run) - 1):
            _add(run[index : index + 2])
    return tokens


def content_hash(kind: str, session_key: str, content: str) -> str:
    """Return the dedupe hash of one memory row.

    Args:
        kind: Memory kind.
        session_key: Owning session.
        content: Memory body.

    Returns:
        Hex digest used by the unique constraint.
    """
    digest = hashlib.sha256(f"{kind}\x00{session_key}\x00{content}".encode())
    return digest.hexdigest()


def pack_vector(vector: Sequence[float]) -> bytes:
    """Serialize a float vector for storage.

    Args:
        vector: Plain float sequence.

    Returns:
        Little-endian float32 bytes.
    """
    return np.asarray(vector, dtype=np.float32).tobytes()


def unpack_vector(blob: bytes) -> np.ndarray:
    """Deserialize a stored vector.

    Args:
        blob: Bytes produced by :func:`pack_vector`.

    Returns:
        Float32 array; empty when the blob is empty.
    """
    if not blob:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(blob, dtype=np.float32)


class MemoryStore(CursorStoreMixin):
    """Async SQLite store for long-term memory rows."""

    cursor_model = LifeMemoryCursor

    def __init__(self, db_helper: BaseDatabase, retention_days: int = 0) -> None:
        """Initialize the store.

        Args:
            db_helper: Core async database helper.
            retention_days: Retention window; 0 keeps rows forever.
        """
        self.db = db_helper
        self.retention_days = retention_days

    async def ensure_schema(self) -> None:
        """Create memory tables and indexes when they do not exist."""
        await ensure_tables(self.db, LifeMemory, LifeMemoryCursor)

    async def insert_many(self, rows: list[dict[str, Any]]) -> int:
        """Insert memory rows, ignoring duplicates.

        Args:
            rows: Row payloads; missing hashes and timestamps are filled in.

        Returns:
            Number of rows actually inserted.
        """
        if not rows:
            return 0
        now = int(time.time())
        payload: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item.setdefault("scope", "session")
            item.setdefault("importance", 0.5)
            item.setdefault("meta", {})
            item.setdefault("source_ids", [])
            item.setdefault("ts", now)
            item.setdefault("created_at", now)
            item["updated_at"] = now
            item["content_hash"] = item.get("content_hash") or content_hash(
                item["kind"],
                item["session_key"],
                item["content"],
            )
            item.setdefault("keywords", " ".join(tokenize(item["content"])[:64]))
            payload.append(item)
        async with self.db.get_db() as session:
            result = await session.execute(
                sqlite_insert(LifeMemory)
                .values(payload)
                .on_conflict_do_nothing(
                    index_elements=["kind", "session_key", "content_hash"],
                )
            )
            await session.commit()
            return max(int(result.rowcount or 0), 0)  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount

    async def set_embedding(
        self,
        memory_id: int,
        vector: Sequence[float],
        model: str,
    ) -> None:
        """Attach a vector to an existing memory row.

        Args:
            memory_id: Target row id.
            vector: Embedding produced by the provider.
            model: Provider/model identifier stored for later invalidation.
        """
        packed = pack_vector(vector)
        async with self.db.get_db() as session:
            stored = await session.get(LifeMemory, memory_id)
            if stored is None:
                return
            stored.embedding = packed
            stored.embedding_model = model
            stored.embedding_dim = len(vector)
            stored.updated_at = int(time.time())
            await session.commit()

    async def get(self, memory_id: int) -> dict[str, Any] | None:
        """Return one memory row as a plain dict.

        Args:
            memory_id: Row id.

        Returns:
            Row dictionary, or None when missing.
        """
        async with self.db.get_db() as session:
            row = await session.get(LifeMemory, memory_id)
            return row.model_dump() if row else None

    async def pending_embeddings(
        self,
        limit: int = 20,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return rows that still need a vector for the given model.

        Args:
            limit: Maximum number of rows.
            model: Only rows whose stored model differs are returned.

        Returns:
            Row dictionaries ordered by creation time.
        """
        query = select(LifeMemory).where(col(LifeMemory.embedding).is_(None))
        if model is not None:
            query = select(LifeMemory).where(col(LifeMemory.embedding_model) != model)
        async with self.db.get_db() as session:
            result = await session.execute(
                query.order_by(col(LifeMemory.id)).limit(limit)
            )
            return [row.model_dump() for row in result.scalars().all()]

    async def candidates(
        self,
        session_key: str,
        kinds: Iterable[str] | None = None,
        since_ts: int | None = None,
        limit: int = 600,
    ) -> list[dict[str, Any]]:
        """Return recent candidate rows for scoring.

        Args:
            session_key: Session scope; global-scope rows are always included.
            kinds: Kind filter; None keeps every kind.
            since_ts: Only rows anchored at or after this timestamp.
            limit: Maximum number of rows fetched from SQLite.

        Returns:
            Row dictionaries, newest first.
        """
        query = select(LifeMemory).where(
            or_(
                col(LifeMemory.session_key) == session_key,
                col(LifeMemory.scope) == "global",
            )
        )
        if kinds:
            query = query.where(col(LifeMemory.kind).in_(tuple(kinds)))
        if since_ts:
            query = query.where(col(LifeMemory.ts) >= since_ts)
        async with self.db.get_db() as session:
            result = await session.execute(
                query.order_by(desc(col(LifeMemory.ts))).limit(limit)
            )
            return [row.model_dump() for row in result.scalars().all()]

    async def search(
        self,
        session_key: str,
        query: str = "",
        vector: Sequence[float] | None = None,
        mode: str = "hybrid",
        limit: int = 5,
        kinds: Iterable[str] | None = None,
        since_ts: int | None = None,
        time_start: int | None = None,
        time_end: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search memory rows with one of the five supported modes.

        Args:
            session_key: Session scope; global-scope rows are included.
            query: Natural-language query used by keyword, hybrid and search modes.
            vector: Query embedding used by vector, hybrid and search modes.
            mode: ``search`` / ``time`` / ``hybrid`` / ``episode`` / ``aggregate``.
            limit: Maximum number of hits.
            kinds: Kind filter.
            since_ts: Lower timestamp bound.
            time_start: Explicit window start for ``time`` and ``episode`` modes.
            time_end: Explicit window end.

        Returns:
            Hit dictionaries with ``score``, ordered best first.
        """
        if mode == "time":
            start = time_start if time_start is not None else (since_ts or 0)
            return await self.time_window(
                session_key,
                start_ts=start,
                end_ts=time_end,
                limit=limit,
                kinds=kinds,
            )
        if mode == "aggregate":
            return await self.aggregate(session_key, since_ts=since_ts, limit=limit)

        rows = await self.candidates(session_key, kinds, since_ts)
        if mode == "episode":
            rows = [row for row in rows if row["kind"] == "episode"]
            if mode == "episode" and not rows:
                rows = await self.candidates(session_key, ("summary",), since_ts)
        if not rows:
            return []

        tokens = tokenize(query) if query else []
        token_set = set(tokens)
        query_vector = (
            np.asarray(vector, dtype=np.float32) if vector else np.zeros(0, np.float32)
        )
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            keyword_score = 0.0
            if token_set:
                row_tokens = set(row.get("keywords", "").split())
                overlap = len(token_set & row_tokens)
                keyword_score = overlap / (len(token_set) + 1.0)
            similarity = 0.0
            if query_vector.size and row.get("embedding"):
                stored = unpack_vector(row["embedding"])
                if stored.size == query_vector.size:
                    norm = float(
                        np.linalg.norm(stored) * np.linalg.norm(query_vector) + 1e-9
                    )
                    similarity = float(np.dot(stored, query_vector)) / norm
                    similarity = max(0.0, similarity)
            if mode == "search":
                score = similarity if similarity else keyword_score
            elif mode == "hybrid":
                score = 0.7 * similarity + 0.3 * keyword_score
            else:  # episode
                score = 0.5 * keyword_score + 0.5 * (similarity or 0.5)
            score *= 0.9 + 0.2 * min(max(float(row.get("importance") or 0.5), 0.0), 1.0)
            if score <= 0:
                continue
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {**row, "score": round(score, 4)} for score, row in scored[: max(limit, 1)]
        ]

    async def time_window(
        self,
        session_key: str,
        start_ts: int = 0,
        end_ts: int | None = None,
        limit: int = 20,
        kinds: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return memories inside an explicit time window.

        Args:
            session_key: Session scope.
            start_ts: Inclusive lower bound.
            end_ts: Inclusive upper bound; None means no upper bound.
            limit: Maximum rows.
            kinds: Kind filter.

        Returns:
            Row dictionaries ordered by anchor timestamp.
        """
        query = select(LifeMemory).where(
            or_(
                col(LifeMemory.session_key) == session_key,
                col(LifeMemory.scope) == "global",
            )
        )
        if start_ts:
            query = query.where(col(LifeMemory.ts) >= start_ts)
        if end_ts:
            query = query.where(col(LifeMemory.ts) <= end_ts)
        if kinds:
            query = query.where(col(LifeMemory.kind).in_(tuple(kinds)))
        async with self.db.get_db() as session:
            result = await session.execute(
                query.order_by(col(LifeMemory.ts)).limit(max(limit, 1))
            )
            return [
                {**row.model_dump(), "score": 1.0} for row in result.scalars().all()
            ]

    async def aggregate(
        self,
        session_key: str,
        since_ts: int | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Summarize memory volume per kind and person.

        Args:
            session_key: Session scope.
            since_ts: Lower timestamp bound.
            limit: Maximum groups per dimension.

        Returns:
            Synthetic rows describing counts rather than memory content.
        """
        scope_filter = or_(
            col(LifeMemory.session_key) == session_key,
            col(LifeMemory.scope) == "global",
        )
        async with self.db.get_db() as session:
            kind_query = select(
                col(LifeMemory.kind),
                func.count(),
            ).where(scope_filter)
            person_query = select(
                col(LifeMemory.person_id),
                func.count(),
            ).where(scope_filter)
            if since_ts:
                kind_query = kind_query.where(col(LifeMemory.ts) >= since_ts)
                person_query = person_query.where(col(LifeMemory.ts) >= since_ts)
            kinds = await session.execute(
                kind_query.group_by(col(LifeMemory.kind)).order_by(desc(func.count()))
            )
            persons = await session.execute(
                person_query.where(col(LifeMemory.person_id).is_not(None))
                .group_by(col(LifeMemory.person_id))
                .order_by(desc(func.count()))
                .limit(limit)
            )
        hits: list[dict[str, Any]] = []
        for kind, count in kinds.all():
            hits.append(
                {
                    "kind": "aggregate",
                    "content": f"{kind}: {int(count)}",
                    "score": float(int(count)),
                    "meta": {"group": "kind", "value": kind, "count": int(count)},
                }
            )
        for person_id, count in persons.all():
            hits.append(
                {
                    "kind": "aggregate",
                    "person_id": person_id,
                    "content": f"{person_id}: {int(count)}",
                    "score": float(int(count)),
                    "meta": {
                        "group": "person",
                        "value": person_id,
                        "count": int(count),
                    },
                }
            )
        return hits[: max(limit, 1)]

    async def list_recent(
        self,
        limit: int = 20,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the newest memory rows.

        Args:
            limit: Maximum rows.
            kind: Optional kind filter.

        Returns:
            Row dictionaries without vectors.
        """
        query = select(LifeMemory)
        if kind:
            query = query.where(col(LifeMemory.kind) == kind)
        async with self.db.get_db() as session:
            result = await session.execute(
                query.order_by(desc(col(LifeMemory.ts))).limit(max(limit, 1))
            )
            return [_public(row) for row in result.scalars().all()]

    async def delete(
        self,
        memory_ids: Iterable[int] | None = None,
        session_key: str | None = None,
    ) -> int:
        """Delete memories by id or by session.

        Args:
            memory_ids: Explicit row ids.
            session_key: Delete every row of one session.

        Returns:
            Number of deleted rows.
        """
        if not memory_ids and not session_key:
            raise ValueError("delete requires memory_ids or session_key")
        async with self.db.get_db() as session:
            if memory_ids:
                result = await session.execute(
                    delete(LifeMemory).where(col(LifeMemory.id).in_(tuple(memory_ids)))
                )
            else:
                result = await session.execute(
                    delete(LifeMemory).where(col(LifeMemory.session_key) == session_key)
                )
            await session.commit()
            return max(int(result.rowcount or 0), 0)  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount

    async def export(self, session_key: str | None = None) -> list[dict[str, Any]]:
        """Export memories for backup or migration.

        Args:
            session_key: Restrict to one session; None exports everything.

        Returns:
            JSON-serializable rows without vectors.
        """
        query = select(LifeMemory)
        if session_key:
            query = query.where(col(LifeMemory.session_key) == session_key)
        async with self.db.get_db() as session:
            result = await session.execute(query.order_by(col(LifeMemory.ts)))
            return [_public(row) for row in result.scalars().all()]

    async def stats(self) -> dict[str, Any]:
        """Return memory counters.

        Returns:
            Mapping with totals, per-kind counts and vector coverage.
        """
        async with self.db.get_db() as session:
            total = (
                await session.execute(select(func.count()).select_from(LifeMemory))
            ).scalar_one()
            embedded = (
                await session.execute(
                    select(func.count())
                    .select_from(LifeMemory)
                    .where(col(LifeMemory.embedding).is_not(None))
                )
            ).scalar_one()
            kinds = await session.execute(
                select(col(LifeMemory.kind), func.count())
                .group_by(col(LifeMemory.kind))
                .order_by(desc(func.count()))
            )
            latest = (
                await session.execute(select(func.max(col(LifeMemory.ts))))
            ).scalar_one()
        return {
            "total": int(total or 0),
            "embedded": int(embedded or 0),
            "by_kind": {kind: int(count) for kind, count in kinds.all()},
            "latest_ts": int(latest) if latest else None,
        }

    async def cleanup(self) -> int:
        """Delete memories older than the retention window.

        Returns:
            Number of deleted rows; 0 when retention is disabled.
        """
        if self.retention_days <= 0:
            return 0
        cutoff = int(time.time()) - self.retention_days * 86400
        async with self.db.get_db() as session:
            result = await session.execute(
                delete(LifeMemory).where(col(LifeMemory.ts) < cutoff)
            )
            await session.commit()
            deleted = max(int(result.rowcount or 0), 0)  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount
        if deleted:
            logger.info(f"[life] memory retention removed {deleted} rows")
        return deleted

def _public(row: LifeMemory) -> dict[str, Any]:
    """Strip the vector from a stored row.

    Args:
        row: Stored memory row.

    Returns:
        JSON-serializable dictionary.
    """
    data = row.model_dump()
    data.pop("embedding", None)
    data["embedded"] = bool(row.embedding)
    return data


def recency_weight(ts: int, half_life_days: float = 14.0) -> float:
    """Return a decay factor for a memory timestamp.

    Args:
        ts: Memory anchor timestamp.
        half_life_days: Days after which the factor halves.

    Returns:
        Factor in ``(0, 1]``.
    """
    age_days = max(0.0, (time.time() - ts) / 86400.0)
    if half_life_days <= 0:
        return 1.0
    return math.pow(0.5, age_days / half_life_days)
