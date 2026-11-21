import asyncio
import json
import uuid
from contextlib import aclosing

import mcp

from bot import logger
from bot.core.agent.mcp_client import MCPTool
from bot.core.agent.run_context import ContextWrapper
from bot.core.agent.runtime import get_agent_permissions
from bot.core.agent.tool import FunctionTool
from bot.core.agent.tool_executor import BaseFunctionToolExecutor
from bot.core.agent_wakeup import wake_main_agent
from bot.core.bot_agent_runtime.context import BotAgentContext
from bot.core.bot_agent_runtime.tools.computer_tools import (
    ShellSessionTool,
)
from bot.core.chatui import ViewResult, emit_view
from bot.core.message.message_event_result import (
    MessageChain,
)
from bot.core.star.tool_execution import call_local_llm_tool
from bot.core.utils.active_event_registry import active_event_registry

BACKGROUND_TASK_RESULT_WOKE_USER_PROMPT = (
    "<system_reminder>\n"
    "A background task you initiated has completed. "
    "Use the existing conversation to decide what to do next. "
    "Do not greet the user, ask unnecessary questions, or send a message if no meaningful action is required. "
    "If contacting the user, explain why and refer to the task naturally. "
    "You can use `send_message_to_user` to send images, files, and other generated artifacts. "
    "Output in the same language as the previous conversation.\n\n"
    "# BACKGROUND TASK CONTEXT\n"
    "The following object describes the background task that completed:\n"
    "{background_task_result}"
    "\n</system_reminder>"
)


class FunctionToolExecutor(BaseFunctionToolExecutor[BotAgentContext]):
    @classmethod
    async def execute(cls, tool, run_context, **tool_args):
        """执行函数调用。

        Args:
            event (BotMessageEvent): 事件对象, 当 origin 为 local 时必须提供。
            **kwargs: 函数调用的参数。

        Returns:
            AsyncGenerator[None | mcp.types.CallToolResult, None]

        """
        if isinstance(tool, MCPTool):
            async with aclosing(
                cls._execute_mcp(tool, run_context, **tool_args)
            ) as results:
                async for r in results:
                    yield r
            return

        elif tool.is_background_task:
            event = run_context.context.event
            if active_event_registry.get_background_stop_signal(event).is_set():
                return
            task_id = uuid.uuid4().hex

            async def _run_in_background() -> None:
                try:
                    await cls._execute_background(
                        tool=tool,
                        run_context=run_context,
                        task_id=task_id,
                        **tool_args,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.error(
                        f"Background task {task_id} failed: {e!s}",
                        exc_info=True,
                    )

            task = asyncio.create_task(_run_in_background())
            active_event_registry.register_background_task(event, task)
            text_content = mcp.types.TextContent(
                type="text",
                text=f"Background task submitted. task_id={task_id}",
            )
            yield mcp.types.CallToolResult(content=[text_content])

            return
        else:
            async with aclosing(
                cls._execute_local(tool, run_context, **tool_args)
            ) as results:
                async for r in results:
                    yield r
            return

    @classmethod
    async def _execute_background(
        cls,
        tool: FunctionTool,
        run_context: ContextWrapper[BotAgentContext],
        task_id: str,
        **tool_args,
    ) -> None:
        # run the tool
        permissions = get_agent_permissions(run_context)
        result = mcp.types.CallToolResult(content=[])
        try:
            async for r in cls._execute_local(
                tool, run_context, tool_call_timeout=3600, **tool_args
            ):
                if isinstance(r, mcp.types.CallToolResult):
                    result.content.extend(r.content)
                    result.isError = result.isError or r.isError
                    if r.structuredContent is not None:
                        result.structuredContent = r.structuredContent
        except Exception as e:
            result.isError = True
            result.content.append(
                mcp.types.TextContent(
                    type="text",
                    text=f"error: Background task execution failed, internal error: {e!s}",
                )
            )

        event = run_context.context.event

        await wake_main_agent(
            ctx=run_context.context.context,
            umo=event.unified_msg_origin,
            source="background_task",
            source_id=task_id,
            user_prompt=BACKGROUND_TASK_RESULT_WOKE_USER_PROMPT.format(
                background_task_result=json.dumps(
                    {
                        "task_id": task_id,
                        "tool_name": tool.name,
                        "tool_args": tool_args,
                        "note": event.get_extra("background_note"),
                    },
                    ensure_ascii=False,
                )
            ),
            role=permissions.role.value,
            tool_result=(tool.name, result),
            source_event=event,
            stop_signal=active_event_registry.get_background_stop_signal(event),
        )

    @classmethod
    async def _execute_local(
        cls,
        tool: FunctionTool,
        run_context: ContextWrapper[BotAgentContext],
        *,
        tool_call_timeout: int | None = None,
        **tool_args,
    ):
        event = run_context.context.event
        if not event:
            raise ValueError("Event must be provided for local function tools.")

        is_override_call = False
        for ty in type(tool).mro():
            if "call" in ty.__dict__ and ty.__dict__["call"] is not FunctionTool.call:
                is_override_call = True
                break

        # 检查 tool 下有没有 run 方法
        if not tool.handler and not hasattr(tool, "run") and not is_override_call:
            raise ValueError("Tool must have a valid handler or override 'run' method.")

        awaitable = None
        method_name = ""
        if tool.handler:
            awaitable = tool.handler
            method_name = "decorator_handler"
        elif is_override_call:
            awaitable = tool.call
            method_name = "call"
        elif hasattr(tool, "run"):
            awaitable = getattr(tool, "run")
            method_name = "run"
        if awaitable is None:
            raise ValueError("Tool must have a valid handler or override 'run' method.")

        effective_timeout = (
            tool_call_timeout or tool.execution_timeout or run_context.tool_call_timeout
        )
        if isinstance(tool, ShellSessionTool) and tool_args.get("action") in {
            "poll",
            "write",
            "write_line",
            "interrupt",
        }:
            yield_time_ms = tool_args.get("yield_time_ms", 5_000)
            if isinstance(yield_time_ms, int) and 0 <= yield_time_ms <= 300_000:
                # Allow the requested wait plus time to write input and collect output.
                effective_timeout = max(effective_timeout, yield_time_ms / 1000 + 5)

        wrapper = call_local_llm_tool(
            context=run_context,
            handler=awaitable,
            method_name=method_name,
            **tool_args,
        )
        async with aclosing(wrapper):
            while True:
                try:
                    resp = await asyncio.wait_for(
                        anext(wrapper),
                        timeout=effective_timeout,
                    )
                    if resp is not None:
                        if isinstance(resp, ViewResult):
                            if resp.name is None:
                                raise ValueError(
                                    "Tools must create named views, not update a UI action."
                                )
                            await emit_view(
                                event,
                                tool.handler_module_path
                                or getattr(awaitable, "func", awaitable).__module__,
                                resp,
                            )
                            resp = resp.text
                        if isinstance(resp, mcp.types.CallToolResult):
                            yield resp
                        else:
                            text_content = mcp.types.TextContent(
                                type="text",
                                text=str(resp),
                            )
                            yield mcp.types.CallToolResult(content=[text_content])
                    else:
                        # NOTE: Tool 在这里直接请求发送消息给用户
                        # TODO: 是否需要判断 event.get_result() 是否为空?
                        # 如果为空,则说明没有发送消息给用户,并且返回值为空,将返回一个特殊的 TextContent,其内容如"工具没有返回内容"
                        if res := run_context.context.event.get_result():
                            if res.chain:
                                try:
                                    await event.send(
                                        MessageChain(
                                            chain=res.chain,
                                            type="tool_direct_result",
                                        )
                                    )
                                except Exception as e:
                                    logger.error(
                                        f"Tool 直接发送消息失败: {e}",
                                        exc_info=True,
                                    )
                        yield None
                except asyncio.TimeoutError:
                    raise Exception(
                        f"tool {tool.name} execution timeout after {effective_timeout} seconds.",
                    )
                except StopAsyncIteration:
                    break

    @classmethod
    async def _execute_mcp(
        cls,
        tool: FunctionTool,
        run_context: ContextWrapper[BotAgentContext],
        **tool_args,
    ):
        res = await tool.call(run_context, **tool_args)
        if not res:
            return
        yield res

    @classmethod
    def detach(cls, *, tool, run_context, executor, pending_result, task_id, arguments):
        """Continue an in-flight tool in the background and wake its original session.

        Args:
            tool: Tool already executing.
            run_context: Context of the initiating turn.
            executor: Owned iterator, resumed after the pending read completes.
            pending_result: Existing in-flight read; never execute the tool again.
            task_id: Execution identity linking submission and completion.
            arguments: Actual tool arguments included in the completion notice.

        Returns:
            A submission result, or None when no native session can receive completion.
        """
        context = run_context.context
        session = getattr(context, "agent_session", None)
        if session is None:
            return None
        event = context.event
        permissions = get_agent_permissions(run_context)
        stop_signal = active_event_registry.get_background_stop_signal(event)
        if stop_signal.is_set():
            return None

        async def complete() -> None:
            result = mcp.types.CallToolResult(content=[])
            error = None
            try:
                # Register after starting so cancellation always runs iterator cleanup.
                task = asyncio.current_task()
                assert task is not None
                active_event_registry.register_background_task(event, task)
                try:
                    item = await pending_result
                    while True:
                        if isinstance(item, mcp.types.CallToolResult):
                            result.content.extend(item.content)
                            result.isError = result.isError or item.isError
                            if item.structuredContent is not None:
                                result.structuredContent = item.structuredContent
                        item = await anext(executor)
                except StopAsyncIteration:
                    pass
                except Exception as exc:
                    error = {"code": type(exc).__name__, "message": str(exc)}
            finally:
                if not pending_result.done():
                    pending_result.cancel()
                await asyncio.gather(pending_result, return_exceptions=True)
                try:
                    await executor.aclose()
                except Exception as exc:
                    error = {"code": type(exc).__name__, "message": str(exc)}
            if error is not None:
                result.isError = True
                result.content.append(
                    mcp.types.TextContent(
                        type="text", text=f"{error['code']}: {error['message']}"
                    )
                )
            try:
                await wake_main_agent(
                    ctx=context.context,
                    umo=event.unified_msg_origin,
                    source="background_task",
                    source_id=task_id,
                    user_prompt=BACKGROUND_TASK_RESULT_WOKE_USER_PROMPT.format(
                        background_task_result=json.dumps(
                            {
                                "task_id": task_id,
                                "tool_name": tool.name,
                                "tool_args": arguments,
                            },
                            ensure_ascii=False,
                        )
                    ),
                    role=permissions.role.value,
                    stop_signal=stop_signal,
                    agent_session=session,
                    tool_result=(tool.name, result),
                    source_event=event,
                )
            except Exception:
                logger.exception("Unable to deliver background tool result %s", task_id)

        asyncio.create_task(complete(), name=f"background-tool:{task_id}")
        return mcp.types.CallToolResult(
            content=[
                mcp.types.TextContent(
                    type="text",
                    text=f"Tool continues in background. task_id={task_id}. "
                    "Its result will arrive in a separate wakeup; do not repeat the tool call.",
                )
            ],
            structuredContent={"background_task_id": task_id, "status": "running"},
        )
