import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import mcp
import pytest

from bot.core.agent.run_context import ContextWrapper
from bot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from bot.core.agent.tool import FunctionTool
from bot.core.bot_agent_runtime.tool_executor import FunctionToolExecutor
from bot.core.bot_agent_runtime.tools.computer_tools.shell import ShellSessionTool
from bot.core.provider.func_tool_manager import (
    FunctionToolManager,
)
from bot.core.utils.active_event_registry import (
    ActiveEventRegistry,
)


class _DummyEvent:
    def __init__(self, message_components: list[object] | None = None) -> None:
        self.unified_msg_origin = "webchat:FriendMessage:webchat!user!session"
        self.message_obj = SimpleNamespace(message=message_components or [])
        self.role = "member"
        self.extras: dict[str, object] = {}

    def get_extra(self, key: str):
        return self.extras.get(key)

    def set_extra(self, key: str, value: object) -> None:
        self.extras[key] = value


class _DummyTool:
    def __init__(self) -> None:
        self.name = "transfer_to_subagent"
        self.agent = SimpleNamespace(name="subagent")


def _build_run_context(message_components: list[object] | None = None):
    event = _DummyEvent(message_components=message_components)
    ctx = SimpleNamespace(event=event, context=SimpleNamespace())
    return ContextWrapper(context=ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["poll", "write", "write_line", "interrupt"])
@pytest.mark.parametrize(
    ("yield_time_ms", "configured_timeout", "expected_timeout"),
    [(300_000, 120, 305), (5_000, 120, 120), (300_000, 600, 600)],
)
async def test_shell_session_wait_fits_inside_tool_timeout(
    monkeypatch, action, yield_time_ms, configured_timeout, expected_timeout
):
    from bot.core.bot_agent_runtime import tool_executor as tool_exec

    tool = ShellSessionTool()
    monkeypatch.setattr(tool, "call", AsyncMock(return_value="output"))
    run_context = _build_run_context()
    run_context.tool_call_timeout = configured_timeout
    wait_for = AsyncMock(wraps=asyncio.wait_for)
    monkeypatch.setattr(tool_exec.asyncio, "wait_for", wait_for)

    results = [
        result
        async for result in FunctionToolExecutor._execute_local(
            tool,
            run_context,
            action=action,
            session_id="sh_test",
            yield_time_ms=yield_time_ms,
        )
    ]

    assert results[0].content[0].text == "output"
    assert all(
        call.kwargs["timeout"] == expected_timeout for call in wait_for.await_args_list
    )


@pytest.mark.asyncio

@pytest.mark.parametrize("foreground_done", [False, True])
@pytest.mark.parametrize("phase", ["tool", "wakeup", "complete", "failure"])
async def test_background_execution_lifecycle(
    monkeypatch, foreground_done, phase
):
    registry = ActiveEventRegistry()
    monkeypatch.setattr(
        "bot.core.bot_agent_runtime.tool_executor.active_event_registry",
        registry,
    )
    run_context = _build_run_context()
    event = run_context.context.event
    started, release, delivered = (asyncio.Event() for _ in range(3))

    async def execute(cls, *args, **kwargs):
        if phase == "failure":
            raise RuntimeError("tool failed")
        if phase == "tool":
            started.set()
            await release.wait()
        yield mcp.types.CallToolResult(
            content=[mcp.types.TextContent(type="text", text="done")]
        )

    async def wake(**kwargs):
        started.set()
        await release.wait()
        delivered.set()

    wakeup = AsyncMock(side_effect=wake)
    monkeypatch.setattr(
        "bot.core.bot_agent_runtime.tool_executor.wake_main_agent", wakeup
    )
    monkeypatch.setattr(
        FunctionToolExecutor,
        "_execute_local",
        classmethod(execute),
    )
    tool = FunctionTool(
        name="slow",
        description="slow tool",
        parameters={"type": "object", "properties": {}},
        is_background_task=True,
    )
    arguments: dict = {}
    registry.register(event)
    results = [
        r async for r in FunctionToolExecutor.execute(tool, run_context, **arguments)
    ]
    task = next(iter(registry._background_tasks[event.unified_msg_origin]))
    try:
        assert "task_id=" in results[0].content[0].text
        await asyncio.wait_for(started.wait(), timeout=1)
        if foreground_done:
            registry.unregister(event)
        cancelled = phase in ("tool", "wakeup")
        if cancelled:
            assert registry.request_agent_stop_all(event.unified_msg_origin) == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)
        assert task.cancelled() == cancelled
        assert delivered.is_set() != cancelled
        assert wakeup.await_count == (0 if phase == "tool" else 1)
        if wakeup.await_count:
            assert wakeup.call_args.kwargs["source"] == "background_task"
            assert wakeup.call_args.kwargs["source_id"] in results[0].content[0].text
            assert wakeup.call_args.kwargs["user_prompt"].startswith(
                "<system_reminder>\n"
            )
            assert wakeup.call_args.kwargs["user_prompt"].endswith(
                "\n</system_reminder>"
            )
        if phase == "failure":
            assert "tool failed" in wakeup.call_args.kwargs["user_prompt"]
        assert not registry._background_tasks
        if cancelled:
            assert [
                r
                async for r in FunctionToolExecutor.execute(
                    tool, run_context, **arguments
                )
            ] == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        registry.unregister(event)


@pytest.mark.asyncio
async def test_external_runner_cancel_closes_pending_tool_executor():
    runner = ToolLoopAgentRunner()
    runner._abort_signal = asyncio.Event()
    started, release, closed, side_effect = (asyncio.Event() for _ in range(4))

    async def execute():
        started.set()
        try:
            await release.wait()
            side_effect.set()
            yield "done"
        finally:
            closed.set()

    results = runner._iter_tool_executor_results(execute())
    task = asyncio.create_task(anext(results))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)
        assert task.cancelled() and closed.is_set()
        assert not side_effect.is_set()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await results.aclose()


@pytest.mark.asyncio
async def test_tool_specific_timeout_survives_permission_wrapper(monkeypatch):
    from bot.core.bot_agent_runtime import tool_executor as executor

    manager = FunctionToolManager()
    tool = FunctionTool(
        name="waiting",
        description="Wait",
        parameters={},
        handler=AsyncMock(return_value="done"),
        execution_timeout=65,
    )
    manager.func_list = [tool]
    context = _build_run_context()
    context.tool_call_timeout = 1
    waiting = AsyncMock(wraps=asyncio.wait_for)
    monkeypatch.setattr(executor.asyncio, "wait_for", waiting)
    guarded = manager.get_full_tool_set().get_tool("waiting")
    results = [
        result async for result in FunctionToolExecutor._execute_local(guarded, context)
    ]
    assert results[0].content[0].text == "done"
    assert waiting.await_args_list[0].kwargs["timeout"] == 65
