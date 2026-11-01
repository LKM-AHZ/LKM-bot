from pydantic import Field
from pydantic.dataclasses import dataclass

from bot.core.agent.run_context import ContextWrapper
from bot.core.platform.bot_message_event import BotMessageEvent
from bot.core.star.context import Context


@dataclass
class BotAgentContext:
    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Context
    """The star context instance"""
    event: BotMessageEvent
    """The message event associated with the agent context."""
    extra: dict[str, str] = Field(default_factory=dict)
    """Customized extra data."""


AgentContextWrapper = ContextWrapper[BotAgentContext]
