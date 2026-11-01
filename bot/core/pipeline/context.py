from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot.core.config import BotConfig

from .context_utils import call_event_hook, call_handler

if TYPE_CHECKING:
    from bot.core.db import BaseDatabase
    from bot.core.star import PluginManager


@dataclass
class PipelineContext:
    """上下文对象，包含管道执行所需的上下文信息"""

    bot_config: BotConfig  # LKMBot 配置对象
    plugin_manager: PluginManager  # 插件管理器对象
    bot_config_id: str
    db_helper: BaseDatabase | None = None
    call_handler = call_handler
    call_event_hook = call_event_hook
