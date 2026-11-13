"""Tests for background sub-agent tasks, delivery and the three tools."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from bot.core.agent.tool import FunctionTool
from bot.core.db.sqlite import SQLiteDatabase
from bot.core.provider.entities import LLMResponse
from bot.core.provider.provider import Provider
from bot.life.config import LifeConfig, SubAgentConfig
from bot.life.service import LifeService, set_life_service
from bot.life.subagent.inbox import SubAgentInbox, SubAgentNotice, notice_from_task
from bot.life.subagent.models import SubAgentOutcome, SubAgentTask
from bot.life.subagent.notify import SubAgentNotifier
from bot.life.subagent.runner import run_subagent_task
from bot.life.subagent.tasks import SubAgentTaskManager
from bot.life.subagent.tools import (
    register_subagent_tools,
    unregister_subagent_tools,
)


class FakeManager:
    """Minimal stand-in for ``FunctionToolManager``."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def add_func(self, name, func_args, desc, handler) -> None:
        self.tools[name] = handler

    def remove_func(self, name) -> None:
        self.tools.pop(name, None)

    def get_func(self, name):
        return self.tools.get(name)


def _event(umo: str = "aiocqhttp:GroupMessage:1"):
    return SimpleNamespace(
        unified_msg_origin=umo,
        get_group_id=lambda: "1",
    )


def _task(task_id: str = "subagent-1", session_id: str = "s1") -> SubAgentTask:
    return SubAgentTask(
        task_id=task_id,
        session_id=session_id,
        description="查一下版本号",
        conclusion="版本 3.2.1" if task_id != "subagent-2" else "",
    )


def _ok_runner(outcome: SubAgentOutcome | None = None):
    async def runner(task: SubAgentTask) -> SubAgentOutcome:
        return outcome or SubAgentOutcome(conclusion="完成：版本 3.2.1")

    return runner


@pytest.mark.asyncio
async def test_start_runs_task_and_reports_finished() -> None:
    finished: list[SubAgentTask] = []
    manager = SubAgentTaskManager()
    task = manager.start(
        session_id="s1",
        description="查版本",
        granted_tools=[],
        runner=_ok_runner(),
        on_finished=finished.append,
    )
    assert task is not None
    assert task.status == "running"
    assert task.task_id == "subagent-1"

    await task.asyncio_task

    assert task.status == "completed"
    assert task.conclusion == "完成：版本 3.2.1"
    assert finished == [task]
    assert manager.stats()["completed"] == 1


@pytest.mark.asyncio
async def test_start_rejects_global_and_session_capacity() -> None:
    gate = asyncio.Event()

    async def blocked(task: SubAgentTask) -> SubAgentOutcome:
        await gate.wait()
        return SubAgentOutcome(conclusion="done")

    manager = SubAgentTaskManager(max_running=1, max_running_per_session=1)
    first = manager.start(
        session_id="s1",
        description="a",
        granted_tools=[],
        runner=blocked,
        on_finished=lambda task: None,
    )
    assert first is not None
    second = manager.start(
        session_id="s2",
        description="b",
        granted_tools=[],
        runner=blocked,
        on_finished=lambda task: None,
    )
    assert second is None
    gate.set()
    await first.asyncio_task


@pytest.mark.asyncio
async def test_session_capacity_is_independent_of_global_one() -> None:
    gate = asyncio.Event()

    async def blocked(task: SubAgentTask) -> SubAgentOutcome:
        await gate.wait()
        return SubAgentOutcome(conclusion="done")

    manager = SubAgentTaskManager(max_running=3, max_running_per_session=1)
    first = manager.start(
        session_id="s1",
        description="a",
        granted_tools=[],
        runner=blocked,
        on_finished=lambda task: None,
    )
    second = manager.start(
        session_id="s1",
        description="b",
        granted_tools=[],
        runner=blocked,
        on_finished=lambda task: None,
    )
    assert first is not None
    assert second is None
    gate.set()
    await first.asyncio_task


@pytest.mark.asyncio
async def test_runner_exception_marks_failed() -> None:
    async def boom(task: SubAgentTask) -> SubAgentOutcome:
        raise RuntimeError("provider down")

    manager = SubAgentTaskManager()
    task = manager.start(
        session_id="s1",
        description="x",
        granted_tools=[],
        runner=boom,
        on_finished=lambda finished: None,
    )
    assert task is not None
    await task.asyncio_task
    assert task.status == "failed"
    assert "provider down" in task.error


@pytest.mark.asyncio
async def test_timeout_marks_failed() -> None:
    async def slow(task: SubAgentTask) -> SubAgentOutcome:
        await asyncio.sleep(0.5)
        return SubAgentOutcome(conclusion="late")

    manager = SubAgentTaskManager(timeout_seconds=0.1)
    task = manager.start(
        session_id="s1",
        description="x",
        granted_tools=[],
        runner=slow,
        on_finished=lambda finished: None,
    )
    assert task is not None
    await task.asyncio_task
    assert task.status == "failed"
    assert "timeout" in task.error


@pytest.mark.asyncio
async def test_stop_cancels_running_task() -> None:
    async def blocked(task: SubAgentTask) -> SubAgentOutcome:
        await asyncio.sleep(10)
        return SubAgentOutcome(conclusion="never")

    manager = SubAgentTaskManager()
    task = manager.start(
        session_id="s1",
        description="x",
        granted_tools=[],
        runner=blocked,
        on_finished=lambda finished: None,
    )
    assert task is not None
    await asyncio.sleep(0)
    assert manager.stop(task.task_id) is True
    await asyncio.sleep(0.01)
    assert task.status == "stopped"
    assert manager.stop(task.task_id) is False


@pytest.mark.asyncio
async def test_finished_keep_drops_oldest_per_session() -> None:
    manager = SubAgentTaskManager(finished_keep=1)
    for _ in range(2):
        task = manager.start(
            session_id="s1",
            description="x",
            granted_tools=[],
            runner=_ok_runner(),
            on_finished=lambda finished: None,
        )
        assert task is not None
        await task.asyncio_task
    kept = manager.list_for("s1")
    assert [task.task_id for task in kept] == ["subagent-2"]
    assert manager.get("subagent-1") is None


def test_inbox_keeps_newest_and_drops_expired() -> None:
    inbox = SubAgentInbox(max_per_session=2, max_age_minutes=0)
    for index in range(3):
        inbox.add(
            SubAgentNotice(
                session_id="s1",
                task_id=f"subagent-{index + 1}",
                description="x",
                conclusion=f"c{index}",
            )
        )
    assert [notice.task_id for notice in inbox.take("s1")] == [
        "subagent-2",
        "subagent-3",
    ]

    aged = SubAgentInbox(max_per_session=5, max_age_minutes=1)
    aged.add(
        SubAgentNotice(
            session_id="s1",
            task_id="old",
            description="x",
            conclusion="c",
            finished_at=time.time() - 3600,
        )
    )
    assert aged.take("s1") == []


def test_inbox_take_limit_keeps_the_rest() -> None:
    inbox = SubAgentInbox()
    for index in range(3):
        inbox.add(
            SubAgentNotice(
                session_id="s1",
                task_id=f"subagent-{index + 1}",
                description="x",
                conclusion="c",
            )
        )
    first = inbox.take("s1", limit=1)
    assert [notice.task_id for notice in first] == ["subagent-1"]
    assert [notice.task_id for notice in inbox.take("s1")] == [
        "subagent-2",
        "subagent-3",
    ]
    assert inbox.stats() == {"sessions": 0, "pending": 0}


def test_notice_from_task_caps_conclusion() -> None:
    task = _task()
    task.conclusion = "x" * 100
    notice = notice_from_task(task, max_chars=10)
    assert notice.conclusion == "x" * 10 + "…"
    assert notice.session_id == "s1"


@pytest.mark.asyncio
async def test_notifier_injects_block_once() -> None:
    config = SubAgentConfig(enabled=True, inject_max_items=2)
    inbox = SubAgentInbox()
    inbox.add(
        SubAgentNotice(
            session_id="s1",
            task_id="subagent-1",
            description="查版本",
            conclusion="版本 3.2.1",
        )
    )
    notifier = SubAgentNotifier(config, inbox)

    req = SimpleNamespace(system_prompt="你是助手。")
    assert await notifier.inject(_event("s1"), req) is True
    assert "<life_subagent>" in req.system_prompt
    assert "[subagent-1] 查版本：版本 3.2.1" in req.system_prompt
    assert inbox.stats()["pending"] == 0

    assert await notifier.inject(_event("s1"), req) is False
    assert notifier.status()["injected"] == 1


@pytest.mark.asyncio
async def test_notifier_skips_when_tag_already_present() -> None:
    config = SubAgentConfig(enabled=True)
    inbox = SubAgentInbox()
    inbox.add(
        SubAgentNotice(
            session_id="s1",
            task_id="subagent-1",
            description="x",
            conclusion="c",
        )
    )
    notifier = SubAgentNotifier(config, inbox)
    req = SimpleNamespace(system_prompt="<life_subagent>old</life_subagent>")
    assert await notifier.inject(_event("s1"), req) is False
    assert inbox.stats()["pending"] == 1


@pytest.mark.asyncio
async def test_subagent_tools_status_and_stop_are_session_scoped() -> None:
    manager = SubAgentTaskManager()
    task = manager.start(
        session_id="s1",
        description="x",
        granted_tools=["web_search"],
        runner=_ok_runner(),
        on_finished=lambda finished: None,
    )
    assert task is not None
    await task.asyncio_task

    service = SimpleNamespace(
        subagents=manager,
        config=SimpleNamespace(session_enabled=lambda umo, group_id=None: True),
    )
    tools = FakeManager()
    register_subagent_tools(tools, service)
    status = tools.tools["subagent_status"]

    listed = await status(_event("s1"))
    assert "subagent-1" in listed
    assert "completed" in listed

    other = await status(_event("s2"))
    assert "No sub-agent tasks" in other

    hidden = await status(_event("s2"), task_id="subagent-1")
    assert "no task" in hidden

    stopped = await tools.tools["subagent_stop"](_event("s1"), "subagent-1")
    assert "not running" in stopped

    unregister_subagent_tools(tools)
    assert tools.tools == {}


class FakeTextProvider(Provider):
    """Provider that answers once, without tools."""

    def __init__(self) -> None:
        super().__init__({}, {})
        self.calls: list[dict] = []

    def get_current_key(self) -> str:
        return "test_key"

    def set_key(self, key: str) -> None:
        pass

    async def get_models(self) -> list[str]:
        return ["test_model"]

    async def text_chat(self, **kwargs) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(role="assistant", completion_text="结论：版本 3.2.1")


class ToolThenFinalProvider(Provider):
    """Provider that calls one tool, then answers."""

    def __init__(self, tool_name: str, args: dict) -> None:
        super().__init__({}, {})
        self.tool_name = tool_name
        self.args = args
        self.calls = 0

    def get_current_key(self) -> str:
        return "test_key"

    def set_key(self, key: str) -> None:
        pass

    async def get_models(self) -> list[str]:
        return ["test_model"]

    async def text_chat(self, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                role="assistant",
                completion_text="",
                tools_call_name=[self.tool_name],
                tools_call_args=[self.args],
                tools_call_ids=["call-1"],
            )
        return LLMResponse(role="assistant", completion_text="结论：42")


@pytest.mark.asyncio
async def test_run_subagent_text_only_uses_provider() -> None:
    provider = FakeTextProvider()
    outcome = await run_subagent_task(
        provider=provider,
        event=_event(),
        session_id="s1",
        description="查版本",
        tools=[],
        max_turns=3,
        tool_timeout_seconds=5,
    )
    assert outcome.conclusion == "结论：版本 3.2.1"
    assert outcome.tool_usage == {}
    assert provider.calls[0]["prompt"] == "查版本"
    assert "后台子代理" in provider.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_run_subagent_can_use_a_granted_tool() -> None:
    calls: list[str] = []

    async def lookup(event, query: str) -> str:
        calls.append(query)
        return "pixiv 插件 3.2.1"

    tool = FunctionTool(
        name="lookup",
        description="lookup",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
        handler=lookup,
    )
    provider = ToolThenFinalProvider("lookup", {"query": "pixiv"})
    outcome = await run_subagent_task(
        provider=provider,
        event=_event(),
        session_id="s1",
        description="查插件版本",
        tools=[tool],
        max_turns=3,
        tool_timeout_seconds=5,
    )

    assert outcome.conclusion == "结论：42"
    assert outcome.tool_usage == {"lookup": 1}
    assert calls == ["pixiv"]


@pytest.mark.asyncio
async def test_run_subagent_without_provider_raises() -> None:
    with pytest.raises(RuntimeError):
        await run_subagent_task(
            provider=None,
            event=_event(),
            session_id="s1",
            description="x",
            tools=[],
            max_turns=1,
            tool_timeout_seconds=1,
        )


@pytest_asyncio.fixture
async def life_db(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    yield database
    await database.engine.dispose()


async def _make_service(db, tmp_path, **subagent) -> LifeService:
    config = LifeConfig(enabled=True, subagent=SubAgentConfig(enabled=True, **subagent))
    config.scope.mode = "all"
    service = LifeService(
        db,
        config=config,
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )
    await service.initialize()
    return service


@pytest.mark.asyncio
async def test_service_start_subagent_fails_without_provider(life_db, tmp_path) -> None:
    service = await _make_service(life_db, tmp_path)
    try:
        line = await service.start_subagent(
            event=_event(),
            description="查版本",
        )
        assert line.startswith("started: subagent-1")
        task = service.subagents.get("subagent-1")
        assert task is not None
        await task.asyncio_task
        assert task.status == "failed"
        assert "no chat provider" in task.error
    finally:
        await service.stop()
        set_life_service(None)


@pytest.mark.asyncio
async def test_service_start_subagent_reports_capacity(life_db, tmp_path) -> None:
    service = await _make_service(life_db, tmp_path, max_running=1)
    gate = asyncio.Event()

    async def blocked(task, event, tools):
        await gate.wait()
        return SubAgentOutcome(conclusion="done")

    service._run_subagent = blocked
    try:
        first = await service.start_subagent(event=_event(), description="a")
        assert first.startswith("started:")
        second = await service.start_subagent(event=_event(), description="b")
        assert "too many" in second
        gate.set()
        task = service.subagents.get("subagent-1")
        assert task is not None
        await task.asyncio_task
    finally:
        await service.stop()
        set_life_service(None)


@pytest.mark.asyncio
async def test_service_note_finished_delivers_to_inbox(life_db, tmp_path) -> None:
    service = await _make_service(life_db, tmp_path)
    try:
        task = _task()
        task.status = "completed"
        service.note_subagent_finished(task)
        assert service.subagent_inbox.stats()["pending"] == 1
        assert service._subagent_counts["inboxed"] == 1
    finally:
        await service.stop()
        set_life_service(None)


@pytest.mark.asyncio
async def test_service_note_finished_wakes_active_runner(life_db, tmp_path) -> None:
    service = await _make_service(life_db, tmp_path)
    run_turn = AsyncMock()
    service.star_context = SimpleNamespace(
        agent=SimpleNamespace(
            active_turn=lambda umo: object(),
            run_turn=run_turn,
        )
    )
    try:
        task = _task()
        task.status = "completed"
        service.note_subagent_finished(task)
        await asyncio.sleep(0)
        assert service._subagent_counts["woken"] == 1
        assert service.subagent_inbox.stats()["pending"] == 0
        run_turn.assert_awaited_once()
    finally:
        await service.stop()
        set_life_service(None)


@pytest.mark.asyncio
async def test_service_status_reports_subagent(life_db, tmp_path) -> None:
    service = await _make_service(life_db, tmp_path)
    try:
        status = await service.status()
        block = status["subagent"]
        assert block["enabled"] is True
        assert block["counters"]["started"] == 0
        assert block["tasks"]["running"] == 0
        assert block["delivery"]["inbox"]["pending"] == 0
    finally:
        await service.stop()
        set_life_service(None)


@pytest.mark.asyncio
async def test_subagent_tools_respect_allowlist() -> None:
    service = SimpleNamespace(
        config=SimpleNamespace(
            subagent=SimpleNamespace(
                allowed_tools=["web_search", "query_memory"],
                max_granted_tools=1,
            )
        ),
        star_context=SimpleNamespace(
            provider_manager=SimpleNamespace(
                llm_tools=SimpleNamespace(
                    get_func=lambda name: SimpleNamespace(name=name, active=True)
                )
            )
        ),
    )
    granted = LifeService.subagent_tools(service, "web_search,query_memory")
    assert [tool.name for tool in granted] == ["web_search"]

    denied = LifeService.subagent_tools(service, "send_meme")
    assert denied == []


def test_subagent_defaults_match_the_unified_background_model() -> None:
    """MaiBot 1.4 runs sub-agents only in the background with a larger budget."""
    from bot.life.config import SubAgentConfig

    config = SubAgentConfig()

    assert config.max_turns == 20
    assert config.max_running == 3
