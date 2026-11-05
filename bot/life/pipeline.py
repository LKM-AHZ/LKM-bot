"""Pipeline stage that archives every inbound message for the Life layer.

The stage runs first in ``STAGES_ORDER`` so it sees messages that are never
woken by ``WakingCheckStage``. It must never stop event propagation.
"""

from __future__ import annotations

from bot.core.pipeline.context import PipelineContext
from bot.core.pipeline.stage import Stage, register_stage
from bot.core.platform.bot_message_event import BotMessageEvent


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
