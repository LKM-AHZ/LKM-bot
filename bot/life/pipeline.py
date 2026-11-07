"""Pipeline stages of the Life layer.

``LifeIngestStage`` archives every inbound message and runs first in
``STAGES_ORDER``, so it sees messages that are never woken by
``WakingCheckStage``. It must never stop event propagation.

``LifeReactionStage`` runs next: a matched keyword rule sends its canned reply
and stops the event, so the model never sees that message.

``LifeTurnStage`` runs right after it and is the only stage the Life layer may
use to hold an event: ambient group messages are merged into a turn, the
heart-flow gate decides whether the turn deserves an answer, and only the
leader of the turn continues the pipeline (with the merged text and a forced
wake). Addressed messages, commands and private chats bypass it untouched.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from bot import logger
from bot.core.message.components import At, AtAll, Reply
from bot.core.pipeline.context import PipelineContext
from bot.core.pipeline.stage import Stage, register_stage
from bot.core.platform.bot_message_event import BotMessageEvent
from bot.life.heartflow.runtime import ACTION_CONTINUE, TurnEntry, TurnRuntime
from bot.life.scenes import policy_for, reply_blocked

if TYPE_CHECKING:
    from bot.life.service import LifeService


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
class LifeReactionStage(Stage):
    """Answer known keyword triggers with a fixed reply.

    This is the only stage that may stop an inbound message without the model
    seeing it: a rule matched, the canned reply was sent, and the rest of the
    pipeline (including the planner) skips this event.
    """

    def __init__(self) -> None:
        self.service = None

    async def initialize(self, ctx: PipelineContext) -> None:
        """Resolve the Life service.

        Args:
            ctx: Pipeline context.
        """
        from bot.life.service import get_life_service

        self.service = get_life_service()

    async def process(self, event: BotMessageEvent) -> None:
        """Match a keyword rule, send its reply and stop the event.

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
        reactions = getattr(service, "reactions", None)
        if reactions is None:
            return
        text = (event.message_str or "").strip()
        if not text or text.startswith("/"):
            return
        umo = str(event.unified_msg_origin)
        group_id = event.get_group_id() or None
        if not service.config.session_enabled(umo, group_id):
            return
        if reply_blocked(service.config, umo, group_id):
            return
        try:
            if await reactions.try_react(umo, text):
                event.stop_event()
        except Exception:
            # A canned reply must never break message handling.
            logger.exception("[life] keyword reaction failed")


@register_stage
class LifeAcpStage(Stage):
    """Let an external ACP agent answer instead of the local model.

    Only active when ``life.acp.takeover`` is on. A missing answer, a failed
    send or a disabled adapter all fall through to the local pipeline, so the
    external agent can never silence a chat.
    """

    def __init__(self) -> None:
        self.service = None

    async def initialize(self, ctx: PipelineContext) -> None:
        """Resolve the Life service.

        Args:
            ctx: Pipeline context.
        """
        from bot.life.service import get_life_service

        self.service = get_life_service()

    async def process(self, event: BotMessageEvent) -> None:
        """Ask the ACP agent and stop the event when it answered.

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
        acp = getattr(service, "acp", None)
        if acp is None or not acp.enabled or not acp.config.takeover:
            return
        text = (event.message_str or "").strip()
        if not text or text.startswith("/"):
            return
        umo = str(event.unified_msg_origin)
        group_id = event.get_group_id() or None
        if not service.config.session_enabled(umo, group_id):
            return
        if reply_blocked(service.config, umo, group_id):
            return
        try:
            answer = await acp.ask(umo, text)
        except Exception:
            logger.exception("[life] ACP stage failed; falling back to the local model")
            return
        if not answer:
            return
        try:
            sent = await service.send_text(umo, answer)
        except Exception:
            logger.exception("[life] ACP reply send failed")
            return
        if getattr(sent, "ok", False):
            event.stop_event()


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
        umo = str(event.unified_msg_origin)
        group_id = event.get_group_id() or None
        if not self.is_command(event) and reply_blocked(service.config, umo, group_id):
            # Messages are archived by the ingest stage; pausing only stops the
            # answer, matching upstream's "record but do not think".
            event.stop_event()
            return
        if not self.is_ambient(event):
            return
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
        monitor = getattr(service, "monitor", None)
        if monitor is not None:
            try:
                decision = outcome.decision
                monitor.record_decision(
                    session_id=umo,
                    action=outcome.action,
                    reply=bool(decision.reply) if decision else False,
                    likelihood=decision.likelihood if decision else 0.0,
                    threshold=decision.threshold if decision else 0.0,
                    reason=decision.reason if decision else outcome.reason,
                )
            except Exception:
                logger.exception("[life] monitor decision record failed")
        if outcome.action != ACTION_CONTINUE:
            event.stop_event()
            return
        if outcome.merged_text:
            event.message_str = outcome.merged_text
        event.set_extra("_life_force_wake", True)
        if service.config.reply.supplement_enabled:
            # Plan the send order in parallel with the agent; post-processing
            # picks the finished payload up and sends the extra messages.
            try:
                task = asyncio.create_task(
                    _plan_reply_supplement(
                        service,
                        event,
                        outcome.merged_text or event.message_str or "",
                    )
                )
            except RuntimeError:
                return
            event.set_extra("_life_reply_supplement_task", task)

    def is_command(self, event: BotMessageEvent) -> bool:
        """Return whether the event is a command that bypasses pause.

        Args:
            event: Inbound message event.

        Returns:
            True for ``/`` commands and wake-prefixed messages.
        """
        text = (event.message_str or "").strip()
        if not text:
            return False
        if text.startswith("/"):
            return True
        return any(text.startswith(prefix) for prefix in self.wake_prefixes)

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


async def _plan_reply_supplement(
    service: LifeService, event: BotMessageEvent, guidance: str
) -> dict | None:
    """Resolve one reply supplement plan for an ambient turn.

    Args:
        service: Life service holding the store, provider context and stickers.
        event: Event that will carry the agent's reply.
        guidance: Merged conversation text used as sticker context.

    Returns:
        Payload for ``_life_reply_supplement``, or None when planning is off,
        unavailable or invalid.
    """
    from bot.core.provider.entities import ProviderType
    from bot.life.reply.supplement import (
        SupplementResources,
        generate_reply_supplement,
        resolve_supplement,
    )

    reply_config = service.config.reply
    store = getattr(service, "store", None)
    context = getattr(service, "star_context", None)
    if store is None or context is None:
        return None
    umo = str(event.unified_msg_origin)
    try:
        rows = await store.fetch_range(umo, limit=24)
    except Exception:
        logger.exception("[life] supplement resource capture failed")
        return None
    allow_at = reply_config.supplement_allow_at
    allow_quote = reply_config.supplement_allow_quote
    scene = policy_for(
        getattr(service.config, "scenes", None),
        umo,
        event.get_group_id() or None,
    )
    if scene is not None:
        allow_at = allow_at and scene.allow_at
        allow_quote = allow_quote and scene.allow_quote
    resources = SupplementResources.from_rows(
        rows,
        allow_at=allow_at,
        allow_quote=allow_quote,
        session_key=umo,
    )
    if not resources.messages:
        return None
    manager = getattr(context, "provider_manager", None)
    provider = None
    if manager is not None:
        try:
            provider = await manager.get_using_provider_async(
                ProviderType.CHAT_COMPLETION
            )
        except Exception:
            provider = None
    plan = await generate_reply_supplement(
        provider,
        resources,
        guidance=guidance,
        timeout_seconds=reply_config.supplement_timeout_seconds,
        max_parts=reply_config.supplement_max_parts,
        retry_once=reply_config.supplement_retry_once,
    )
    if plan is None:
        return None
    emoji = getattr(service, "emoji", None)
    pick_emoji = None
    if emoji is not None:
        pick_emoji = (
            emoji.pick_semantic
            if getattr(emoji.config, "semantic_enabled", False)
            else emoji.pick
        )
    try:
        resolved = await resolve_supplement(
            plan,
            resources,
            pick_emoji=pick_emoji,
            emoji_context=guidance,
            max_parts=reply_config.supplement_max_parts,
        )
    except ValueError:
        logger.warning("[life] reply supplement failed validation")
        return None
    return resolved.to_extra()
