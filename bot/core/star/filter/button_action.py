from bot.core.config import BotConfig
from bot.core.platform.bot_message_event import BotMessageEvent

from . import HandlerFilter


class ButtonActionFilter(HandlerFilter):
    """Match portable button interactions, optionally by action ID."""

    def __init__(self, action_id: str | None = None) -> None:
        self.action_id = action_id

    def filter(self, event: BotMessageEvent, cfg: BotConfig) -> bool:
        """Return whether an event carries the requested button action.

        Args:
            event: Incoming LKMBot message event.
            cfg: Active LKMBot configuration.

        Returns:
            Whether the event contains a matching button interaction.
        """
        interaction = event.get_button_interaction()
        return interaction is not None and (
            self.action_id is None or interaction.action_id == self.action_id
        )
