"""Tests for tool-result condensation, the raw stash and read_tool_result."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import LifeConfig, ToolResultConfig
from bot.life.service import LifeService, set_life_service
from bot.life.toolresult.condense import build_task, run_condense, should_condense
from bot.life.toolresult.read import (
    register_tool_result_tools,
    render_read_view,
    unregister_tool_result_tools,
)
from bot.life.toolresult.store import StoredToolResult, ToolResultStore


class FakeManager:
    """Minimal stand-in for ``FunctionToolManager``."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def add_func(self, name, func_args, desc, handler) -> None:
        self.tools[name] = handler

    def remove_func(self, name) -> None:
        self.tools.pop(name, None)


def _service(
    store: ToolResultStore | None, *, enabled: bool = True, allowed: bool = True
):
    config = ToolResultConfig(enabled=enabled, read_max_chars=500)

    class _Service:
        tool_results = store

    service = _Service()
    service.config = SimpleNamespace(
        tool_result=config,
        session_enabled=lambda umo, group_id=None: allowed,
    )
    return service


def _event(umo: str = "aiocqhttp:GroupMessage:1"):
    return SimpleNamespace(
        unified_msg_origin=umo,
        get_group_id=lambda: "1",
    )


def test_should_condense_rules() -> None:
    excluded = ("read_tool_result", "send_meme")
    assert should_condense(
        "web_search",
        "x" * 100,
        enabled=True,
        threshold_chars=100,
        excluded_tool_names=excluded,
    )
    assert not should_condense(
        "web_search",
        "x" * 99,
        enabled=True,
        threshold_chars=100,
        excluded_tool_names=excluded,
    )
    assert not should_condense(
        "web_search",
        "x" * 100,
        enabled=False,
        threshold_chars=100,
        excluded_tool_names=excluded,
    )
    assert not should_condense(
        "read_tool_result",
        "x" * 100,
        enabled=True,
        threshold_chars=100,
        excluded_tool_names=excluded,
    )
    assert not should_condense(
        "", "x" * 100, enabled=True, threshold_chars=100, excluded_tool_names=excluded
    )


def test_build_task_is_self_contained_and_truncates() -> None:
    task = build_task(
        "web_search",
        {"query": "pixiv 插件"},
        "A" * 300,
        "想找一个 pixiv 相关的插件",
        max_input_chars=100,
        intent_max_chars=20,
    )
    assert "工具名称：web_search" in task
    assert '"query": "pixiv 插件"' in task
    assert "<调用意图>\n想找一个 pixiv 相关的插件\n</调用意图>" in task
    assert "<原始结果>\n" + "A" * 100 + "\n</原始结果>" in task
    assert "原始结果共 300 字" in task


def test_build_task_marks_missing_thought() -> None:
    task = build_task(
        "web_search",
        {},
        "short",
        "   ",
        max_input_chars=100,
        intent_max_chars=20,
    )
    assert "（决策模型没有写下想法）" in task


@pytest.mark.asyncio
async def test_run_condense_stashes_and_wraps_summary() -> None:
    store = ToolResultStore()
    outcome = await run_condense(
        tool_name="web_search",
        tool_args={"query": "x"},
        content="raw " * 100,
        intent="找插件",
        llm=_llm("结论：插件 A 最合适。"),
        store=store,
        session_id="aiocqhttp:GroupMessage:1",
        max_input_chars=20000,
        intent_max_chars=800,
        timeout_seconds=5,
    )

    assert outcome.condensed
    assert outcome.result_id == "r1"
    assert "压缩后信息" in outcome.content
    assert 'read_tool_result(result_id="r1")' in outcome.content
    assert outcome.content.endswith("结论：插件 A 最合适。")
    stored = store.get("aiocqhttp:GroupMessage:1", "r1")
    assert stored is not None
    assert stored.tool_name == "web_search"
    assert stored.chars == 400


@pytest.mark.asyncio
async def test_run_condense_keeps_raw_when_not_shorter() -> None:
    store = ToolResultStore()
    content = "short raw"
    outcome = await run_condense(
        tool_name="web_search",
        tool_args={},
        content=content,
        intent="",
        llm=_llm("A summary that is definitely longer than the raw text"),
        store=store,
        session_id="s",
        max_input_chars=20000,
        intent_max_chars=800,
        timeout_seconds=5,
    )

    assert not outcome.condensed
    assert outcome.content == content
    assert outcome.error == "not_shorter"
    assert store.stats()["results"] == 0


@pytest.mark.asyncio
async def test_run_condense_keeps_raw_on_exception() -> None:
    store = ToolResultStore()

    async def boom(system_prompt: str, user_prompt: str) -> str | None:
        raise RuntimeError("provider down")

    outcome = await run_condense(
        tool_name="web_search",
        tool_args={},
        content="x" * 50,
        intent="",
        llm=boom,
        store=store,
        session_id="s",
        max_input_chars=20000,
        intent_max_chars=800,
        timeout_seconds=5,
    )

    assert not outcome.condensed
    assert outcome.error == "exception"
    assert outcome.content == "x" * 50
    assert store.stats()["results"] == 0


@pytest.mark.asyncio
async def test_run_condense_keeps_raw_on_timeout() -> None:
    store = ToolResultStore()

    async def slow(system_prompt: str, user_prompt: str) -> str | None:
        await asyncio.sleep(0.3)
        return "late"

    outcome = await run_condense(
        tool_name="web_search",
        tool_args={},
        content="y" * 50,
        intent="",
        llm=slow,
        store=store,
        session_id="s",
        max_input_chars=20000,
        intent_max_chars=800,
        timeout_seconds=0.05,
    )

    assert not outcome.condensed
    assert outcome.error == "timeout"
    assert store.stats()["results"] == 0


def test_store_ids_increment_and_get() -> None:
    store = ToolResultStore()
    assert store.save("s1", "tool", "one") == "r1"
    assert store.save("s1", "tool", "two") == "r2"
    assert store.save("s2", "tool", "other") == "r1"
    assert store.get("s1", "r2").content == "two"  # ty: ignore[union-attr]
    assert store.get("s1", "r9") is None
    assert store.ids("s1") == ["r1", "r2"]


def test_store_evicts_oldest_result_per_session() -> None:
    store = ToolResultStore(max_results_per_session=2, max_chars_per_session=10_000)
    for index in range(3):
        store.save("s", "tool", f"text-{index}")
    assert store.ids("s") == ["r2", "r3"]


def test_store_drops_least_recently_used_session() -> None:
    store = ToolResultStore(max_sessions=2)
    store.save("a", "tool", "a")
    store.save("b", "tool", "b")
    assert store.get("a", "r1") is not None
    store.save("c", "tool", "c")
    assert store.ids("b") == []
    assert store.ids("a") == ["r1"]
    assert store.ids("c") == ["r1"]


def test_store_respects_character_budget_but_keeps_newest() -> None:
    store = ToolResultStore(max_results_per_session=10, max_chars_per_session=10)
    store.save("s", "tool", "x" * 8)
    store.save("s", "tool", "y" * 8)
    assert store.ids("s") == ["r2"]


def test_store_stats() -> None:
    store = ToolResultStore()
    store.save("a", "tool", "12345")
    store.save("b", "tool", "123")
    assert store.stats() == {"sessions": 2, "results": 2, "chars": 8}


def test_render_read_view_pages_with_offset() -> None:
    stored = StoredToolResult(
        "r1", "tool", "\n".join(f"line {i}" for i in range(1, 21))
    )
    view = render_read_view(stored, offset=5, limit=3, max_chars=500)
    assert "showing lines 5-7" in view
    assert "L5: line 5" in view
    assert "L7: line 7" in view
    assert "offset=8" in view


def test_render_read_view_wraps_single_long_line() -> None:
    content = "x" * 900 + "NEEDLE" + "y" * 100
    stored = StoredToolResult("r3", "tool", content)

    hit = render_read_view(stored, keyword="needle")
    assert "1 line(s) contain 'needle'" in hit
    assert "L3: " in hit
    assert "NEEDLE" in hit

    page = render_read_view(stored, offset=2, limit=1)
    assert "3 lines] showing lines 2-2" in page


def test_render_read_view_keyword_match_and_miss() -> None:
    stored = StoredToolResult("r2", "tool", "alpha\nbeta\ngamma alpha")
    hit = render_read_view(stored, keyword="ALPHA")
    assert "2 line(s) contain 'ALPHA'" in hit
    assert "L1: alpha" in hit
    assert "L3: gamma alpha" in hit

    miss = render_read_view(stored, keyword="zzz")
    assert "no line contains 'zzz'" in miss


@pytest.mark.asyncio
async def test_read_tool_result_unknown_id_lists_kept_ids() -> None:
    store = ToolResultStore()
    store.save("aiocqhttp:GroupMessage:1", "tool", "raw")
    manager = FakeManager()
    register_tool_result_tools(manager, _service(store))
    handler = manager.tools["read_tool_result"]

    answer = await handler(_event(), "r9")

    assert "no stored result 'r9'" in answer
    assert "r1" in answer


@pytest.mark.asyncio
async def test_read_tool_result_is_session_scoped() -> None:
    store = ToolResultStore()
    store.save("aiocqhttp:GroupMessage:1", "tool", "secret")
    manager = FakeManager()
    register_tool_result_tools(manager, _service(store))
    handler = manager.tools["read_tool_result"]

    answer = await handler(_event("aiocqhttp:GroupMessage:2"), "r1")

    assert "no stored result" in answer


@pytest.mark.asyncio
async def test_read_tool_result_respects_disabled_and_scope() -> None:
    store = ToolResultStore()
    store.save("aiocqhttp:GroupMessage:1", "tool", "raw")
    manager = FakeManager()
    register_tool_result_tools(manager, _service(store, enabled=False))
    handler = manager.tools["read_tool_result"]
    assert "not enabled" in await handler(_event(), "r1")

    manager = FakeManager()
    register_tool_result_tools(manager, _service(store, allowed=False))
    handler = manager.tools["read_tool_result"]
    assert "not enabled for this session" in await handler(_event(), "r1")


@pytest.mark.asyncio
async def test_register_and_unregister_tool_result_tools() -> None:
    manager = FakeManager()
    register_tool_result_tools(manager, _service(ToolResultStore()))
    assert "read_tool_result" in manager.tools
    unregister_tool_result_tools(manager)
    assert "read_tool_result" not in manager.tools


def _llm(text: str):
    async def call(system_prompt: str, user_prompt: str) -> str | None:
        return text

    return call


@pytest_asyncio.fixture
async def life_db(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    yield database
    await database.engine.dispose()


@pytest_asyncio.fixture
async def life_service(life_db, tmp_path):
    config = LifeConfig(enabled=True, tool_result=ToolResultConfig(enabled=True))
    config.scope.mode = "all"
    instance = LifeService(
        life_db,
        config=config,
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )
    await instance.initialize()
    yield instance
    await instance.stop()
    set_life_service(None)


@pytest.mark.asyncio
async def test_service_condense_returns_none_when_disabled(life_db, tmp_path) -> None:
    config = LifeConfig(enabled=True)
    config.scope.mode = "all"
    service = LifeService(
        life_db,
        config=config,
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )
    await service.initialize()
    try:
        answer = await service.condense_tool_result(
            tool_name="web_search",
            tool_args={},
            content="x" * 5000,
            intent="",
            session_id="aiocqhttp:GroupMessage:1",
        )
        assert answer is None
    finally:
        await service.stop()
        set_life_service(None)


@pytest.mark.asyncio
async def test_service_condense_condenses_and_counts(life_service) -> None:
    life_service._llm_summarizer = _llm("结论：只保留这一条。")

    answer = await life_service.condense_tool_result(
        tool_name="web_search",
        tool_args={"query": "x"},
        content="raw " * 1000,
        intent="找插件",
        session_id="aiocqhttp:GroupMessage:1",
    )

    assert answer is not None
    assert "压缩后信息" in answer
    assert life_service.tool_results.stats()["results"] == 1
    counters = life_service._tool_result_counts
    assert counters["attempts"] == 1
    assert counters["condensed"] == 1
    assert counters["chars_saved"] > 0


@pytest.mark.asyncio
async def test_service_condense_skips_outside_scope(life_db, tmp_path) -> None:
    config = LifeConfig(enabled=True, tool_result=ToolResultConfig(enabled=True))
    service = LifeService(
        life_db,
        config=config,
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )
    await service.initialize()
    life_service_llm_calls = 0

    async def counting_llm(system_prompt: str, user_prompt: str) -> str | None:
        nonlocal life_service_llm_calls
        life_service_llm_calls += 1
        return "summary"

    service._llm_summarizer = counting_llm
    try:
        answer = await service.condense_tool_result(
            tool_name="web_search",
            tool_args={},
            content="x" * 5000,
            intent="",
            session_id="aiocqhttp:GroupMessage:999",
        )
        assert answer is None
        assert life_service_llm_calls == 0
    finally:
        await service.stop()
        set_life_service(None)


@pytest.mark.asyncio
async def test_service_status_reports_tool_result(life_service) -> None:
    status = await life_service.status()
    block = status["tool_result"]
    assert block["enabled"] is True
    assert block["threshold_chars"] == 4000
    assert block["counters"]["condensed"] == 0
    assert block["store"] == {"sessions": 0, "results": 0, "chars": 0}
