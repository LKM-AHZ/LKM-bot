"""本地 Agent 模式的 LKMBot 插件调用 Stage"""

import traceback
from collections.abc import AsyncGenerator
from typing import Any

from bot.core import logger
from bot.core.message.message_event_result import MessageEventResult
from bot.core.platform.bot_message_event import BotMessageEvent
from bot.core.star.filter.command import CommandFilter
from bot.core.star.filter.command_group import CommandGroupFilter
from bot.core.star.filter.regex import RegexFilter
from bot.core.star.star import star_map
from bot.core.star.star_handler import EventType, StarHandlerMetadata

from ...context import PipelineContext, call_event_hook, call_handler
from ...stage import Stage


class StarRequestSubStage(Stage):
    async def initialize(self, ctx: PipelineContext) -> None:
        self.prompt_prefix = ctx.bot_config["provider_settings"]["prompt_prefix"]
        self.identifier = ctx.bot_config["provider_settings"]["identifier"]
        self.ctx = ctx

    async def process(  # ty: ignore[invalid-method-override]  # 基类声明为协程、子类用 yield 实现为异步生成器（同一层级两种调用形态），ty 的 LSP 检查无法表达该联合契约
        self,
        event: BotMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        activated_handlers: list[StarHandlerMetadata] = event.get_extra(
            "activated_handlers",
        )
        handlers_parsed_params: dict[str, dict[str, Any]] = event.get_extra(
            "handlers_parsed_params",
        )
        if not handlers_parsed_params:
            handlers_parsed_params = {}

        for handler in activated_handlers:
            if event.is_stopped():
                break
            params = handlers_parsed_params.get(handler.handler_full_name, {})
            md = star_map.get(handler.handler_module_path)
            if not md:
                logger.warning(
                    f"Cannot find plugin for given handler module path: {handler.handler_module_path}",
                )
                continue
            logger.debug(f"plugin -> {md.name} - {handler.handler_name}")
            # Match the component categories used by the plugin detail page.
            handler_category = (
                "command"
                if any(
                    isinstance(
                        event_filter, CommandFilter | CommandGroupFilter | RegexFilter
                    )
                    for event_filter in handler.event_filters
                )
                else "listener"
            )
            event.trace.record(
                "handler_start",
                kind="plugin_handler",
                status="running",
                plugin=md.name or handler.handler_module_path,
                plugin_display_name=md.display_name or md.name,
                handler=handler.handler_name,
                handler_full_name=handler.handler_full_name,
                handler_type=handler.event_type.name,
                handler_category=handler_category,
                priority=handler.extras_configs.get("priority", 0),
            )
            try:
                wrapper = call_handler(event, handler.handler, **params)
                async for ret in wrapper:
                    yield ret
                event.trace.record(
                    "handler_end",
                    kind="plugin_handler",
                    status="success",
                    plugin=md.name or handler.handler_module_path,
                    plugin_display_name=md.display_name or md.name,
                    handler=handler.handler_name,
                    handler_full_name=handler.handler_full_name,
                    handler_type=handler.event_type.name,
                    handler_category=handler_category,
                )
                if event.is_stopped():
                    break
                event.clear_result()  # 清除上一个 handler 的结果
            except Exception as e:
                traceback_text = traceback.format_exc()
                plugin_name = md.name or handler.handler_module_path
                # Keep handler failures on the plugin's side of the log filter.
                logger.error(
                    traceback_text,
                    extra={"plugin_name": plugin_name},
                )
                logger.error(
                    f"Star {handler.handler_full_name} handle error: {e}",
                    extra={"plugin_name": plugin_name},
                )
                event.trace.record(
                    "handler_end",
                    kind="plugin_handler",
                    status="error",
                    plugin=md.name or handler.handler_module_path,
                    plugin_display_name=md.display_name or md.name,
                    handler=handler.handler_name,
                    handler_full_name=handler.handler_full_name,
                    handler_type=handler.event_type.name,
                    handler_category=handler_category,
                    error=str(e),
                )

                await call_event_hook(
                    event,
                    EventType.OnPluginErrorEvent,
                    md.name,
                    handler.handler_name,
                    e,
                    traceback_text,
                )

                if not event.is_stopped() and event.is_at_or_wake_command:
                    ret = f":(\n\n在调用插件 {md.name} 的处理函数 {handler.handler_name} 时出现异常：{e}"
                    event.set_result(MessageEventResult().message(ret))
                    yield
                    event.clear_result()

                event.stop_event()
