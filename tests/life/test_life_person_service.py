"""Unit tests for the person service: observation, facts, profiles, delete."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import LifeConfig, PersonConfig
from bot.life.person.service import PersonService, infer_category

SESSION = "aiocqhttp:GroupMessage:1"


class FakeChatProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def text_chat(self, prompt=None, system_prompt=None):
        self.calls += 1

        class Response:
            completion_text = "Alice 是个喜欢猫、住在杭州的工程师。"

        return Response()


class FakeManager:
    def __init__(self) -> None:
        self.provider = FakeChatProvider()

    async def get_using_provider_async(self, _provider_type):
        return self.provider


class FakeContext:
    def __init__(self) -> None:
        self.provider_manager = FakeManager()


def _message(sender_id: str, name: str, ts: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        session_key=SESSION,
        platform_id="aiocqhttp",
        sender_id=sender_id,
        sender_name=name,
        group_id="1",
        ts=ts if ts is not None else int(time.time()),
    )


@pytest_asyncio.fixture
async def service(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    life_config = LifeConfig(enabled=True)
    life_config.scope.mode = "all"
    instance = PersonService(
        context=FakeContext(),
        config=PersonConfig(enabled=True, profile_ttl_minutes=60),
        db_helper=database,
        life_config=life_config,
    )
    await instance.initialize()
    yield instance
    await instance.close()
    await database.engine.dispose()


def test_infer_category_uses_hints():
    assert infer_category("用户喜欢吃辣") == "preference"
    assert infer_category("用户是后端工程师") == "identity"
    assert infer_category("用户和小王是同事") == "relation"
    assert infer_category("用户在做一个开源项目") == "other"


@pytest.mark.asyncio
async def test_observe_messages_groups_by_sender(service):
    now = int(time.time())
    written = await service.observe_messages(
        [
            _message("42", "Alice", now),
            _message("42", "Alice", now + 1),
            _message("43", "Bob", now + 2),
        ]
    )

    assert written == 2
    alice = await service.resolve(SESSION, "Alice")
    assert alice["interactions"] == 2
    bob = await service.resolve(SESSION, "Bob")
    assert bob["interactions"] == 1


@pytest.mark.asyncio
async def test_ingest_facts_creates_unknown_people(service):
    await service.observe_messages([_message("42", "Alice")])

    result = await service.ingest_facts(
        SESSION,
        [
            {"person_id": "Alice", "content": "Alice 喜欢猫", "importance": 0.7},
            {"person_id": "Carol", "content": "Carol 是设计师", "importance": 0.6},
            {"person_id": "", "content": "无人称的事实"},
        ],
    )

    assert result["inserted"] == 2
    alice = await service.resolve(SESSION, "Alice")
    carol = await service.resolve(SESSION, "Carol")
    assert carol is not None and carol["display_name"] == "Carol"
    facts = await service.store.list_facts(SESSION, alice["person_id"])
    assert facts[0]["content"] == "Alice 喜欢猫"
    assert facts[0]["category"] == "preference"


@pytest.mark.asyncio
async def test_profile_uses_llm_then_cache(service):
    await service.observe_messages([_message("42", "Alice")])
    await service.ingest_facts(
        SESSION,
        [{"person_id": "Alice", "content": "Alice 喜欢猫", "importance": 0.8}],
    )

    first = await service.profile(SESSION, "Alice")
    second = await service.profile(SESSION, "Alice")

    assert first["generator"] == "llm"
    assert "工程师" in first["summary"]
    assert first["fact_ids"] == second["fact_ids"]
    assert service.context.provider_manager.provider.calls == 1
    assert first["display_name"] == "Alice"
    assert first["relation_score"] >= 1.0


@pytest.mark.asyncio
async def test_profile_refresh_and_extractive_fallback(service):
    service.config.use_llm_profile = False
    await service.observe_messages([_message("42", "Alice")])
    await service.ingest_facts(
        SESSION,
        [{"person_id": "Alice", "content": "Alice 住在杭州", "importance": 0.8}],
    )

    profile = await service.profile(SESSION, "Alice", refresh=True)

    assert profile["generator"] == "extractive"
    assert "杭州" in profile["summary"]
    assert profile["highlight"]


@pytest.mark.asyncio
async def test_profile_of_unknown_person_is_none(service):
    assert await service.profile(SESSION, "Nobody") is None


@pytest.mark.asyncio
async def test_forget_fact_and_person(service):
    await service.observe_messages([_message("42", "Alice")])
    await service.ingest_facts(
        SESSION,
        [{"person_id": "Alice", "content": "Alice 喜欢猫", "importance": 0.8}],
    )
    alice = await service.resolve(SESSION, "Alice")
    facts = await service.store.list_facts(SESSION, alice["person_id"])

    by_fact = await service.forget(SESSION, "Alice", fact_id=int(facts[0]["id"]))
    assert by_fact["facts"] == 1

    whole = await service.forget(SESSION, "Alice")
    assert whole["people"] == 1
    assert await service.resolve(SESSION, "Alice") is None


@pytest.mark.asyncio
async def test_scope_blocks_other_sessions(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "scoped.db"))
    await database.initialize()
    life_config = LifeConfig(enabled=True)
    life_config.scope.enabled_umos = ["aiocqhttp:GroupMessage:other"]
    instance = PersonService(
        context=FakeContext(),
        config=PersonConfig(enabled=True),
        db_helper=database,
        life_config=life_config,
    )
    await instance.initialize()
    try:
        written = await instance.observe_messages([_message("42", "Alice")])
        assert written == 0
    finally:
        await instance.close()
        await database.engine.dispose()
