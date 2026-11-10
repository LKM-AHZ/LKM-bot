"""Storage for group slang and the learning cursor."""

from __future__ import annotations

import hashlib
import time
from typing import Any, Iterable, Sequence

from sqlalchemy import desc, func, select
from sqlmodel import col

from bot.core.db import BaseDatabase
from bot.life.common import CursorStoreMixin, delete_rows, ensure_tables, get_row
from bot.life.config import JargonConfig
from bot.life.jargon.models import LifeJargon, LifeJargonCursor


def term_checksum(term: str) -> str:
    """Return the dedupe hash of a term.

    Args:
        term: Slang term.

    Returns:
        Hex digest used by the unique constraint.
    """
    return hashlib.sha256(term.strip().casefold().encode("utf-8")).hexdigest()


def _normalize_meaning(meaning: str) -> str:
    """Return the comparison form of an explanation.

    Args:
        meaning: Explanation text.

    Returns:
        Whitespace-free, casefolded text.
    """
    return "".join(meaning.split()).casefold()


class JargonStore(CursorStoreMixin):
    """Async SQLite store for slang terms."""

    cursor_model = LifeJargonCursor

    def __init__(
        self, db_helper: BaseDatabase, config: JargonConfig | None = None
    ) -> None:
        """Initialize the store.

        Args:
            db_helper: Core async database helper.
            config: Jargon settings controlling confidence and text limits.
        """
        self.db = db_helper
        self.config = config or JargonConfig()

    async def ensure_schema(self) -> None:
        """Create the jargon tables when missing."""
        await ensure_tables(self.db, LifeJargon, LifeJargonCursor)

    async def upsert_many(self, rows: Sequence[dict[str, Any]]) -> dict[str, int]:
        """Insert new terms and flag conflicting explanations.

        Args:
            rows: Payloads with ``session_key``, ``term``, ``meaning``,
                ``confidence``, ``source`` and optional ``evidence``/``last_seen_ts``.

        Returns:
            Counters with ``inserted``, ``updated`` and ``conflicts``.
        """
        if not rows:
            return {"inserted": 0, "updated": 0, "conflicts": 0}
        now = int(time.time())
        inserted = updated = conflicts = 0
        async with self.db.get_db() as session:
            for row in rows:
                term = str(row.get("term") or "").strip()
                meaning = str(row.get("meaning") or "").strip()
                if not term:
                    continue
                session_key = str(row["session_key"])
                checksum = term_checksum(term)
                result = await session.execute(
                    select(LifeJargon).where(
                        col(LifeJargon.session_key) == session_key,
                        col(LifeJargon.checksum) == checksum,
                    )
                )
                existing = result.scalars().first()
                evidence = list(row.get("evidence") or [])
                if existing is None:
                    session.add(
                        LifeJargon(
                            session_key=session_key,
                            term=term,
                            meaning=meaning,
                            confidence=float(row.get("confidence") or 0.6),
                            status=str(row.get("status") or "active"),
                            source=str(row.get("source") or "heuristic"),
                            evidence=evidence,
                            checksum=checksum,
                            last_seen_ts=int(row.get("last_seen_ts") or now),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    inserted += 1
                    continue
                old_meaning = _normalize_meaning(str(existing.meaning or ""))
                new_meaning = _normalize_meaning(meaning)
                merged_evidence = list(existing.evidence or [])
                for sample in evidence:
                    if sample and sample not in merged_evidence:
                        merged_evidence.append(sample)
                existing.evidence = merged_evidence[-6:]
                existing.last_seen_ts = max(
                    int(existing.last_seen_ts or 0),
                    int(row.get("last_seen_ts") or now),
                )
                existing.updated_at = now
                if new_meaning and old_meaning and new_meaning != old_meaning:
                    existing.status = "pending"
                    existing.confidence = min(float(existing.confidence or 0.6), 0.5)
                    conflicts += 1
                elif not old_meaning and new_meaning:
                    existing.meaning = meaning
                    existing.confidence = max(
                        float(existing.confidence or 0.0),
                        float(row.get("confidence") or 0.6),
                    )
                updated += 1
            await session.commit()
        return {"inserted": inserted, "updated": updated, "conflicts": conflicts}

    async def get(self, jargon_id: int) -> dict[str, Any] | None:
        """Return one jargon row.

        Args:
            jargon_id: Row id.

        Returns:
            Row dictionary, or None when missing.
        """
        return await get_row(self.db, LifeJargon, jargon_id)

    async def get_by_term(self, session_key: str, term: str) -> dict[str, Any] | None:
        """Return one term of a session.

        Args:
            session_key: Session scope.
            term: Slang term.

        Returns:
            Row dictionary, or None when unknown.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifeJargon).where(
                    col(LifeJargon.session_key) == session_key,
                    col(LifeJargon.checksum) == term_checksum(term),
                )
            )
            row = result.scalars().first()
            return row.model_dump() if row else None

    async def list_terms(
        self,
        session_key: str,
        statuses: Iterable[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return terms of one session.

        Args:
            session_key: Session scope.
            statuses: Status filter; defaults to every status.
            limit: Maximum rows.

        Returns:
            Row dictionaries ordered by confidence then recency.
        """
        query = select(LifeJargon).where(col(LifeJargon.session_key) == session_key)
        if statuses:
            query = query.where(col(LifeJargon.status).in_(tuple(statuses)))
        async with self.db.get_db() as session:
            result = await session.execute(
                query.order_by(
                    desc(col(LifeJargon.confidence)),
                    desc(col(LifeJargon.last_seen_ts)),
                ).limit(max(limit, 1))
            )
            return [row.model_dump() for row in result.scalars().all()]

    async def review(
        self,
        jargon_id: int,
        status: str,
        meaning: str | None = None,
    ) -> bool:
        """Confirm, reject or correct one term.

        Args:
            jargon_id: Row id.
            status: ``active`` or ``rejected``.
            meaning: Optional corrected explanation.

        Returns:
            True when the row was updated.
        """
        if status not in ("active", "rejected", "pending"):
            raise ValueError(f"unsupported jargon status: {status}")
        async with self.db.get_db() as session:
            row = await session.get(LifeJargon, jargon_id)
            if row is None:
                return False
            row.status = status
            if meaning is not None:
                row.meaning = meaning.strip()[: self.config.max_meaning_chars]
                row.source = "manual"
                row.confidence = max(float(row.confidence or 0.0), 0.9)
            row.updated_at = int(time.time())
            await session.commit()
            return True

    async def bump_usage(self, jargon_ids: Iterable[int]) -> int:
        """Record that terms were surfaced to the model.

        Args:
            jargon_ids: Rows that were injected.

        Returns:
            Number of rows updated.
        """
        ids = [int(value) for value in jargon_ids]
        if not ids:
            return 0
        now = int(time.time())
        updated = 0
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifeJargon).where(col(LifeJargon.id).in_(ids))
            )
            for row in result.scalars().all():
                row.usage_count = int(row.usage_count or 0) + 1
                row.last_seen_ts = now
                row.updated_at = now
                updated += 1
            await session.commit()
        return updated

    async def delete(
        self,
        jargon_ids: Iterable[int] | None = None,
        session_key: str | None = None,
    ) -> int:
        """Delete terms by id or session.

        Args:
            jargon_ids: Explicit row ids.
            session_key: Delete every term of one session.

        Returns:
            Number of deleted rows.

        Raises:
            ValueError: When neither filter is given.
        """
        return await delete_rows(self.db, LifeJargon, jargon_ids, session_key, "jargon")

    async def stats(self) -> dict[str, Any]:
        """Return jargon counters.

        Returns:
            Mapping with totals, per-status counts and pending reviews.
        """
        async with self.db.get_db() as session:
            total = (
                await session.execute(select(func.count()).select_from(LifeJargon))
            ).scalar_one()
            by_status = await session.execute(
                select(col(LifeJargon.status), func.count()).group_by(
                    col(LifeJargon.status)
                )
            )
        return {
            "total": int(total or 0),
            "by_status": {status: int(count) for status, count in by_status.all()},
        }
