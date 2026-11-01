from typing import Any

from mcp.types import CallToolResult

from bot.core.agent.hooks import BaseAgentRunHooks
from bot.core.agent.run_context import ContextWrapper
from bot.core.agent.tool import FunctionTool
from bot.core.bot_agent_context import BotAgentContext
from bot.core.pipeline.context_utils import call_event_hook
from bot.core.star.star_handler import EventType


class MainAgentHooks(BaseAgentRunHooks[BotAgentContext]):
    async def on_agent_begin(
        self, run_context: ContextWrapper[BotAgentContext]
    ) -> None:
        await call_event_hook(
            run_context.context.event,
            EventType.OnAgentBeginEvent,
            run_context,
        )

    async def on_agent_done(self, run_context, llm_response) -> None:
        # 执行事件钩子
        if llm_response and llm_response.reasoning_content:
            # we will use this in result_decorate stage to inject reasoning content to chain
            run_context.context.event.set_extra(
                "_llm_reasoning_content", llm_response.reasoning_content
            )

        await call_event_hook(
            run_context.context.event,
            EventType.OnLLMResponseEvent,
            llm_response,
        )
        await call_event_hook(
            run_context.context.event,
            EventType.OnAgentDoneEvent,
            run_context,
            llm_response,
        )

    async def on_tool_start(
        self,
        run_context: ContextWrapper[BotAgentContext],
        tool: FunctionTool[Any],
        tool_args: dict | None,
    ) -> None:
        await call_event_hook(
            run_context.context.event,
            EventType.OnUsingLLMToolEvent,
            tool,
            tool_args,
        )

    async def on_tool_end(
        self,
        run_context: ContextWrapper[BotAgentContext],
        tool: FunctionTool[Any],
        tool_args: dict | None,
        tool_result: CallToolResult | None,
    ) -> None:
        run_context.context.event.clear_result()
        await call_event_hook(
            run_context.context.event,
            EventType.OnLLMToolRespondEvent,
            tool,
            tool_args,
            tool_result,
        )


class EmptyAgentHooks(BaseAgentRunHooks[BotAgentContext]):
    pass


MAIN_AGENT_HOOKS = MainAgentHooks()
