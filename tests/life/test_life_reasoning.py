"""Unit tests for reply reasoning records (MaiBot 1.4 reasoning log)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.core.message.components import Plain
from bot.life.config import ReasoningConfig, ReplyConfig
from bot.life.reasoning import ReasoningRecorder
from bot.life.reply.postprocess import ReplyPostProcessor


class _Trace:
    def __init__(self, trace_id: str = "trace-1") -> None:
        self.trace_id = trace_id


class _Event:
    def __init__(self, trace_id: str = "trace-1") -> None:
        self.unified_msg_origin = "aiocqhttp:GroupMessage:1"
        self.trace = _Trace(trace_id)

    def get_extra(self, key: str, default=None):
        return default

    def get_group_id(self):
        return "1"

    def get_platform_name(self) -> str:
        return "aiocqhttp"


class _ToolSet:
    def names(self) -> list[str]:
        return ["web_search", "execute_shell"]


class _Req:
    def __init__(self) -> None:
        self.system_prompt = "SYSTEM " * 20
        self.prompt = "hello"
        self.model = "test-model"
        self.func_tool = _ToolSet()


@pytest_asyncio.fixture
async def recorder(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    instance = ReasoningRecorder(
        database,
        ReasoningConfig(enabled=True, max_chars=32, retention_days=1),
    )
    await instance.ensure_schema()
    yield instance, database
    await database.engine.dispose()


def test_reasoning_config_defaults_are_off() -> None:
    config = ReasoningConfig()

    assert config.enabled is False
    assert config.max_chars == 4000
    assert config.retention_days == 7


@pytest.mark.asyncio
async def test_recorder_round_trip(recorder) -> None:
    store, _ = recorder
    event = _Event()

    await store.remember_request(event, _Req())
    await store.remember_reply(event, "你好呀")

    items = await store.list_recent()
    assert len(items) == 1
    summary = items[0]
    assert summary["trace_id"] == "trace-1"
    assert summary["session_key"] == "aiocqhttp:GroupMessage:1"
    assert summary["tools"] == ["web_search", "execute_shell"]
    assert summary["reply_chars"] == 3

    full = await store.get(summary["id"])
    assert full is not None
    assert full["request_prompt"] == "hello"
    assert full["reply_text"] == "你好呀"


@pytest.mark.asyncio
async def test_recorder_updates_instead_of_duplicating(recorder) -> None:
    store, _ = recorder
    event = _Event()

    await store.remember_request(event, _Req())
    await store.remember_reply(event, "第一版")
    await store.remember_request(event, _Req())
    await store.remember_reply(event, "第二版")

    items = await store.list_recent()
    assert len(items) == 1
    full = await store.get(items[0]["id"])
    assert full is not None
    assert full["reply_text"] == "第二版"


@pytest.mark.asyncio
async def test_recorder_bounds_long_fields(recorder) -> None:
    store, _ = recorder
    event = _Event()

    await store.remember_request(event, _Req())
    await store.remember_reply(event, "长" * 100)

    full = await store.get((await store.list_recent())[0]["id"])
    assert full is not None
    assert len(full["request_system"]) == 32
    assert len(full["reply_text"]) == 32


@pytest.mark.asyncio
async def test_recorder_prunes_expired_rows(recorder) -> None:
    store, database = recorder
    await store.remember_request(_Event(), _Req())
    async with database.get_db() as session:
        from sqlmodel import select

        from bot.life.reasoning import LifeReasoning

        row = (
            await session.execute(select(LifeReasoning))
        ).scalars().one()
        row.created_at = int(time.time()) - 3 * 86400
        await session.commit()

    removed = await store.prune()

    assert removed == 1
    assert await store.list_recent() == []


@pytest.mark.asyncio
async def test_inject_captures_the_request(monkeypatch) -> None:
    import bot.life.memory.inject as inject_module

    captured: list[tuple] = []

    class _Recorder:
        async def remember_request(self, event, req) -> None:
            captured.append((event, req))

    service = SimpleNamespace(reasoning=_Recorder())
    monkeypatch.setattr(inject_module, "get_life_service", lambda: service)
    request = SimpleNamespace(system_prompt="SYS")

    await inject_module.inject_life_context(_Event(), request)

    assert len(captured) == 1


@pytest.mark.asyncio
async def test_postprocess_captures_the_final_reply() -> None:
    bodies: list[str] = []

    class _Recorder:
        async def remember_reply(self, event, text) -> None:
            bodies.append(text)

    processor = ReplyPostProcessor(
        ReplyConfig(enabled=True),
        persona=None,
        reasoning=_Recorder(),
    )
    # A persona is required to reach the rewrite path; keep it minimal.
    processor.persona = SimpleNamespace(
        profile_for=_profile,
    )

    class _Result:
        chain = [Plain("你好")]

        def is_llm_result(self) -> bool:
            return True

    await processor.process(_Event(), _Result())

    assert bodies == ["你好"]


async def _profile(_persona_id: str) -> dict:
    return {}
