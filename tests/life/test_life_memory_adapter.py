"""Unit tests for the memory adapter: write, search and prompt injection."""

from __future__ import annotations

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import MemoryConfig
from bot.life.memory.adapter import MemoryAdapter

SESSION = "aiocqhttp:GroupMessage:1"


class FakeProvider:
    async def get_embeddings(self, texts):
        return [[1.0, float(index), 0.5] for index, _ in enumerate(texts)]

    def get_model(self):
        return "fake-embedding"


class FakeManager:
    async def get_using_provider_async(self, _provider_type):
        return FakeProvider()

    async def get_provider_by_id(self, _provider_id):
        return FakeProvider()


class FakeContext:
    def __init__(self) -> None:
        self.provider_manager = FakeManager()


class FakeEvent:
    def __init__(self, umo: str = SESSION, group_id: str = "1") -> None:
        self.unified_msg_origin = umo
        self._group_id = group_id

    def get_group_id(self):
        return self._group_id


class FakeRequest:
    def __init__(self, prompt: str = "我们之前聊过什么？") -> None:
        self.prompt = prompt
        self.system_prompt = "You are a helpful assistant."


@pytest_asyncio.fixture
async def adapter(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    config = MemoryConfig(enabled=True)
    memory = MemoryAdapter(
        context=FakeContext(),
        config=config,
        db_helper=database,
        data_dir=tmp_path,
    )
    await memory.initialize()
    yield memory
    await memory.close()
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_runtime_is_off_until_initialized(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "off.db"))
    await database.initialize()
    memory = MemoryAdapter(
        context=FakeContext(),
        config=MemoryConfig(enabled=True),
        db_helper=database,
        data_dir=tmp_path,
    )

    assert await memory.search("hello", SESSION) == []
    assert await memory.remember("hello", SESSION) == 0
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_remember_writes_the_row_and_a_vector(adapter):
    inserted = await adapter.remember("用户喜欢喝美式咖啡", SESSION, kind="note")

    assert inserted == 1
    status = await adapter.status()
    assert status["storage"]["total"] == 1
    assert status["storage"]["embedded"] == 1
    assert status["engine"] == "local"


@pytest.mark.asyncio
async def test_inject_appends_memory_block_once(adapter):
    await adapter.remember("用户在迁移 MaiBot 的记忆层", SESSION, kind="note")
    event = FakeEvent()
    req = FakeRequest(prompt="迁移记忆层进度如何")

    first = await adapter.inject(event, req)
    second = await adapter.inject(event, req)

    assert first is True
    assert second is False
    assert "<life_memory>" in req.system_prompt
    assert "迁移" in req.system_prompt
    assert req.system_prompt.count("<life_memory>") == 1


@pytest.mark.asyncio
async def test_inject_respects_scope_and_empty_prompt(adapter):
    adapter._scope_check = lambda umo, group_id: False
    await adapter.remember("只有启用会话才可见", SESSION, kind="note")
    req = FakeRequest(prompt="启用会话")
    assert await adapter.inject(FakeEvent(), req) is False
    assert "<life_memory>" not in req.system_prompt

    adapter._scope_check = lambda umo, group_id: True
    empty = FakeRequest(prompt="")
    assert await adapter.inject(FakeEvent(), empty) is False


@pytest.mark.asyncio
async def test_inject_skips_low_score_memories(adapter):
    config = adapter.config.inject
    config.min_score = 2.0
    await adapter.remember("完全不相关的记忆内容", SESSION, kind="note")

    req = FakeRequest(prompt="今天天气如何")

    assert await adapter.inject(FakeEvent(), req) is False


@pytest.mark.asyncio
async def test_export_and_delete(adapter):
    await adapter.remember("first", SESSION, kind="note")
    await adapter.remember("second", SESSION, kind="note")

    exported = await adapter.export(SESSION)
    deleted = await adapter.delete(session_key=SESSION)

    assert len(exported) == 2
    assert deleted == 2
    assert await adapter.export(SESSION) == []
