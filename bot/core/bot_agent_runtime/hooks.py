from typing import TYPE_CHECKING, Any

from mcp.types import CallToolResult

from bot.core.agent.hooks import BaseAgentRunHooks
from bot.core.agent.run_context import ContextWrapper
from bot.core.agent.tool import FunctionTool
from bot.core.bot_agent_runtime.context import BotAgentContext
from bot.core.bot_agent_runtime.history import ConversationHistoryObserver
from bot.core.pipeline.context_utils import call_event_hook
from bot.core.star.star_handler import EventType

if TYPE_CHECKING:
    from bot.core.provider.entities import ProviderRequest


class MainAgentHooks(BaseAgentRunHooks[BotAgentContext]):
    def __init__(
        self,
        request: "ProviderRequest | None" = None,
        *,
        context_observer: ConversationHistoryObserver | None = None,
    ) -> None:
        """Bind hooks to the prepared native request when one is available.

        Args:
            request: Native request with conversation and tool settings.
            context_observer: Host observer for plugin context mutations.
        """
        self._request = request
        self._context_observer = context_observer

    async def on_agent_begin(
        self, run_context: ContextWrapper[BotAgentContext]
    ) -> None:
        await call_event_hook(
            run_context.context.event,
            EventType.OnAgentBeginEvent,
            run_context,
            context_observer=self._context_observer,
        )

    async def on_llm_response(self, run_context, llm_response) -> None:
        if llm_response.reasoning_content:
            # we will use this in result_decorate stage to inject reasoning content to chain
            run_context.context.event.set_extra(
                "_llm_reasoning_content", llm_response.reasoning_content
            )

        await call_event_hook(
            run_context.context.event,
            EventType.OnLLMResponseEvent,
            llm_response,
            context_observer=self._context_observer,
        )

    async def on_agent_done(self, run_context, llm_response) -> None:
        await call_event_hook(
            run_context.context.event,
            EventType.OnAgentDoneEvent,
            run_context,
            llm_response,
            context_observer=self._context_observer,
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
            context_observer=self._context_observer,
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
            context_observer=self._context_observer,
        )


class EmptyAgentHooks(BaseAgentRunHooks[BotAgentContext]):
    pass
