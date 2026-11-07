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
from bot.core.provider.entities import ProviderType
from bot.life.acp.service import AcpService
from bot.life.acp.tools import register_acp_tools, unregister_acp_tools
from bot.life.config import (
    LifeConfig,
    default_config_path,
    load_life_config,
    save_life_config,
)
from bot.life.emoji.service import EmojiService
from bot.life.emoji.tools import register_emoji_tools, unregister_emoji_tools
from bot.life.expression.service import ExpressionService
from bot.life.heartflow.attention import AttentionRegistry
from bot.life.heartflow.focus.service import FocusService
from bot.life.heartflow.proactive import ProactiveService
from bot.life.heartflow.runtime import TurnRuntime
from bot.life.ingest import IngestedMessage, normalize_event
from bot.life.jargon.service import JargonService
from bot.life.memory.adapter import MemoryAdapter
from bot.life.memory.tools import register_memory_tools, unregister_memory_tools
from bot.life.monitor.recorder import PlannerMonitor
from bot.life.person.service import PersonService
from bot.life.persona.service import PersonaService
from bot.life.prompt import PromptAssembler
from bot.life.reaction.service import ReactionService
from bot.life.reply.postprocess import ReplyPostProcessor
from bot.life.sender import LifeSender, SendResult
from bot.life.session.registry import SessionRegistry
from bot.life.store import LifeStore
from bot.life.subagent.inbox import SubAgentInbox, notice_from_task
from bot.life.subagent.notify import SubAgentNotifier
from bot.life.subagent.runner import run_subagent_task
from bot.life.subagent.tasks import SubAgentTaskManager
from bot.life.subagent.tools import (
    register_subagent_tools,
    unregister_subagent_tools,
)
from bot.life.toolresult.condense import run_condense, should_condense
from bot.life.toolresult.read import (
    register_tool_result_tools,
    unregister_tool_result_tools,
)
from bot.life.toolresult.store import ToolResultStore

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
        self.persona: PersonaService | None = None
        self.prompt: PromptAssembler | None = None
        self.reply: ReplyPostProcessor | None = None
        self.expression: ExpressionService | None = None
        self.jargon: JargonService | None = None
        self.emoji: EmojiService | None = None
        self.focus: FocusService | None = None
        self.heartflow: TurnRuntime | None = None
        self.attention: AttentionRegistry | None = None
        self.proactive: ProactiveService | None = None
        self.tool_results: ToolResultStore | None = None
        self.subagents: SubAgentTaskManager | None = None
        self.subagent_inbox: SubAgentInbox | None = None
        self.subagent: SubAgentNotifier | None = None
        self.monitor: PlannerMonitor | None = None
        self.reactions: ReactionService | None = None
        self.acp: AcpService | None = None
        self.registry = SessionRegistry()
        self.started_at = 0
        self._queue: asyncio.Queue[IngestedMessage | None] | None = None
        self._worker: asyncio.Task | None = None
        self._maintenance: asyncio.Task | None = None
        self._memory_task: asyncio.Task | None = None
        self._expression_task: asyncio.Task | None = None
        self._jargon_task: asyncio.Task | None = None
        self._emoji_task: asyncio.Task | None = None
        self._focus_task: asyncio.Task | None = None
        self._proactive_task: asyncio.Task | None = None
        self._running = False
        self._counters = {
            "ingested": 0,
            "written": 0,
            "duplicates": 0,
            "dropped": 0,
            "failed": 0,
            "normalize_failed": 0,
        }
        self._tool_result_counts = {
            "attempts": 0,
            "condensed": 0,
            "skipped": 0,
            "failed": 0,
            "chars_saved": 0,
        }
        self._subagent_counts = {
            "started": 0,
            "completed": 0,
            "failed": 0,
            "stopped": 0,
            "woken": 0,
            "inboxed": 0,
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
        self.prompt = PromptAssembler(self.config.prompt)
        if self.config.tool_result.enabled:
            self.tool_results = ToolResultStore(
                max_results_per_session=self.config.tool_result.keep_per_session,
                max_sessions=self.config.tool_result.max_sessions,
                max_chars_per_session=self.config.tool_result.max_chars_per_session,
            )
        if self.config.subagent.enabled:
            self.subagents = SubAgentTaskManager(
                max_running=self.config.subagent.max_running,
                max_running_per_session=self.config.subagent.max_running_per_session,
                finished_keep=self.config.subagent.finished_keep,
                timeout_seconds=self.config.subagent.timeout_seconds,
            )
            self.subagent_inbox = SubAgentInbox(
                max_per_session=self.config.subagent.inbox_max_per_session,
                max_age_minutes=self.config.subagent.notice_max_age_minutes,
            )
            self.subagent = SubAgentNotifier(self.config.subagent, self.subagent_inbox)
        if self.config.monitor.enabled:
            self.monitor = PlannerMonitor(self.config.monitor)
        if self.config.reaction.enabled:
            self.reactions = ReactionService(self.config.reaction, self.send_text)
        if self.config.acp.enabled:
            self.acp = AcpService(self.config.acp, self.data_dir)
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

        if self.config.persona.enabled:
            self.persona = PersonaService(
                config=self.config.persona,
                db_helper=self.db,
                life_config=self.config,
            )
            await self.persona.initialize()

        if self.config.reply.enabled:
            self.reply = ReplyPostProcessor(
                self.config.reply,
                life_config=self.config,
                persona=self.persona,
            )

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

        if self.config.heartflow.enabled and self.config.heartflow.runtime.enabled:
            self.heartflow = TurnRuntime(
                self.config.heartflow,
                on_reply=self._note_heartflow_reply,
            )
            logger.info(
                "[life] heart-flow runtime ready; shadow=%s bot_names=%s",
                self.config.heartflow.runtime.shadow,
                self.config.heartflow.bot_names,
            )

        if self.config.heartflow.enabled and self.config.heartflow.focus.enabled:
            self.focus = FocusService(
                db_helper=self.db,
                config=self.config.heartflow.focus,
                life_config=self.config,
            )
            await self.focus.initialize()

        if self.config.heartflow.enabled and self.config.heartflow.attention.enabled:
            self.attention = AttentionRegistry(self.config.heartflow.attention)

        if (
            self.config.heartflow.enabled
            and self.config.heartflow.idle.enabled
            and self.sender is not None
        ):
            self.proactive = ProactiveService(
                config=self.config.heartflow.idle,
                db_helper=self.db,
                archive=self.store,
                send=self.send_text,
                life_config=self.config,
                summarizer=self._llm_summarizer,
                focus=self.focus,
            )
            await self.proactive.initialize()

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
            self.memory is not None
            or self.emoji is not None
            or self.tool_results is not None
            or self.subagents is not None
            or self.acp is not None
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
                if self.tool_results is not None:
                    register_tool_result_tools(manager, self)
                if self.subagents is not None:
                    register_subagent_tools(manager, self)
                if self.acp is not None:
                    register_acp_tools(manager, self)

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
        if self.focus is not None:
            self._focus_task = asyncio.create_task(
                self._focus_loop(),
                name="life_focus",
            )
        if self.proactive is not None:
            self._proactive_task = asyncio.create_task(
                self._proactive_loop(),
                name="life_proactive",
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
        if self._focus_task is not None:
            self._focus_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._focus_task
        if self.heartflow is not None:
            await self.heartflow.close()
        if self._proactive_task is not None:
            self._proactive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._proactive_task
        if self.proactive is not None:
            await self.proactive.close()
        if self.focus is not None:
            await self.focus.close()
        if self.expression is not None:
            await self.expression.close()
        if self.jargon is not None:
            await self.jargon.close()
        if self.emoji is not None:
            await self.emoji.close()
        if self.person is not None:
            await self.person.close()
        if self.persona is not None:
            await self.persona.close()
        if self.star_context is not None and (
            self.memory is not None
            or self.emoji is not None
            or self.tool_results is not None
            or self.subagents is not None
            or self.acp is not None
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
                if self.tool_results is not None:
                    unregister_tool_result_tools(manager)
                if self.subagents is not None:
                    unregister_subagent_tools(manager)
                if self.acp is not None:
                    unregister_acp_tools(manager)
        if self.memory is not None:
            await self.memory.close()
        if self.subagents is not None:
            await self.subagents.cancel_all()
        if self.acp is not None:
            await self.acp.close()
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
        if self.focus is not None:
            try:
                await self.focus.observe_messages(batch)
            except Exception:
                logger.exception("[life] focus observe failed")
        if self.proactive is not None:
            try:
                await self.proactive.observe_messages(batch)
            except Exception:
                logger.exception("[life] proactive observe failed")

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
            if self.focus is not None:
                try:
                    await self.focus.compress_round()
                except Exception:
                    logger.exception("[life] focus compression failed")

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

    async def _proactive_loop(self) -> None:
        """Speak first only when the idle rules allow it."""
        if self.proactive is None:
            return
        interval = max(self.config.heartflow.idle.scan_interval_minutes, 1) * 60
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                return
            try:
                await self.proactive.run_round()
            except Exception:
                logger.exception("[life] proactive round failed")

    async def _focus_loop(self) -> None:
        """Checkpoint dirty timelines on a fixed interval."""
        if self.focus is None:
            return
        interval = max(self.config.heartflow.focus.checkpoint_interval_minutes, 1) * 60
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                return
            try:
                await self.focus.checkpoint_all()
            except Exception:
                logger.exception("[life] focus checkpoint failed")

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

    async def _note_heartflow_reply(
        self,
        session_key: str,
        text: str,
        now: float,
    ) -> None:
        """Update attention and talk value after an answered turn.

        Args:
            session_key: Session the bot answered in.
            text: Text of the merged turn.
            now: Wall-clock timestamp of the answer.
        """
        if self.attention is not None:
            self.attention.observe(session_key, text, now)
        if self.proactive is not None:
            self.proactive.note_engagement(session_key)

    async def condense_tool_result(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any] | None,
        content: str,
        intent: str,
        session_id: str,
    ) -> str | None:
        """Condense one oversized tool result before it enters the history.

        Args:
            tool_name: Tool that produced the result.
            tool_args: Arguments the model passed to the tool.
            content: Raw result text.
            intent: The model's own reasoning before the call.
            session_id: Unified message origin of the session.

        Returns:
            The text to write into the history, or None when the caller should
            keep the upstream behaviour (disabled, out of scope, short result,
            or a failed utility call).
        """
        config = self.config.tool_result
        if not config.enabled or self.tool_results is None:
            return None
        if not session_id:
            return None
        group_id = None
        parts = session_id.split(":")
        if len(parts) >= 3 and parts[-2] == "GroupMessage":
            group_id = parts[-1]
        if not self.config.session_enabled(session_id, group_id):
            return None
        if not should_condense(
            tool_name,
            content,
            enabled=True,
            threshold_chars=config.threshold_chars,
            excluded_tool_names=config.excluded_tool_names,
        ):
            return None

        self._tool_result_counts["attempts"] += 1
        outcome = await run_condense(
            tool_name=tool_name,
            tool_args=tool_args,
            content=content,
            intent=intent,
            llm=self._llm_summarizer,
            store=self.tool_results,
            session_id=session_id,
            max_input_chars=config.max_input_chars,
            intent_max_chars=config.intent_max_chars,
            timeout_seconds=config.llm_timeout_seconds,
        )
        if outcome.condensed:
            self._tool_result_counts["condensed"] += 1
            self._tool_result_counts["chars_saved"] += (
                outcome.original_chars - outcome.condensed_chars
            )
            return outcome.content
        if outcome.error in {"timeout", "exception"}:
            self._tool_result_counts["failed"] += 1
        else:
            self._tool_result_counts["skipped"] += 1
        return None

    async def chat_provider(self) -> Any:
        """Return the active chat provider, if any.

        Returns:
            The provider used for chat completions, or None.
        """
        manager = getattr(self.star_context, "provider_manager", None)
        if manager is None:
            return None
        return await manager.get_using_provider_async(ProviderType.CHAT_COMPLETION)

    def subagent_tools(self, requested: str) -> list[Any]:
        """Resolve the tool subset one sub-agent may use.

        Args:
            requested: Comma-separated tool names from the model; empty means
                every tool this deployment allows.

        Returns:
            Granted tools, capped by ``max_granted_tools``.
        """
        config = self.config.subagent
        allowed = set(config.allowed_tools)
        if not allowed:
            return []
        names = [name.strip() for name in requested.split(",") if name.strip()]
        manager = getattr(
            getattr(self.star_context, "provider_manager", None),
            "llm_tools",
            None,
        )
        if manager is None:
            return []
        granted: list[Any] = []
        for name in (names or list(config.allowed_tools))[: config.max_granted_tools]:
            if name not in allowed:
                continue
            tool = manager.get_func(name)
            if tool is None or not getattr(tool, "active", True):
                continue
            granted.append(tool)
        return granted

    async def start_subagent(
        self,
        *,
        event: Any,
        description: str,
        requested_tools: str = "",
    ) -> str:
        """Register and launch one background sub-agent task.

        Args:
            event: Bot message event that asked for the task.
            description: Self-contained task text.
            requested_tools: Comma-separated tool names from the model.

        Returns:
            A status line for the tool result, or an ``error:`` string.
        """
        if not self.config.subagent.enabled or self.subagents is None:
            return "error: sub-agent is not enabled"
        umo = getattr(event, "unified_msg_origin", "") or ""
        text = (description or "").strip()
        if not umo:
            return "error: session is unknown"
        if not text:
            return "error: description is empty"
        if len(text) > 2000:
            text = text[:2000]
        tools = self.subagent_tools(requested_tools)
        task = self.subagents.start(
            session_id=umo,
            description=text,
            granted_tools=[tool.name for tool in tools],
            runner=lambda task: self._run_subagent(task, event, tools),
            on_finished=self.note_subagent_finished,
        )
        if task is None:
            return "error: too many sub-agent tasks are running; wait or stop one first"
        self._subagent_counts["started"] += 1
        return (
            f"started: {task.task_id} (background). "
            f'Use subagent_status(task_id="{task.task_id}") to check it; '
            "the conclusion is delivered into the chat when it finishes. "
            "Keep working on the current turn meanwhile."
        )

    async def _run_subagent(
        self,
        task: Any,
        event: Any,
        tools: list[Any],
    ) -> Any:
        """Run one registered sub-agent task.

        Args:
            task: Registered task.
            event: Parent message event handed to the granted tools.
            tools: Granted tools.

        Returns:
            The run outcome.
        """
        provider = await self.chat_provider()
        return await run_subagent_task(
            provider=provider,
            event=event,
            session_id=task.session_id,
            description=task.description,
            tools=tools,
            max_turns=self.config.subagent.max_turns,
            tool_timeout_seconds=self.config.subagent.tool_timeout_seconds,
        )

    def note_subagent_finished(self, task: Any) -> None:
        """Deliver a finished task's conclusion into its session.

        The conclusion is appended to a run that is still active (a real wake);
        otherwise it lands in the inbox and the next prompt shows it.

        Args:
            task: Task that reached a terminal state.
        """
        if task.status == "completed":
            self._subagent_counts["completed"] += 1
        elif task.status == "failed":
            self._subagent_counts["failed"] += 1
        else:
            self._subagent_counts["stopped"] += 1
        if task.status != "completed" or self.subagent_inbox is None:
            return
        notice = notice_from_task(task, self.config.subagent.conclusion_max_chars)
        if not notice.conclusion:
            return
        try:
            from bot.core.pipeline.process_stage.follow_up import get_active_runner

            runner = get_active_runner(task.session_id)
        except Exception:
            runner = None
        if runner is not None:
            ticket = runner.follow_up(
                message_text=f"[{task.task_id} 子任务完成]\n{notice.conclusion}"
            )
            if ticket is not None:
                self._subagent_counts["woken"] += 1
                asyncio.create_task(
                    self._inbox_when_unconsumed(ticket, notice),
                    name=f"life_{task.task_id}_delivery",
                )
                return
        self.subagent_inbox.add(notice)
        self._subagent_counts["inboxed"] += 1

    async def _inbox_when_unconsumed(self, ticket: Any, notice: Any) -> None:
        """Keep a conclusion in the inbox when the active run never saw it.

        Args:
            ticket: Follow-up ticket handed to the active runner.
            notice: Conclusion to fall back to.
        """
        await ticket.resolved.wait()
        if not ticket.consumed and self.subagent_inbox is not None:
            self.subagent_inbox.add(notice)
            self._subagent_counts["inboxed"] += 1

    async def _llm_summarizer(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str | None:
        """Run one chat completion through the active provider.

        Args:
            system_prompt: Instructions for the model.
            user_prompt: Payload of the call.

        Returns:
            Completion text, or None when no provider is available.
        """
        provider = await self.chat_provider()
        if provider is None:
            return None
        response = await provider.text_chat(
            prompt=user_prompt,
            system_prompt=system_prompt,
        )
        return (response.completion_text or "").strip() or None

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
        if result.ok and self.focus is not None:
            try:
                await self.focus.note_reply(umo, text)
            except Exception:
                logger.exception("[life] focus reply record failed")
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
        persona: dict[str, Any] = {"enabled": False}
        if self.persona is not None:
            try:
                persona = await self.persona.status()
            except Exception as exc:
                persona = {"enabled": True, "error": str(exc)}
        prompt: dict[str, Any] = {"enabled": False}
        if self.prompt is not None:
            try:
                prompt = self.prompt.status()
            except Exception as exc:
                prompt = {"enabled": True, "error": str(exc)}
        reply: dict[str, Any] = {"enabled": False}
        if self.reply is not None:
            reply = self.reply.status()
        monitor: dict[str, Any] = {"enabled": False}
        if self.monitor is not None:
            monitor = self.monitor.status()
        reaction: dict[str, Any] = {"enabled": False}
        if self.reactions is not None:
            reaction = self.reactions.status()
        acp: dict[str, Any] = {"enabled": False}
        if self.acp is not None:
            acp = self.acp.status()
        subagent: dict[str, Any] = {"enabled": False}
        if self.subagents is not None:
            subagent = {
                "enabled": True,
                "counters": dict(self._subagent_counts),
                "tasks": self.subagents.stats(),
            }
            if self.subagent is not None:
                subagent["delivery"] = self.subagent.status()
        tool_result: dict[str, Any] = {"enabled": False}
        if self.tool_results is not None:
            tool_result = {
                "enabled": True,
                "threshold_chars": self.config.tool_result.threshold_chars,
                "counters": dict(self._tool_result_counts),
                "store": self.tool_results.stats(),
            }
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
        heartflow: dict[str, Any] = {"enabled": False}
        if self.heartflow is not None:
            try:
                heartflow = self.heartflow.status()
            except Exception as exc:
                heartflow = {"enabled": True, "error": str(exc)}
        attention: dict[str, Any] = {"enabled": False}
        if self.attention is not None:
            try:
                attention = self.attention.status()
            except Exception as exc:
                attention = {"enabled": True, "error": str(exc)}
        proactive: dict[str, Any] = {"enabled": False}
        if self.proactive is not None:
            try:
                proactive = await self.proactive.status()
            except Exception as exc:
                proactive = {"enabled": True, "error": str(exc)}
        focus: dict[str, Any] = {"enabled": False}
        if self.focus is not None:
            try:
                focus = await self.focus.status()
            except Exception as exc:
                focus = {"enabled": True, "error": str(exc)}
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
            "persona": persona,
            "prompt": prompt,
            "reply": reply,
            "tool_result": tool_result,
            "subagent": subagent,
            "monitor": monitor,
            "reaction": reaction,
            "acp": acp,
            "expression": expression,
            "jargon": jargon,
            "emoji": emoji,
            "focus": focus,
            "heartflow": heartflow,
            "attention": attention,
            "proactive": proactive,
            "sessions": len(self.registry.snapshot()),
            "started_at": self.started_at,
        }
