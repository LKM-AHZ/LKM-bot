import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.tools.computer_tools.shell import ShellSessionTool


class _DummyEvent:
    def __init__(self, message_components: list[object] | None = None) -> None:
        self.unified_msg_origin = "webchat:FriendMessage:webchat!user!session"
        self.message_obj = SimpleNamespace(message=message_components or [])
        self.role = "member"

    def get_extra(self, _key: str):
        return None


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
    from astrbot.core import astr_agent_tool_exec as tool_exec

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


class _DoneRunner:
    async def step_until_done(self, _max_step):
        for item in ():
            yield item

    def get_final_llm_resp(self):
        return SimpleNamespace(role="assistant", completion_text="done")



@pytest.mark.asyncio
async def test_background_wakeup_passes_history_and_provider_settings_to_main_agent(
    monkeypatch: pytest.MonkeyPatch,
):
    """Test background wakeup keeps structured history and provider settings."""
    provider_settings = {
        "fallback_chat_models": ["fallback-provider"],
        "request_max_retries": 3,
        "stream": True,
    }
    history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    captured: dict = {}

    async def _fake_get_session_conv(**_kwargs):
        return SimpleNamespace(history=json.dumps(history))

    async def _fake_build_main_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(agent_runner=_DoneRunner())

    monkeypatch.setattr(
        "astrbot.core.astr_main_agent._get_session_conv",
        _fake_get_session_conv,
    )
    monkeypatch.setattr(
        "astrbot.core.astr_main_agent.build_main_agent",
        _fake_build_main_agent,
    )
    monkeypatch.setattr(
        "astrbot.core.astr_agent_tool_exec.persist_agent_history",
        AsyncMock(),
    )

    send_tool = FunctionTool(
        name="send_message_to_user",
        description="send",
        parameters={"type": "object", "properties": {}},
    )
    context = SimpleNamespace(
        get_config=lambda **_kwargs: {"provider_settings": provider_settings},
        get_llm_tool_manager=lambda: SimpleNamespace(
            get_builtin_tool=lambda _tool_cls: send_tool
        ),
        conversation_manager=SimpleNamespace(),
    )
    run_context = ContextWrapper(
        context=SimpleNamespace(event=_DummyEvent([]), context=context),
        tool_call_timeout=456,
    )

    await FunctionToolExecutor._wake_main_agent_for_background_result(
        run_context,
        task_id="task-id",
        tool_name="long_tool",
        result_text="ok",
        tool_args={},
        note="task finished",
        summary_name="BackgroundTask",
    )

    config = captured["config"]
    assert config.tool_call_timeout == 456
    assert config.streaming_response == provider_settings["stream"]
    assert config.provider_settings == provider_settings
    assert config.provider_settings["fallback_chat_models"] == ["fallback-provider"]
    request = captured["req"]
    assert "old question" not in request.system_prompt
    assert "old answer" not in request.system_prompt
    assert request.contexts == history


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_settings", "expected_max_step"),
    [
        pytest.param({"max_agent_step": 50}, 50, id="configured"),
        pytest.param({}, 128, id="missing_falls_back_to_default"),
        pytest.param({"max_agent_step": True}, 128, id="boolean_falls_back_to_default"),
        pytest.param({"max_agent_step": "50"}, 50, id="numeric_string_coerced"),
        pytest.param({"max_agent_step": 0}, 1, id="zero_clamped_to_min"),
    ],
)
async def test_background_wakeup_applies_max_agent_step(
    monkeypatch: pytest.MonkeyPatch,
    provider_settings: dict,
    expected_max_step: int,
):
    class _StepCapturingRunner:
        def __init__(self):
            self.captured_max_step = None

        async def step_until_done(self, max_step):
            self.captured_max_step = max_step
            if False:
                yield

        def get_final_llm_resp(self):
            return SimpleNamespace(role="assistant", completion_text="done")

    runner = _StepCapturingRunner()

    async def _fake_get_session_conv(**_kwargs):
        return SimpleNamespace(history="[]")

    async def _fake_build_main_agent(**_kwargs):
        return SimpleNamespace(agent_runner=runner)

    monkeypatch.setattr(
        "astrbot.core.astr_main_agent._get_session_conv",
        _fake_get_session_conv,
    )
    monkeypatch.setattr(
        "astrbot.core.astr_main_agent.build_main_agent",
        _fake_build_main_agent,
    )
    monkeypatch.setattr(
        "astrbot.core.astr_agent_tool_exec.persist_agent_history",
        AsyncMock(),
    )

    send_tool = FunctionTool(
        name="send_message_to_user",
        description="send",
        parameters={"type": "object", "properties": {}},
    )
    context = SimpleNamespace(
        get_config=lambda **_kwargs: {
            "provider_settings": {},
            "agent_runner": {
                "runner_type": "local",
                "config": {
                    "misc": {"max_steps": provider_settings.get("max_agent_step", 128)}
                },
            },
        },
        get_llm_tool_manager=lambda: SimpleNamespace(
            get_builtin_tool=lambda _tool_cls: send_tool
        ),
        conversation_manager=SimpleNamespace(),
    )
    run_context = ContextWrapper(
        context=SimpleNamespace(event=_DummyEvent([]), context=context),
        tool_call_timeout=120,
    )

    await FunctionToolExecutor._wake_main_agent_for_background_result(
        run_context,
        task_id="task-id",
        tool_name="long_tool",
        result_text="ok",
        tool_args={},
        note="task finished",
        summary_name="BackgroundTask",
    )

    assert runner.captured_max_step == expected_max_step
