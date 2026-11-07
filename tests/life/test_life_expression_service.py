"""Unit tests for the expression service: selection, injection and learning."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import ExpressionConfig, LifeConfig, MemoryEmbeddingConfig
from bot.life.expression.service import ExpressionService

SESSION = "aiocqhttp:GroupMessage:1"


class FakeChatProvider:
    async def text_chat(self, prompt=None, system_prompt=None):
        class Response:
            completion_text = "[]"

        return Response()


class FakeManager:
    def __init__(self) -> None:
        self.provider = FakeChatProvider()

    async def get_using_provider_async(self, _provider_type):
        return self.provider


class FakeContext:
    def __init__(self) -> None:
        self.provider_manager = FakeManager()


class FakeEvent:
    def __init__(self, umo: str = SESSION, group_id: str | None = "1") -> None:
        self.unified_msg_origin = umo
        self._group_id = group_id

    def get_group_id(self):
        return self._group_id


class FakeRequest:
    def __init__(self, prompt: str = "这个 bug 怎么又出现了") -> None:
        self.prompt = prompt
        self.system_prompt = "You are a group member."


class FakeSource:
    def __init__(self, messages):
        self.messages = messages

    async def fetch_range(self, session_key, since_ts=0, until_ts=None, limit=200):
        return [row for row in self.messages if row["ts"] > since_ts][:limit]

    async def active_sessions(self, since_ts=0, limit=20):
        return sorted({row["session_key"] for row in self.messages})


async def _seed(store, style: str, situation: str, weight: float = 1.0) -> None:
    await store.upsert_many(
        [
            {
                "session_key": SESSION,
                "situation": situation,
                "style": style,
                "scene": "group",
                "weight": weight,
                "source": "heuristic",
            }
        ]
    )


@pytest_asyncio.fixture
async def service(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    life_config = LifeConfig(enabled=True)
    life_config.scope.mode = "all"
    instance = ExpressionService(
        context=FakeContext(),
        config=ExpressionConfig(enabled=True, use_llm=False),
        db_helper=database,
        data_dir=tmp_path,
        life_store=None,
        life_config=life_config,
        embedding_config=MemoryEmbeddingConfig(enabled=False),
    )
    await instance.initialize()
    yield instance
    await instance.close()
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_initializes_without_vector_index(service):
    status = await service.status()

    assert status["enabled"] is True
    assert status["index"] is None
    assert status["storage"]["total"] == 0


@pytest.mark.asyncio
async def test_inject_selects_a_matching_expression(service):
    await service.store.upsert_many(
        [
            {
                "session_key": SESSION,
                "situation": "bug 又出现了",
                "style": "笑死",
                "scene": "group",
                "weight": 3.0,
                "source": "heuristic",
            },
            {
                "session_key": SESSION,
                "situation": "有人请假",
                "style": "准了",
                "scene": "group",
                "weight": 1.0,
                "source": "heuristic",
            },
        ]
    )
    req = FakeRequest(prompt="这个 bug 又出现了 怎么处理")

    first = await service.inject(FakeEvent(), req)
    second = await service.inject(FakeEvent(), req)

    assert first is True
    assert second is False
    assert "<life_expression>" in req.system_prompt
    assert "笑死" in req.system_prompt
    assert req.system_prompt.count("<life_expression>") == 1
    rows = await service.store.list_expressions(SESSION)
    used = next(row for row in rows if row["style"] == "笑死")
    assert used["usage_count"] == 1


@pytest.mark.asyncio
async def test_inject_respects_scope_scene_and_empty_prompt(service):
    await _seed(service.store, "笑死", "bug 又出现了", weight=5.0)

    service.life_config.scope.mode = "allowlist"
    service.life_config.scope.enabled_umos = ["aiocqhttp:GroupMessage:other"]
    blocked = FakeRequest(prompt="这个 bug 又出现了")
    assert await service.inject(FakeEvent(), blocked) is False
    assert "<life_expression>" not in blocked.system_prompt

    service.life_config.scope.enabled_umos = [SESSION]
    service.config.scene = "group"
    private = FakeRequest(prompt="这个 bug 又出现了")
    assert await service.inject(FakeEvent(group_id=None), private) is False

    empty = FakeRequest(prompt="")
    assert await service.inject(FakeEvent(), empty) is False


@pytest.mark.asyncio
async def test_low_score_expressions_are_not_injected(service):
    await _seed(service.store, "笑死", "完全无关的场合", weight=1.0)
    service.config.min_score = 0.5

    req = FakeRequest(prompt="今天吃什么")

    assert await service.inject(FakeEvent(), req) is False


@pytest.mark.asyncio
async def test_learn_round_reads_the_archive(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "learn.db"))
    await database.initialize()
    now = int(time.time())
    messages = [
        {
            "session_key": SESSION,
            "plain_text": "笑死 这也能过",
            "ts": now - 10,
            "group_id": "1",
            "sender_name": "Alice",
        },
        {
            "session_key": SESSION,
            "plain_text": "笑死 真的离谱",
            "ts": now,
            "group_id": "1",
            "sender_name": "Bob",
        },
    ]
    service = ExpressionService(
        context=FakeContext(),
        config=ExpressionConfig(enabled=True, use_llm=False, min_frequency=2),
        db_helper=database,
        data_dir=tmp_path,
        life_store=FakeSource(messages),
        life_config=LifeConfig(enabled=True),
        embedding_config=MemoryEmbeddingConfig(enabled=False),
    )
    await service.initialize()
    try:
        result = await service.learn_round([SESSION])
        rows = await service.list_expressions(SESSION)
    finally:
        await service.close()
        await database.engine.dispose()

    assert result["inserted"] >= 1
    assert any(row["style"] == "笑死" for row in rows)


@pytest.mark.asyncio
async def test_forget_deletes_expressions(service):
    await _seed(service.store, "笑死", "bug 又出现了")

    deleted = await service.forget(session_key=SESSION)

    assert deleted == 1
    assert await service.list_expressions(SESSION) == []
