"""Unit tests for the jargon service: contextual explanation and review."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import JargonConfig, LifeConfig
from bot.life.jargon.service import JargonService

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
    def __init__(self, prompt: str = "绷不住 这也太离谱了") -> None:
        self.prompt = prompt
        self.system_prompt = "You are a group member."


class FakeSource:
    def __init__(self, messages):
        self.messages = messages

    async def fetch_range(self, session_key, since_ts=0, until_ts=None, limit=200):
        return [row for row in self.messages if row["ts"] > since_ts][:limit]

    async def active_sessions(self, since_ts=0, limit=20):
        return sorted({row["session_key"] for row in self.messages})


async def _seed(
    store, term: str, meaning: str, status: str = "active", confidence: float = 0.75
) -> None:
    await store.upsert_many(
        [
            {
                "session_key": SESSION,
                "term": term,
                "meaning": meaning,
                "confidence": confidence,
                "status": status,
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
    instance = JargonService(
        context=FakeContext(),
        config=JargonConfig(enabled=True, use_llm=False),
        db_helper=database,
        life_store=None,
        life_config=life_config,
    )
    await instance.initialize()
    yield instance
    await instance.close()
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_explains_only_terms_from_the_prompt(service):
    await _seed(service.store, "绷不住", "想笑但憋着")
    await _seed(service.store, "典", "形容事情很典型")

    req = FakeRequest(prompt="绷不住 这也太离谱了")
    changed = await service.inject(FakeEvent(), req)

    assert changed is True
    assert "绷不住" in req.system_prompt
    assert "典" not in req.system_prompt
    assert "<life_jargon>" in req.system_prompt


@pytest.mark.asyncio
async def test_does_not_inject_when_no_term_matches(service):
    await _seed(service.store, "绷不住", "想笑但憋着")

    req = FakeRequest(prompt="今天中午吃什么")

    assert await service.inject(FakeEvent(), req) is False
    assert "<life_jargon>" not in req.system_prompt


@pytest.mark.asyncio
async def test_skips_pending_low_confidence_and_duplicate_blocks(service):
    await _seed(service.store, "待定", "还没确定", status="pending")
    await _seed(service.store, "低分", "置信度太低", confidence=0.1)
    await _seed(service.store, "绷不住", "想笑但憋着")

    req = FakeRequest(prompt="待定 低分 绷不住")
    changed = await service.inject(FakeEvent(), req)
    second = await service.inject(FakeEvent(), req)

    assert changed is True
    assert "待定" not in req.system_prompt
    assert "低分" not in req.system_prompt
    assert second is False
    assert req.system_prompt.count("<life_jargon>") == 1


@pytest.mark.asyncio
async def test_respects_scope_and_empty_prompt(service):
    await _seed(service.store, "绷不住", "想笑但憋着")
    service.life_config.scope.mode = "allowlist"
    service.life_config.scope.enabled_umos = ["aiocqhttp:GroupMessage:other"]

    blocked = FakeRequest(prompt="绷不住")
    assert await service.inject(FakeEvent(), blocked) is False

    service.life_config.scope.enabled_umos = [SESSION]
    empty = FakeRequest(prompt="")
    assert await service.inject(FakeEvent(), empty) is False


@pytest.mark.asyncio
async def test_review_activates_pending_term(service):
    await _seed(service.store, "典", "很典型")
    await service.store.upsert_many(
        [
            {
                "session_key": SESSION,
                "term": "典",
                "meaning": "很搞笑",
                "confidence": 0.75,
                "source": "llm",
            }
        ]
    )
    row = await service.store.get_by_term(SESSION, "典")
    assert row["status"] == "pending"

    updated = await service.review(int(row["id"]), "active", "形容事情很典型")

    assert updated is True
    refreshed = await service.store.get_by_term(SESSION, "典")
    assert refreshed["status"] == "active"
    assert refreshed["meaning"] == "形容事情很典型"


@pytest.mark.asyncio
async def test_learn_round_reads_archive(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "learn.db"))
    await database.initialize()
    now = int(time.time())
    messages = [
        {
            "session_key": SESSION,
            "plain_text": "绷不住 就是想笑但憋着",
            "ts": now,
            "group_id": "1",
            "sender_name": "Alice",
        }
    ]
    service = JargonService(
        context=FakeContext(),
        config=JargonConfig(enabled=True, use_llm=False),
        db_helper=database,
        life_store=FakeSource(messages),
        life_config=LifeConfig(enabled=True),
    )
    await service.initialize()
    try:
        result = await service.learn_round([SESSION])
        rows = await service.list_jargon(SESSION)
    finally:
        await service.close()
        await database.engine.dispose()

    assert result["inserted"] == 1
    assert rows[0]["term"] == "绷不住"


@pytest.mark.asyncio
async def test_forget_and_status(service):
    await _seed(service.store, "绷不住", "想笑但憋着")

    deleted = await service.forget(session_key=SESSION)
    status = await service.status()

    assert deleted == 1
    assert status["enabled"] is True
    assert status["storage"]["total"] == 0
