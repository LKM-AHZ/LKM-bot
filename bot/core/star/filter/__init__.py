import abc

from bot.core.config import BotConfig
from bot.core.platform.bot_message_event import BotMessageEvent
from bot.core.platform.message_type import MessageType


class HandlerFilter(abc.ABC):
    @abc.abstractmethod
    def filter(self, event: BotMessageEvent, cfg: BotConfig) -> bool:
        """是否应当被过滤"""
        raise NotImplementedError


__all__ = ["BotConfig", "BotMessageEvent", "HandlerFilter", "MessageType"]
