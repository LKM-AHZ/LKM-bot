"""Expression service: learn from the archive, select at reply time.

The runtime surface mirrors the memory adapter: one object that owns the store,
the optional FAISS index, the learner and the selector, and exposes
``learn_round`` plus ``inject``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from bot import logger
from bot.core.provider.entities import ProviderType
from bot.life.config import ExpressionConfig, MemoryEmbeddingConfig
from bot.life.expression.index import ExpressionIndex
from bot.life.expression.learn import ExpressionLearner
from bot.life.expression.select import ExpressionSelector
from bot.life.expression.store import ExpressionStore
from bot.life.memory.embedding import EmbeddingClient

_BLOCK_TAG = "life_expression"


class ExpressionService:
    """Runtime owner of learned expressions."""

    def __init__(
        self,
        context: Any,
        config: ExpressionConfig,
        db_helper: Any,
        data_dir: Path,
        life_store: Any = None,
        life_config: Any = None,
        embedding_config: MemoryEmbeddingConfig | None = None,
    ) -> None:
        """Initialize the service without opening resources.

        Args:
            context: Star context exposing ``provider_manager``.
            config: Expression settings.
            db_helper: Core async database helper.
            data_dir: Life data directory; the index lives under ``expression``.
            life_store: Archive store used as learning input.
            life_config: Root Life configuration used for the allowlist.
            embedding_config: Embedding settings shared with the memory layer.
        """
        self.context = context
        self.config = config
        self.data_dir = Path(data_dir) / "expression"
        self.life_store = life_store
        self.life_config = life_config
        self.embedding_config = embedding_config or MemoryEmbeddingConfig()
        self.store = ExpressionStore(db_helper)
        self.embedding: EmbeddingClient | None = None
        self.index: ExpressionIndex | None = None
        self.learner: ExpressionLearner | None = None
        self.selector: ExpressionSelector | None = None
        self._started = False
        self._counters = {
            "learned": 0,
            "merged": 0,
            "indexed": 0,
            "selected": 0,
            "injections": 0,
        }

    @property
    def enabled(self) -> bool:
        """Return whether the expression runtime is active."""
        return self._started

    def session_enabled(self, umo: str, group_id: str | None = None) -> bool:
        """Return whether a session is inside the Life allowlist.

        Args:
            umo: Session key.
            group_id: Group id, or None.

        Returns:
            True when the session may use expressions.
        """
        if self.life_config is None:
            return True
        return bool(self.life_config.session_enabled(umo, group_id))

    async def initialize(self) -> None:
        """Create tables, open the optional index and wire the learner."""
        await self.store.ensure_schema()
        if self.config.use_vector and self.embedding_config.enabled:
            self.embedding = EmbeddingClient(self.context, self.embedding_config)
            self.index = ExpressionIndex(self.data_dir, self.embedding)
            if not await self.index.initialize():
                self.index = None
        if self.life_store is not None:
            self.learner = ExpressionLearner(
                source=self.life_store,
                target=self.store,
                config=self.config,
                summarizer=self._llm_summarizer if self.config.use_llm else None,
            )
        self.selector = ExpressionSelector(self.store, self.config, self.index)
        self._started = True
        logger.info(f"[life] expression layer ready; vector={self.index is not None}")

    async def close(self) -> None:
        """Close the index and mark the runtime stopped."""
        if self.index is not None:
            await self.index.close()
        self._started = False

    async def _llm_summarizer(self, system_prompt: str, user_prompt: str) -> str | None:
        """Run one extraction call through the active chat provider.

        Args:
            system_prompt: Instructions for the extractor.
            user_prompt: Transcript payload.

        Returns:
            Completion text, or None when no provider is available.
        """
        manager = getattr(self.context, "provider_manager", None)
        if manager is None:
            return None
        provider = await manager.get_using_provider_async(ProviderType.CHAT_COMPLETION)
        if provider is None:
            return None
        response = await provider.text_chat(
            prompt=user_prompt,
            system_prompt=system_prompt,
        )
        return (response.completion_text or "").strip() or None

    async def learn_round(self, session_keys: Sequence[str] | None = None) -> dict:
        """Learn from the archive and sync the vector index.

        Args:
            session_keys: Sessions to process; None discovers active ones.

        Returns:
            Round counters.
        """
        if not self._started or self.learner is None:
            return {"enabled": False}
        result = await self.learner.run(session_keys)
        self._counters["learned"] += int(result.get("inserted") or 0)
        self._counters["merged"] += int(result.get("merged") or 0)
        await self.sync_index()
        return result

    async def sync_index(self, limit: int = 200) -> int:
        """Push pending expressions into the FAISS index.

        Args:
            limit: Maximum rows per call.

        Returns:
            Number of rows indexed.
        """
        if self.index is None or not self.index.available:
            return 0
        pending = await self.store.pending_for_index(limit=limit)
        if not pending:
            return 0
        indexed = await self.index.sync(pending, self.store)
        self._counters["indexed"] += indexed
        return indexed

    async def inject(self, event: Any, req: Any) -> bool:
        """Append selected expressions to the provider request.

        Args:
            event: Bot message event carrying the session identity.
            req: ``ProviderRequest`` about to be sent to the model.

        Returns:
            True when the prompt was modified.
        """
        if not self._started or not self.config.enabled or self.selector is None:
            return False
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return False
        getter = getattr(event, "get_group_id", None)
        group_id = getter() if callable(getter) else None
        if not self.session_enabled(umo, group_id):
            return False
        system_prompt = getattr(req, "system_prompt", "") or ""
        if _BLOCK_TAG in system_prompt:
            return False
        prompt = (getattr(req, "prompt", "") or "").strip()
        if not prompt:
            return False
        scene = "group" if group_id else "private"
        if self.config.scene != "any" and self.config.scene != scene:
            return False
        selected = await self.selector.select(umo, prompt[:500], scene=scene)
        if not selected:
            return False
        block = self.selector.build_block(selected)
        if not block:
            return False
        req.system_prompt = f"{system_prompt}{block}"
        await self.store.bump_usage([int(row["id"]) for row in selected])
        self._counters["selected"] += len(selected)
        self._counters["injections"] += 1
        return True

    async def list_expressions(
        self,
        session_key: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return learned expressions of one session.

        Args:
            session_key: Session scope.
            limit: Maximum rows.

        Returns:
            Expression rows.
        """
        if not self._started:
            return []
        return await self.store.list_expressions(session_key, limit=limit)

    async def forget(
        self,
        expression_ids: Sequence[int] | None = None,
        session_key: str | None = None,
    ) -> int:
        """Delete expressions and drop them from the index.

        Args:
            expression_ids: Explicit row ids.
            session_key: Delete every expression of one session.

        Returns:
            Number of deleted rows.
        """
        if not self._started:
            return 0
        ids = [int(value) for value in expression_ids] if expression_ids else []
        if self.index is not None and self.index.available and ids:
            await self.index.delete(ids)
        return await self.store.delete(
            expression_ids=ids or None,
            session_key=session_key,
        )

    async def status(self) -> dict[str, Any]:
        """Return a serializable status snapshot.

        Returns:
            Mapping with storage, index, learning counters and configuration.
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
            "storage": storage,
            "index": self.index.snapshot() if self.index else None,
            "learning": self.learner.snapshot() if self.learner else {},
            "counters": dict(self._counters),
            "config": {
                "top_k": self.config.top_k,
                "min_score": self.config.min_score,
                "scene": self.config.scene,
                "use_llm": self.config.use_llm,
                "use_vector": self.config.use_vector,
            },
        }
