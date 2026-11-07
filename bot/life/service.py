"""Life layer runtime.

The service owns the ingest queue, the archive store, the session registry and
the proactive sender. It is created by the core lifecycle and is a no-op unless
``LifeConfig.enabled`` is true.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

from bot import logger
from bot.core.message.components import Plain
from bot.core.message.message_event_result import MessageChain
from bot.life.config import (
    LifeConfig,
    default_config_path,
    load_life_config,
    save_life_config,
)
from bot.life.emoji.service import EmojiService
from bot.life.emoji.tools import register_emoji_tools, unregister_emoji_tools
from bot.life.expression.service import ExpressionService
from bot.life.ingest import IngestedMessage, normalize_event
from bot.life.jargon.service import JargonService
from bot.life.memory.adapter import MemoryAdapter
from bot.life.memory.tools import register_memory_tools, unregister_memory_tools
from bot.life.person.service import PersonService
from bot.life.sender import LifeSender, SendResult
from bot.life.session.registry import SessionRegistry
from bot.life.store import LifeStore

_life_service: LifeService | None = None


def get_life_service() -> LifeService | None:
    """Return the process-wide Life service, if initialized."""
    return _life_service


def set_life_service(service: LifeService | None) -> None:
    """Bind or clear the process-wide Life service.

    Args:
        service: Service instance to expose, or None to clear.
    """
    global _life_service
    _life_service = service


class LifeService:
    """Runtime owner of the Life layer."""

    def __init__(
        self,
        db_helper: Any,
        platform_manager: Any = None,
        star_context: Any = None,
        config: LifeConfig | None = None,
        config_path: str | Path | None = None,
        data_dir: str | Path | None = None,
        maintenance_interval: float = 6 * 60 * 60,
    ) -> None:
        """Initialize the service without touching the filesystem.

        Args:
            db_helper: Core async database helper.
            platform_manager: Platform manager used for proactive sends.
            star_context: Plugin context used for proactive sends.
            config: Explicit configuration; otherwise loaded from disk.
            config_path: Alternative configuration path for tests or tooling.
            data_dir: Override for the Life data directory.
            maintenance_interval: Seconds between retention cleanup runs.
        """
        self.db = db_helper
        self.platform_manager = platform_manager
        self.star_context = star_context
        self._provided_config = config
        self.config_path = Path(config_path) if config_path else default_config_path()
        self._data_dir_override = data_dir
        self.maintenance_interval = maintenance_interval

        self.config = LifeConfig()
        self.data_dir: Path | None = None
        self.store: LifeStore | None = None
        self.sender: LifeSender | None = None
        self.memory: MemoryAdapter | None = None
        self.person: PersonService | None = None
        self.expression: ExpressionService | None = None
        self.jargon: JargonService | None = None
        self.emoji: EmojiService | None = None
        self.registry = SessionRegistry()
        self.started_at = 0
        self._queue: asyncio.Queue[IngestedMessage | None] | None = None
        self._worker: asyncio.Task | None = None
        self._maintenance: asyncio.Task | None = None
        self._memory_task: asyncio.Task | None = None
        self._expression_task: asyncio.Task | None = None
        self._jargon_task: asyncio.Task | None = None
        self._emoji_task: asyncio.Task | None = None
        self._running = False
        self._counters = {
            "ingested": 0,
            "written": 0,
            "duplicates": 0,
            "dropped": 0,
            "failed": 0,
            "normalize_failed": 0,
        }

    @property
    def running(self) -> bool:
        """Return whether the runtime is active."""
        return self._running

    async def initialize(self) -> None:
        """Load configuration and start the archive runtime when enabled."""
        self.config = self._provided_config or load_life_config(self.config_path)
        if not self.config_path.exists():
            save_life_config(self.config, self.config_path)
        self.data_dir = self.config.resolved_data_dir(self._data_dir_override)

        if not self.config.enabled:
            logger.info(
                "[life] disabled; set enabled=true in %s to turn it on",
                self.config_path,
            )
            return

        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "state").mkdir(exist_ok=True)
        (self.data_dir / "memory").mkdir(exist_ok=True)

        self.store = LifeStore(
            self.db,
            retention_days=self.config.ingest.retention_days,
        )
        await self.store.ensure_schema()
        if self.config.sender.enabled:
            self.sender = LifeSender(
                platform_manager=self.platform_manager,
                context=self.star_context,
                timeout_seconds=self.config.sender.timeout_seconds,
            )

        if self.config.emoji.enabled:
            self.emoji = EmojiService(
                context=self.star_context,
                config=self.config.emoji,
                db_helper=self.db,
                data_dir=self.data_dir,
                life_store=self.store,
                life_config=self.config,
                sender=self.sender,
                embedding_config=self.config.memory.embedding,
            )
            await self.emoji.initialize()

        if self.config.person.enabled:
            self.person = PersonService(
                context=self.star_context,
                config=self.config.person,
                db_helper=self.db,
                life_config=self.config,
            )
            await self.person.initialize()

        if self.config.memory.enabled:
            self.memory = MemoryAdapter(
                context=self.star_context,
                config=self.config.memory,
                db_helper=self.db,
                data_dir=self.data_dir,
                life_store=self.store,
                scope_check=self.config.session_enabled,
                on_facts=self._ingest_person_facts,
            )
            await self.memory.initialize()

        if self.config.expression.enabled:
            self.expression = ExpressionService(
                context=self.star_context,
                config=self.config.expression,
                db_helper=self.db,
                data_dir=self.data_dir,
                life_store=self.store,
                life_config=self.config,
                embedding_config=self.config.memory.embedding,
            )
            await self.expression.initialize()

        if self.config.jargon.enabled:
            self.jargon = JargonService(
                context=self.star_context,
                config=self.config.jargon,
                db_helper=self.db,
                life_store=self.store,
                life_config=self.config,
            )
            await self.jargon.initialize()

        if self.star_context is not None and (
            self.memory is not None or self.emoji is not None
        ):
            manager = getattr(
                getattr(self.star_context, "provider_manager", None),
                "llm_tools",
                None,
            )
            if manager is not None:
                if self.memory is not None:
                    register_memory_tools(manager, self)
                if self.emoji is not None:
                    register_emoji_tools(manager, self.emoji)

        self._queue = asyncio.Queue(maxsize=self.config.ingest.queue_size)
        self._running = True
        self.started_at = int(time.time())
        self._worker = asyncio.create_task(self._consume_loop(), name="life_ingest")
        self._maintenance = asyncio.create_task(
            self._maintenance_loop(),
            name="life_maintenance",
        )
        if self.memory is not None and self.config.memory.digest.enabled:
            self._memory_task = asyncio.create_task(
                self._memory_loop(),
                name="life_memory",
            )
        if self.expression is not None:
            self._expression_task = asyncio.create_task(
                self._expression_loop(),
                name="life_expression",
            )
        if self.jargon is not None:
            self._jargon_task = asyncio.create_task(
                self._jargon_loop(),
                name="life_jargon",
            )
        if self.emoji is not None:
            self._emoji_task = asyncio.create_task(
                self._emoji_loop(),
                name="life_emoji",
            )
        set_life_service(self)
        logger.info(
            "[life] enabled; data_dir=%s scope_mode=%s memory=%s",
            self.data_dir,
            self.config.scope.mode,
            self.memory.engine_name if self.memory else "off",
        )

    async def stop(self) -> None:
        """Flush the queue and stop background tasks."""
        if not self._running:
            return
        self._running = False
        if self._queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)
        if self._worker is not None:
            try:
                await asyncio.wait_for(self._worker, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._worker
        if self._maintenance is not None:
            self._maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._maintenance
        if self._memory_task is not None:
            self._memory_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._memory_task
        if self._expression_task is not None:
            self._expression_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._expression_task
        if self._jargon_task is not None:
            self._jargon_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._jargon_task
        if self._emoji_task is not None:
            self._emoji_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._emoji_task
        if self.expression is not None:
            await self.expression.close()
        if self.jargon is not None:
            await self.jargon.close()
        if self.emoji is not None:
            await self.emoji.close()
        if self.person is not None:
            await self.person.close()
        if self.star_context is not None and (
            self.memory is not None or self.emoji is not None
        ):
            manager = getattr(
                getattr(self.star_context, "provider_manager", None),
                "llm_tools",
                None,
            )
            if manager is not None:
                if self.memory is not None:
                    unregister_memory_tools(manager)
                if self.emoji is not None:
                    unregister_emoji_tools(manager)
        if self.memory is not None:
            await self.memory.close()
        if get_life_service() is self:
            set_life_service(None)
        logger.info("[life] stopped; counters=%s", self._counters)

    async def ingest(self, event: Any) -> None:
        """Archive one inbound event.

        Args:
            event: ``BotMessageEvent``-like object from the pipeline.
        """
        if not self._running or self._queue is None:
            return
        if not self.config.ingest.enabled:
            return
        try:
            umo = event.unified_msg_origin
            group_id = event.get_group_id() or None
        except Exception:
            self._counters["normalize_failed"] += 1
            return
        if not self.config.session_enabled(umo, group_id):
            return
        try:
            message = normalize_event(event, self.config.ingest)
        except Exception:
            logger.exception("[life] failed to normalize event")
            self._counters["normalize_failed"] += 1
            return
        self.registry.touch(message)
        self._counters["ingested"] += 1
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self._counters["dropped"] += 1
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(message)

    async def _consume_loop(self) -> None:
        """Drain the queue in batches and persist them."""
        if self._queue is None or self.store is None:
            return
        batch_size = max(self.config.ingest.batch_size, 1)
        while True:
            item = await self._queue.get()
            if item is None:
                return
            batch = [item]
            while len(batch) < batch_size:
                try:
                    next_item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if next_item is None:
                    await self._flush(batch)
                    return
                batch.append(next_item)
            await self._flush(batch)

    async def _flush(self, batch: list[IngestedMessage]) -> None:
        """Persist one batch, counting duplicates and failures.

        Args:
            batch: Messages drained from the queue.
        """
        if self.store is None:
            return
        try:
            inserted = await self.store.insert_batch(
                [message.to_row() for message in batch]
            )
        except Exception:
            logger.exception("[life] failed to persist %d messages", len(batch))
            self._counters["failed"] += len(batch)
            return
        self._counters["written"] += inserted
        self._counters["duplicates"] += len(batch) - inserted
        if self.person is not None:
            try:
                await self.person.observe_messages(batch)
            except Exception:
                logger.exception("[life] person observe failed")

    async def _maintenance_loop(self) -> None:
        """Run retention cleanup periodically."""
        if self.store is None:
            return
        while self._running:
            await asyncio.sleep(self.maintenance_interval)
            if not self._running:
                return
            try:
                await self.store.cleanup()
            except Exception:
                logger.exception("[life] retention cleanup failed")
            if self.person is not None:
                try:
                    await self.person.store.decay()
                except Exception:
                    logger.exception("[life] person decay failed")

    async def _expression_loop(self) -> None:
        """Learn expressions from the archive on a fixed interval."""
        if self.expression is None:
            return
        interval = max(self.config.expression.learn_interval_minutes, 1) * 60
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                return
            try:
                await self.expression.learn_round()
            except Exception:
                logger.exception("[life] expression learning round failed")

    async def _jargon_loop(self) -> None:
        """Learn slang from the archive on a fixed interval."""
        if self.jargon is None:
            return
        interval = max(self.config.jargon.learn_interval_minutes, 1) * 60
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                return
            try:
                await self.jargon.learn_round()
            except Exception:
                logger.exception("[life] jargon learning round failed")

    async def _emoji_loop(self) -> None:
        """Collect stickers from the archive on a fixed interval."""
        if self.emoji is None:
            return
        interval = max(self.config.emoji.collect_interval_minutes, 1) * 60
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                return
            try:
                await self.emoji.collect_round()
            except Exception:
                logger.exception("[life] emoji collection round failed")

    async def _ingest_person_facts(self, session_key: str, facts: list[dict]) -> None:
        """Attach digest facts to people.

        Args:
            session_key: Session the facts belong to.
            facts: Fact rows produced by the memory digest.
        """
        if self.person is None:
            return
        await self.person.ingest_facts(session_key, facts)

    async def _memory_loop(self) -> None:
        """Digest archived chat into memory and refresh vectors periodically."""
        if self.memory is None:
            return
        interval = max(self.config.memory.digest.interval_minutes, 1) * 60
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                return
            try:
                await self.memory.digest_round()
                await self.memory.store.cleanup()
            except Exception:
                logger.exception("[life] memory digest round failed")

    async def send_text(self, umo: str, text: str) -> SendResult:
        """Send a plain-text proactive message and record the attempt.

        Args:
            umo: Target unified_msg_origin.
            text: Message text.

        Returns:
            Send outcome.
        """
        if self.sender is None or not self.config.sender.enabled:
            return SendResult(ok=False, error="sender disabled")
        chain = MessageChain(chain=[Plain(text)])
        message_id = LifeSender.new_message_id()
        result = await self.sender.send_chain(umo, chain)
        if self.store is not None:
            try:
                await self.store.record_outbound(
                    session_key=umo,
                    message_id=message_id,
                    status="sent" if result.ok else "failed",
                    platform_message_id=result.platform_message_id,
                    error=result.error,
                    attempts=1,
                )
            except Exception:
                logger.exception("[life] failed to record outbound message")
        return result

    async def status(self) -> dict[str, Any]:
        """Return a serializable runtime status snapshot.

        Returns:
            Status mapping used by the dashboard API and health checks.
        """
        storage: dict[str, Any] = {}
        if self.store is not None:
            try:
                storage = await self.store.stats()
            except Exception as exc:
                storage = {"error": str(exc)}
        memory: dict[str, Any] = {"enabled": False}
        if self.memory is not None:
            try:
                memory = await self.memory.status()
            except Exception as exc:
                memory = {"enabled": True, "error": str(exc)}
        person: dict[str, Any] = {"enabled": False}
        if self.person is not None:
            try:
                person = await self.person.status()
            except Exception as exc:
                person = {"enabled": True, "error": str(exc)}
        expression: dict[str, Any] = {"enabled": False}
        if self.expression is not None:
            try:
                expression = await self.expression.status()
            except Exception as exc:
                expression = {"enabled": True, "error": str(exc)}
        jargon: dict[str, Any] = {"enabled": False}
        if self.jargon is not None:
            try:
                jargon = await self.jargon.status()
            except Exception as exc:
                jargon = {"enabled": True, "error": str(exc)}
        emoji: dict[str, Any] = {"enabled": False}
        if self.emoji is not None:
            try:
                emoji = await self.emoji.status()
            except Exception as exc:
                emoji = {"enabled": True, "error": str(exc)}
        return {
            "enabled": self._running,
            "configured": self.config.enabled,
            "data_dir": str(self.data_dir) if self.data_dir else None,
            "config_path": str(self.config_path),
            "scope": {
                "mode": self.config.scope.mode,
                "enabled_umos": len(self.config.scope.enabled_umos),
                "enabled_group_ids": len(self.config.scope.enabled_group_ids),
            },
            "queue": {
                "size": self._queue.qsize() if self._queue else 0,
                "maxsize": self.config.ingest.queue_size,
            },
            "counters": dict(self._counters),
            "storage": storage,
            "memory": memory,
            "person": person,
            "expression": expression,
            "jargon": jargon,
            "emoji": emoji,
            "sessions": len(self.registry.snapshot()),
            "started_at": self.started_at,
        }
