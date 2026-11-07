"""Pipeline stages of the Life layer.

``LifeIngestStage`` archives every inbound message and runs first in
``STAGES_ORDER``, so it sees messages that are never woken by
``WakingCheckStage``. It must never stop event propagation.

``LifeTurnStage`` runs right after it and is the only stage the Life layer may
use to hold an event: ambient group messages are merged into a turn, the
heart-flow gate decides whether the turn deserves an answer, and only the
leader of the turn continues the pipeline (with the merged text and a forced
wake). Addressed messages, commands and private chats bypass it untouched.
"""

from __future__ import annotations

import time

from bot import logger
from bot.core.message.components import At, AtAll, Reply
from bot.core.pipeline.context import PipelineContext
from bot.core.pipeline.stage import Stage, register_stage
from bot.core.platform.bot_message_event import BotMessageEvent
from bot.life.heartflow.runtime import ACTION_CONTINUE, TurnEntry, TurnRuntime


@register_stage
class LifeIngestStage(Stage):
    """Pass-through stage that feeds the Life archive."""

    def __init__(self) -> None:
        self.service = None

    async def initialize(self, ctx: PipelineContext) -> None:
        """Resolve the Life service once the pipeline context exists.

        Args:
            ctx: Pipeline context.
        """
        from bot.life.service import get_life_service

        self.service = get_life_service()

    async def process(self, event: BotMessageEvent) -> None:
        """Archive the event and continue the pipeline.

        Args:
            event: Inbound message event.
        """
        service = self.service
        if service is None:
            from bot.life.service import get_life_service

            service = get_life_service()
            self.service = service
        if service is None:
            return
        try:
            await service.ingest(event)
        except Exception:
            # Archiving must never break message handling.
            from bot import logger

            logger.exception("[life] ingest stage failed")


@register_stage
class LifeTurnStage(Stage):
    """Hold ambient group messages for the heart-flow decision."""

    def __init__(self) -> None:
        self.service = None
        self.wake_prefixes: list[str] = []

    async def initialize(self, ctx: PipelineContext) -> None:
        """Resolve the Life service and the configured wake prefixes.

        Args:
            ctx: Pipeline context.
        """
        from bot.life.service import get_life_service

        self.service = get_life_service()
        prefixes = ctx.bot_config.get("wake_prefix") or []
        self.wake_prefixes = [str(prefix) for prefix in prefixes if str(prefix)]

    async def process(self, event: BotMessageEvent) -> None:
        """Decide whether the event is held, dropped or passed through.

        Args:
            event: Inbound message event.
        """
        service = self.service
        if service is None:
            from bot.life.service import get_life_service

            service = get_life_service()
            self.service = service
        if service is None:
            return
        runtime = getattr(service, "heartflow", None)
        if not isinstance(runtime, TurnRuntime):
            return
        if not self.is_ambient(event):
            return
        umo = str(event.unified_msg_origin)
        if not service.config.session_enabled(umo, event.get_group_id() or None):
            return
        entry = TurnEntry(
            session_key=umo,
            text=event.message_str or "",
            ts=float(event.message_obj.timestamp or time.time()),
            sender_id=event.get_sender_id() or None,
            sender_name=event.get_sender_name() or None,
            message_id=str(getattr(event.message_obj, "message_id", "") or ""),
        )
        try:
            outcome = await runtime.handle(umo, entry)
        except Exception:
            logger.exception(
                "[life] heart-flow turn failed; passing the message through"
            )
            return
        if outcome.action != ACTION_CONTINUE:
            event.stop_event()
            return
        if outcome.merged_text:
            event.message_str = outcome.merged_text
        event.set_extra("_life_force_wake", True)

    def is_ambient(self, event: BotMessageEvent) -> bool:
        """Return whether the event may be merged into an ambient turn.

        Only plain group chatter qualifies: a private chat, a reply to the bot,
        an ``@``, a wake-prefixed command or a button interaction must reach the
        upstream pipeline unchanged.

        Args:
            event: Inbound message event.

        Returns:
            True when the heart-flow may hold the event.
        """
        try:
            if event.is_private_chat() or event.is_button_interaction():
                return False
        except Exception:
            return False
        text = (event.message_str or "").strip()
        if not text:
            return False
        if text.startswith("/"):
            return False
        for prefix in self.wake_prefixes:
            if text.startswith(prefix):
                return False
        for component in event.get_messages():
            if isinstance(component, At | AtAll | Reply):
                return False
        return not event.get_extra("_life_force_wake")
