"""Sticker service: collect, index, pick and send.

Sending is a first-class operation here because an emoji reply is a real message:
the service uses the Life sender, records the outbound row and consumes one unit
of the session's send budget.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Sequence

from bot import logger
from bot.core.message.components import Image
from bot.core.message.message_event_result import MessageChain
from bot.core.provider.entities import ProviderType
from bot.life.config import EmojiConfig, MemoryEmbeddingConfig
from bot.life.emoji.collect import EmojiCollector
from bot.life.emoji.select import EmojiSelector
from bot.life.emoji.semantic import extract_tags, score_axes
from bot.life.emoji.store import EmojiStore
from bot.life.memory.embedding import EmbeddingClient
from bot.life.memory.store import pack_vector


class EmojiService:
    """Runtime owner of the sticker library."""

    def __init__(
        self,
        context: Any,
        config: EmojiConfig,
        db_helper: Any,
        data_dir: Path,
        life_store: Any = None,
        life_config: Any = None,
        sender: Any = None,
        embedding_config: MemoryEmbeddingConfig | None = None,
    ) -> None:
        """Initialize the service without opening resources.

        Args:
            context: Star context exposing ``provider_manager``.
            config: Sticker settings.
            db_helper: Core async database helper.
            data_dir: Life data directory; files live under ``emoji``.
            life_store: Archive store used as collection input.
            life_config: Root Life configuration used for the allowlist.
            sender: ``LifeSender`` used to deliver stickers.
            embedding_config: Embedding settings shared with the memory layer.
        """
        self.context = context
        self.config = config
        self.data_dir = Path(data_dir) / "emoji"
        self.life_store = life_store
        self.life_config = life_config
        self.sender = sender
        self.embedding_config = embedding_config or MemoryEmbeddingConfig()
        self.store = EmojiStore(db_helper)
        self.embedding: EmbeddingClient | None = None
        self.collector: EmojiCollector | None = None
        self.selector: EmojiSelector | None = None
        self._started = False
        self._counters = {
            "collected": 0,
            "embedded": 0,
            "picks": 0,
            "sent": 0,
            "send_failures": 0,
            "tagged": 0,
        }

    @property
    def enabled(self) -> bool:
        """Return whether the sticker runtime is active."""
        return self._started

    def session_enabled(self, umo: str, group_id: str | None = None) -> bool:
        """Return whether a session is inside the Life allowlist.

        Args:
            umo: Session key.
            group_id: Group id, or None.

        Returns:
            True when the session may use stickers.
        """
        if self.life_config is None:
            return True
        return bool(self.life_config.session_enabled(umo, group_id))

    async def initialize(self) -> None:
        """Create tables, wire the collector and selector."""
        await self.store.ensure_schema()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.embedding_config.enabled:
            self.embedding = EmbeddingClient(self.context, self.embedding_config)
        if self.life_store is not None:
            self.collector = EmojiCollector(
                source=self.life_store,
                target=self.store,
                config=self.config,
                data_dir=self.data_dir,
            )
        self.selector = EmojiSelector(self.store, self.config, self.embedding)
        self._started = True
        logger.info(
            f"[life] emoji layer ready; dir={self.data_dir} "
            f"vector={self.embedding is not None}"
        )

    async def close(self) -> None:
        """Mark the runtime stopped."""
        self._started = False

    async def collect_round(self, session_keys: Sequence[str] | None = None) -> dict:
        """Collect stickers and vectorize new rows.

        Args:
            session_keys: Sessions to process; None discovers active ones.

        Returns:
            Round counters.
        """
        if not self._started or self.collector is None:
            return {"enabled": False}
        result = await self.collector.run(session_keys)
        self._counters["collected"] += int(result.get("stored") or 0)
        await self.embed_pending()
        await self.index_tag_axes()
        return result

    async def embed_pending(self, limit: int = 30) -> int:
        """Vectorize stickers from caption and tags.

        Args:
            limit: Maximum rows per call.

        Returns:
            Number of rows indexed.
        """
        if self.embedding is None or not self.embedding.available:
            return 0
        rows = await self.store.pending_embeddings(limit=limit)
        model = str(self.embedding.snapshot().get("model") or "")
        indexed = 0
        for row in rows:
            text = " ".join(
                [str(row.get("caption") or "")]
                + [str(tag) for tag in (row.get("tags") or [])]
            ).strip()
            if not text:
                continue
            vector = await self.embedding.embed_one(text)
            if not vector:
                break
            await self.store.set_embedding(
                int(row["id"]),
                pack_vector(vector),
                model,
                len(vector),
            )
            indexed += 1
        self._counters["embedded"] += indexed
        return indexed

    async def index_tag_axes(self, limit: int | None = None) -> int:
        """Label stickers with the vision model and embed each tag axis.

        Only runs when ``emoji.semantic_enabled`` is on and an embedding client
        is available; rows are processed in id order so a slow model simply
        spreads the backfill over several rounds.

        Args:
            limit: Stickers to label this round; defaults to the configured
                backfill limit.

        Returns:
            Number of stickers that received tag axes.
        """
        if not self.config.semantic_enabled:
            return 0
        if self.embedding is None or not self.embedding.available:
            return 0
        manager = getattr(self.context, "provider_manager", None)
        provider = None
        if manager is not None:
            try:
                provider = await manager.get_using_provider_async(
                    ProviderType.CHAT_COMPLETION
                )
            except Exception:
                provider = None
        if provider is None:
            return 0
        rows = await self.store.pending_axes(
            limit=limit or self.config.semantic_backfill_limit
        )
        model = str(self.embedding.snapshot().get("model") or "")
        indexed = 0
        for row in rows:
            tags = await extract_tags(
                provider,
                str(row.get("file_path") or ""),
                timeout_seconds=self.config.semantic_timeout_seconds,
            )
            if tags is None:
                continue
            for axis, text in tags.axis_texts().items():
                vector = await self.embedding.embed_one(text)
                if not vector:
                    continue
                await self.store.upsert_axis(
                    str(row["session_key"]),
                    str(row["sha256"]),
                    axis,
                    text,
                    pack_vector(vector),
                    model,
                    len(vector),
                )
            indexed += 1
        self._counters["tagged"] += indexed
        return indexed

    async def pick_semantic(
        self,
        session_key: str,
        context: str,
        require_budget: bool = True,
    ) -> dict[str, Any] | None:
        """Pick a sticker by matching the intent against tag axes.

        Falls back to :meth:`pick` when semantics are disabled, no axis vectors
        exist yet or nothing reaches ``semantic_min_score``.

        Args:
            session_key: Session scope.
            context: Query text describing the desired sticker.
            require_budget: Respect the cooldown and hourly cap.

        Returns:
            Sticker row with ``score`` and ``similarity``, or None.
        """
        if (
            not self.config.semantic_enabled
            or self.embedding is None
            or not self.embedding.available
            or not (context or "").strip()
        ):
            return await self.pick(session_key, context, require_budget=require_budget)
        if self.selector is None:
            return None
        if require_budget and not self.selector.budget.can_send(session_key):
            return None
        rows = await self.store.active_axes(session_key)
        if not rows:
            return await self.pick(session_key, context, require_budget=require_budget)
        recent = int(time.time()) - max(self.config.cooldown_seconds, 0)
        rows = [row for row in rows if int(row.get("last_used_ts") or 0) <= recent]
        vector = await self.embedding.embed_one((context or "").strip()[:200])
        if not vector:
            return None
        scored = score_axes(vector, rows)
        if not scored or scored[0][0] < float(self.config.semantic_min_score):
            return await self.pick(session_key, context, require_budget=require_budget)
        best_score, row = scored[0]
        picked = dict(row)
        picked["score"] = best_score
        picked["similarity"] = best_score
        self._counters["picks"] += 1
        return picked

    async def pick(
        self,
        session_key: str,
        context: str,
        require_budget: bool = True,
    ) -> dict[str, Any] | None:
        """Return a sticker suitable for a context.

        Args:
            session_key: Session scope.
            context: Recent conversation text.
            require_budget: Respect the cooldown and hourly cap.

        Returns:
            Sticker row with score, or None.
        """
        if not self._started or self.selector is None:
            return None
        picked = await self.selector.select(
            session_key,
            context,
            require_budget=require_budget,
        )
        if picked is not None:
            self._counters["picks"] += 1
        return picked

    async def send(
        self,
        session_key: str,
        context: str,
        require_budget: bool = True,
    ) -> dict[str, Any]:
        """Pick and send one sticker.

        Args:
            session_key: Session scope.
            context: Recent conversation text used for selection.
            require_budget: Respect the cooldown and hourly cap.

        Returns:
            Result mapping with ``ok``, ``emoji_id``, ``file_path`` and ``error``.
        """
        if not self._started:
            return {"ok": False, "error": "emoji layer is disabled"}
        if self.sender is None:
            return {"ok": False, "error": "sender is unavailable"}
        picked = await self.pick(session_key, context, require_budget=require_budget)
        if picked is None:
            return {"ok": False, "error": "no suitable sticker"}
        path = Path(str(picked.get("file_path") or ""))
        if not path.is_file():
            return {"ok": False, "error": "sticker file is missing"}
        chain = MessageChain(chain=[Image.fromFileSystem(str(path))])
        result = await self.sender.send_chain(session_key, chain)
        if not result.ok:
            self._counters["send_failures"] += 1
            return {"ok": False, "error": result.error or "send failed"}
        await self.store.bump_usage(int(picked["id"]))
        if self.selector is not None:
            self.selector.budget.mark_sent(session_key)
        if self.life_store is not None:
            try:
                await self.life_store.record_outbound(
                    session_key=session_key,
                    message_id=f"emoji:{picked['id']}:{int(result.platform_message_id or 0)}",
                    status="sent",
                    platform_message_id=result.platform_message_id,
                    attempts=1,
                )
            except Exception:
                logger.debug("[life] failed to record emoji outbound")
        self._counters["sent"] += 1
        return {
            "ok": True,
            "emoji_id": int(picked["id"]),
            "file_path": str(path),
            "score": picked.get("score"),
        }

    async def update(
        self,
        emoji_id: int,
        tags: Sequence[str] | None = None,
        status: str | None = None,
        caption: str | None = None,
    ) -> bool:
        """Update labels or status of one sticker.

        Args:
            emoji_id: Row id.
            tags: Replacement tags.
            status: ``active`` or ``rejected``.
            caption: Replacement caption.

        Returns:
            True when the row was updated.
        """
        if not self._started:
            return False
        return await self.store.update_tags(
            emoji_id,
            tags=list(tags) if tags is not None else None,
            status=status,
            caption=caption,
        )

    async def list_emojis(
        self,
        session_key: str,
        status: str = "active",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return stickers of one session.

        Args:
            session_key: Session scope.
            status: Lifecycle status filter.
            limit: Maximum rows.

        Returns:
            Sticker rows.
        """
        if not self._started:
            return []
        return await self.store.list_emojis(session_key, status=status, limit=limit)

    async def forget(
        self,
        emoji_ids: Sequence[int] | None = None,
        session_key: str | None = None,
    ) -> int:
        """Delete stickers and their files.

        Args:
            emoji_ids: Explicit row ids.
            session_key: Delete every sticker of one session.

        Returns:
            Number of deleted rows.
        """
        if not self._started:
            return 0
        paths = await self.store.delete(
            emoji_ids=list(emoji_ids or []) or None,
            session_key=session_key,
        )
        removed = 0
        for raw in paths:
            path = Path(raw)
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError as exc:
                logger.warning(f"[life] failed to delete emoji file {path}: {exc}")
        return len(paths)

    async def status(self) -> dict[str, Any]:
        """Return a serializable status snapshot.

        Returns:
            Mapping with storage, budget, learning counters and configuration.
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
            "data_dir": str(self.data_dir),
            "storage": storage,
            "collection": self.collector.snapshot() if self.collector else {},
            "budget": self.selector.budget.snapshot() if self.selector else {},
            "counters": dict(self._counters),
            "config": {
                "min_score": self.config.min_score,
                "cooldown_seconds": self.config.cooldown_seconds,
                "max_per_hour": self.config.max_per_hour,
                "semantic_enabled": self.config.semantic_enabled,
                "semantic_min_score": self.config.semantic_min_score,
            },
        }
