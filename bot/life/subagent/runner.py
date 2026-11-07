"""Run one background sub-agent.

The sub-agent never sees the parent conversation: it gets a self-contained task
text, a system prompt that forbids asking the user anything, and an optional
tool subset. It runs a bounded tool loop and returns a conclusion, which the
caller delivers back into the session.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import mcp

from bot import logger
from bot.core.agent.hooks import BaseAgentRunHooks
from bot.core.agent.run_context import ContextWrapper
from bot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from bot.core.agent.tool import FunctionTool, ToolSet
from bot.core.agent.tool_executor import BaseFunctionToolExecutor
from bot.core.provider.entities import ProviderRequest
from bot.life.subagent.models import SubAgentOutcome

SUBAGENT_SYSTEM_PROMPT = (
    "你是一个后台子代理，正在为主对话里的另一个模型完成一件独立的事。\n"
    "要求：\n"
    "1. 你看不到聊天记录，也不能向用户提问；任务里没给的信息就当没有。\n"
    "2. 能自己查清的就用给你的工具查，查不到就直说查不到，不要编造。\n"
    "3. 最后只输出一份结论：先把结果写清楚，再写关键依据（文件名、链接、id、数值）。\n"
    "4. 不要寒暄，不要解释你做了什么，不要对用户说话。"
)


class _SubAgentExecutor(BaseFunctionToolExecutor):
    """Minimal tool executor for the sub-agent.

    It deliberately skips the user-facing side effects of the main executor
    (history persistence, background-task registration, message sending):
    a sub-agent may only call the tools it was granted, and their text result is
    what it sees. Per-run state (usage counter, timeout) travels on the run
    context so the executor itself stays stateless.
    """

    @classmethod
    async def execute(cls, tool: FunctionTool, run_context: Any, **tool_args):
        """Run one tool call and wrap the result for the runner.

        Args:
            tool: Granted tool.
            run_context: Run context carrying the parent event and run state.
            **tool_args: Arguments produced by the model.

        Yields:
            One ``CallToolResult`` with the tool output as text.
        """
        context = getattr(run_context, "context", None)
        usage = getattr(context, "subagent_usage", None)
        if usage is not None:
            usage[tool.name] = usage.get(tool.name, 0) + 1
        timeout_seconds = max(
            1.0, float(getattr(context, "subagent_tool_timeout", 30.0))
        )
        event = getattr(context, "event", None)
        try:
            text = await asyncio.wait_for(
                cls._invoke(tool, run_context, event, tool_args),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            text = f"error: tool {tool.name} timed out after {timeout_seconds:.0f}s"
        except Exception as exc:
            text = f"error: {type(exc).__name__}: {exc}"
        yield mcp.types.CallToolResult(
            content=[mcp.types.TextContent(type="text", text=str(text))],
            isError=str(text).startswith("error:"),
        )

    @staticmethod
    async def _invoke(
        tool: FunctionTool,
        run_context: Any,
        event: Any,
        tool_args: dict[str, Any],
    ) -> str:
        """Call the tool and normalise its return value to text.

        Args:
            tool: Granted tool.
            run_context: Run context carrying the parent event.
            event: Parent message event.
            tool_args: Arguments produced by the model.

        Returns:
            Tool output as text.
        """
        handler = getattr(tool, "handler", None)
        if handler is not None:
            result = handler(event, **tool_args)
            if inspect.isasyncgen(result):
                last: Any = None
                async for item in result:
                    last = item
                result = last
            elif inspect.isawaitable(result):
                result = await result
        else:
            call = getattr(tool, "call", None)
            if call is None:
                return "error: tool has no callable handler"
            result = await call(run_context, **tool_args)

        if isinstance(result, mcp.types.CallToolResult):
            return "\n".join(
                item.text
                for item in result.content
                if isinstance(item, mcp.types.TextContent)
            )
        if isinstance(result, str):
            return result
        plain = getattr(result, "get_plain_text", None)
        if callable(plain):
            return plain()
        return "" if result is None else str(result)


async def run_subagent_task(
    *,
    provider: Any,
    event: Any,
    session_id: str,
    description: str,
    tools: list[FunctionTool],
    max_turns: int,
    tool_timeout_seconds: float,
) -> SubAgentOutcome:
    """Run one sub-agent to completion.

    Args:
        provider: Chat provider used for the run.
        event: Parent message event, handed to the granted tools.
        session_id: Unified message origin of the parent session.
        description: Self-contained task text.
        tools: Granted tools; an empty list runs a text-only completion.
        max_turns: Upper bound of model turns in the tool loop.
        tool_timeout_seconds: Hard timeout of one tool call.

    Returns:
        The conclusion and the per-tool call counts.

    Raises:
        RuntimeError: When no chat provider is available.
    """
    if provider is None:
        raise RuntimeError("no chat provider is configured")

    if not tools:
        response = await provider.text_chat(
            prompt=description,
            session_id=session_id,
            system_prompt=SUBAGENT_SYSTEM_PROMPT,
        )
        return SubAgentOutcome(conclusion=(response.completion_text or "").strip())

    usage: dict[str, int] = {}
    runner = ToolLoopAgentRunner()
    request = ProviderRequest(
        prompt=description,
        session_id=session_id,
        system_prompt=SUBAGENT_SYSTEM_PROMPT,
        func_tool=ToolSet(tools=list(tools)),
        contexts=[],
    )
    await runner.reset(
        provider=provider,
        request=request,
        run_context=ContextWrapper(
            context=SimpleNamespace(
                event=event,
                subagent_usage=usage,
                subagent_tool_timeout=tool_timeout_seconds,
            )
        ),
        tool_executor=_SubAgentExecutor(),
        agent_hooks=BaseAgentRunHooks(),
        streaming=False,
    )
    try:
        async for _ in runner.step_until_done(max(1, int(max_turns))):
            pass
    except Exception:
        logger.exception("[life] subagent tool loop failed")
        raise

    final = runner.get_final_llm_resp()
    conclusion = ""
    if final is not None:
        conclusion = (final.completion_text or "").strip()
    return SubAgentOutcome(conclusion=conclusion, tool_usage=dict(usage))
