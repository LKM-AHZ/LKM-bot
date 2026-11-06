"""Long-term memory adapter used by the Life runtime.

The adapter is the stable surface of S2: the service, the prompt hook and the
agent tools all talk to it, while the storage engine underneath is pluggable.
That keeps the rest of the Life layer independent of whether memory is served by
the built-in SQLite engine or by the upstream ``a_memorix`` engine.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Sequence

from bot import logger
from bot.core.provider.entities import ProviderType
from bot.life.config import MemoryConfig
from bot.life.memory.digest import DigestBuilder
from bot.life.memory.embedding import EmbeddingClient, EmbeddingUnavailable
from bot.life.memory.store import MemoryStore, recency_weight
from bot.life.memory.upstream import AMemorixBridge

_BLOCK_TAG = "life_memory"
_MAX_LINE_CHARS = 220


def _format_ts(ts: int) -> str:
    """Format a timestamp for prompt display.

    Args:
        ts: Unix timestamp.

    Returns:
        ``YYYY-MM-DD HH:MM`` in local time.
    """
    if not ts:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


class MemoryAdapter:
    """Facade over storage, embeddings, digesting and prompt injection."""

    def __init__(
        self,
        context: Any,
        config: MemoryConfig,
        db_helper: Any,
        data_dir: Path,
        life_store: Any = None,
        scope_check: Any = None,
        on_facts: Any = None,
    ) -> None:
        """Initialize the adapter without opening resources.

        Args:
            context: Star context exposing ``provider_manager``.
            config: Memory configuration block.
            db_helper: Core async database helper.
            data_dir: Life data directory; the engine lives under ``memory``.
            life_store: Archive store used as digest input.
            scope_check: Callable ``(umo, group_id) -> bool`` implementing the
                gray-release allowlist; omitted means every session is allowed.
            on_facts: Optional sink receiving ``(session_key, fact_rows)`` so the
                person layer can attach extracted facts to people.
        """
        self.context = context
        self.config = config
        self.data_dir = Path(data_dir) / "memory"
        self.life_store = life_store
        self._scope_check = scope_check
        self._on_facts = on_facts
        self.store = MemoryStore(db_helper, retention_days=config.retention_days)
        self.embedding = EmbeddingClient(context, config.embedding)
        self.digest: DigestBuilder | None = None
        self.upstream: AMemorixBridge | None = None
        self._started = False
        self._counters = {
            "writes": 0,
            "duplicates": 0,
            "searches": 0,
            "injections": 0,
            "injected_hits": 0,
            "embedded": 0,
            "embed_failures": 0,
            "upstream_queries": 0,
        }

    @property
    def enabled(self) -> bool:
        """Return whether the memory runtime is active."""
        return self._started

    async def initialize(self) -> None:
        """Create the engine, wire the digest pipeline and start upstream."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        await self.store.ensure_schema()
        if self.life_store is not None:
            self.digest = DigestBuilder(
                source=self.life_store,
                target=self.store,
                config=self.config.digest,
                summarizer=self._llm_summarizer if self.config.digest.use_llm else None,
                on_facts=self._on_facts,
            )
        if self.config.upstream.engine == "a_memorix":
            self.upstream = AMemorixBridge(
                data_dir=self._upstream_dir(),
                embedding_client=self.embedding,
                namespace_prefix=self.config.upstream.namespace_prefix,
            )
            if not await self.upstream.start():
                logger.warning(
                    "[life] a_memorix engine unavailable; using the local memory engine"
                )
                self.upstream = None
        self._started = True
        logger.info(
            f"[life] memory ready; engine={self.engine_name} dir={self.data_dir}"
        )

    def _upstream_dir(self) -> Path:
        """Return the resolved upstream engine directory.

        Returns:
            Absolute directory for the A_memorix data root.
        """
        if self.config.upstream.data_dir:
            path = Path(self.config.upstream.data_dir)
            return path if path.is_absolute() else self.data_dir.parent / path
        return self.data_dir / "upstream"

    @property
    def engine_name(self) -> str:
        """Return the active engine identifier."""
        return "a_memorix" if self.upstream is not None else "local"

    async def close(self) -> None:
        """Release the upstream engine and clear the started flag."""
        if self.upstream is not None:
            await self.upstream.close()
        self._started = False

    async def _llm_summarizer(self, system_prompt: str, user_prompt: str) -> str | None:
        """Run one summarization call through the active chat provider.

        Args:
            system_prompt: System instructions for the summarizer.
            user_prompt: User payload containing the transcript.

        Returns:
            Completion text, or None when no provider is available.
        """
        manager = getattr(self.context, "provider_manager", None)
        if manager is None:
            return None
        provider = await manager.get_using_provider_async(ProviderType.CHAT_COMPLETION)
        if provider is None:
            return None
        timeout = max(self.config.digest.llm_timeout_seconds, 1.0)
        async with asyncio.timeout(timeout):
            response = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
        return (response.completion_text or "").strip() or None

    async def remember(
        self,
        content: str,
        session_key: str,
        kind: str = "note",
        person_id: str | None = None,
        importance: float = 0.6,
        meta: dict[str, Any] | None = None,
        embed: bool = True,
    ) -> int:
        """Store one memory row and attach a vector when possible.

        Args:
            content: Memory body.
            session_key: Owning session.
            kind: ``summary`` / ``fact`` / ``episode`` / ``note``.
            person_id: Related person, when known.
            importance: Salience in ``[0, 1]``.
            meta: Free-form metadata.
            embed: Whether to vectorize immediately.

        Returns:
            Number of inserted rows (0 when the row already exists).
        """
        if not self._started:
            return 0
        text = content.strip()
        if not text:
            return 0
        inserted = await self.store.insert_many(
            [
                {
                    "kind": kind,
                    "session_key": session_key,
                    "content": text,
                    "person_id": person_id,
                    "importance": min(max(importance, 0.0), 1.0),
                    "ts": int(time.time()),
                    "meta": meta or {},
                }
            ]
        )
        self._counters["writes"] += inserted
        if not inserted:
            self._counters["duplicates"] += 1
            return 0
        if embed and self.embedding.available:
            await self.embed_pending(limit=4)
        return inserted

    async def embed_pending(self, limit: int = 40) -> int:
        """Vectorize rows that have no embedding for the active model.

        Args:
            limit: Maximum number of rows per call.

        Returns:
            Number of rows that received a vector.
        """
        if not self.embedding.available:
            return 0
        model = self.embedding.snapshot().get("model") or ""
        rows = await self.store.pending_embeddings(limit=limit, model=model or None)
        embedded = 0
        for row in rows:
            text = (row.get("content") or "").strip()
            if not text:
                continue
            try:
                vector = await self.embedding.embed_one(text)
            except EmbeddingUnavailable:
                vector = None
            if not vector:
                self._counters["embed_failures"] += 1
                break
            await self.store.set_embedding(
                int(row["id"]),
                vector,
                str(self.embedding.snapshot().get("model") or ""),
            )
            embedded += 1
        self._counters["embedded"] += embedded
        return embedded

    async def search(
        self,
        query: str,
        session_key: str,
        mode: str = "hybrid",
        limit: int = 5,
        kinds: Sequence[str] | None = None,
        time_start: int | None = None,
        time_end: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search memory with one of the five supported modes.

        Args:
            query: Natural-language query.
            session_key: Session scope.
            mode: ``search`` / ``time`` / ``hybrid`` / ``episode`` / ``aggregate``.
            limit: Maximum hits.
            kinds: Kind filter.
            time_start: Explicit window start for time-based modes.
            time_end: Explicit window end.

        Returns:
            Hit dictionaries ordered best first.
        """
        if not self._started:
            return []
        self._counters["searches"] += 1
        if self.upstream is not None:
            hits = await self.upstream.search(
                namespace=session_key,
                query=query,
                mode=mode,
                limit=limit,
                person_id="",
            )
            if hits:
                self._counters["upstream_queries"] += 1
                return hits
        vector = await self.embedding.embed_one(query) if query else None
        hits = await self.store.search(
            session_key=session_key,
            query=query,
            vector=vector,
            mode=mode,
            limit=limit,
            kinds=kinds,
            time_start=time_start,
            time_end=time_end,
        )
        return hits

    async def inject(self, event: Any, req: Any) -> bool:
        """Append retrieved memories to the provider request system prompt.

        Args:
            event: Bot message event carrying the session identity.
            req: ``ProviderRequest`` about to be sent to the model.

        Returns:
            True when the prompt was modified.
        """
        if not self._started or not self.config.inject.enabled:
            return False
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return False
        if self._scope_check is not None:
            getter = getattr(event, "get_group_id", None)
            group_id = getter() if callable(getter) else None
            if not self._scope_check(umo, group_id):
                return False
        system_prompt = getattr(req, "system_prompt", "") or ""
        if _BLOCK_TAG in system_prompt:
            return False
        prompt = (getattr(req, "prompt", "") or "").strip()
        if not prompt:
            return False
        since_ts = 0
        if self.config.inject.max_age_days > 0:
            since_ts = int(time.time()) - self.config.inject.max_age_days * 86400
        hits = await self.search(
            query=prompt[:500],
            session_key=umo,
            mode="hybrid",
            limit=self.config.inject.top_k,
            kinds=self.config.inject.kinds,
        )
        lines: list[str] = []
        used = 0
        for hit in hits:
            ts = int(hit.get("ts") or 0)
            # Upstream hits may not carry a timestamp; keep their raw score.
            score = float(hit.get("score") or 0.0) * (recency_weight(ts) if ts else 1.0)
            if score < self.config.inject.min_score:
                continue
            text = " ".join(str(hit.get("content") or "").split())
            if not text:
                continue
            line = f"- [{_format_ts(ts)}][{hit.get('kind')}] {text[:_MAX_LINE_CHARS]}"
            if since_ts and ts and ts < since_ts:
                continue
            if used + len(line) > self.config.inject.max_chars and lines:
                break
            lines.append(line)
            used += len(line)
        if not lines:
            return False
        block = (
            f"\n\n<{_BLOCK_TAG}>\n"
            "以下是你记得的长期记忆，可能已经过时或与当前话题无关；"
            "只有当它确实有助于回答时才使用，不要向用户复述这份列表。\n"
            + "\n".join(lines)
            + f"\n</{_BLOCK_TAG}>"
        )
        req.system_prompt = f"{system_prompt}{block}"
        self._counters["injections"] += 1
        self._counters["injected_hits"] += len(lines)
        return True

    async def digest_round(self, session_keys: Sequence[str] | None = None) -> dict:
        """Run one digest round and vectorize the new rows.

        Args:
            session_keys: Sessions to digest; None discovers active sessions.

        Returns:
            Round counters, or a disabled marker.
        """
        if not self._started or self.digest is None:
            return {"enabled": False}
        result = await self.digest.run(session_keys)
        await self.embed_pending()
        return result

    async def delete(
        self,
        memory_ids: Sequence[int] | None = None,
        session_key: str | None = None,
    ) -> int:
        """Delete memories by id or session.

        Args:
            memory_ids: Explicit row ids.
            session_key: Delete the whole session.

        Returns:
            Number of deleted rows.
        """
        return await self.store.delete(memory_ids=memory_ids, session_key=session_key)

    async def export(self, session_key: str | None = None) -> list[dict[str, Any]]:
        """Export memories as JSON-serializable rows.

        Args:
            session_key: Restrict to one session.

        Returns:
            Exported rows.
        """
        return await self.store.export(session_key)

    async def status(self) -> dict[str, Any]:
        """Return the memory status snapshot used by the dashboard.

        Returns:
            Mapping with engine, storage, embedding, digest and counters.
        """
        storage: dict[str, Any] = {}
        if self._started:
            try:
                storage = await self.store.stats()
            except Exception as exc:
                storage = {"error": str(exc)}
        return {
            "enabled": self._started,
            "configured": self.config.enabled,
            "engine": self.engine_name,
            "data_dir": str(self.data_dir),
            "storage": storage,
            "embedding": self.embedding.snapshot(),
            "inject": {
                "enabled": self.config.inject.enabled,
                "top_k": self.config.inject.top_k,
                "max_chars": self.config.inject.max_chars,
                "kinds": list(self.config.inject.kinds),
            },
            "digest": {
                "enabled": self.config.digest.enabled,
                "interval_minutes": self.config.digest.interval_minutes,
                "counters": self.digest.snapshot() if self.digest else {},
            },
            "upstream": self.upstream.snapshot() if self.upstream else None,
            "counters": dict(self._counters),
        }
