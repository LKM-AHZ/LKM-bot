"""Unit tests for the person store: identity, facts, conflicts and decay."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import PersonConfig
from bot.life.person.names import clean_name, derive_person_id, normalize_name
from bot.life.person.store import PersonStore

SESSION = "aiocqhttp:GroupMessage:1"


@pytest_asyncio.fixture
async def store(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    person = PersonStore(database, PersonConfig(relation_increment=1.0))
    await person.ensure_schema()
    yield person
    await database.engine.dispose()


def _observation(person_id: str, name: str, ts: int, count: int = 1) -> dict:
    return {
        "session_key": SESSION,
        "person_id": person_id,
        "display_name": name,
        "group_id": "1",
        "ts": ts,
        "count": count,
    }


def test_name_normalization_strips_decorations_and_width():
    assert clean_name("  Alice  （管理） ") == "Alice"
    assert clean_name("小\u200b明") == "小明"
    assert normalize_name("Ａｌｉｃｅ") == "alice"
    assert derive_person_id("aiocqhttp", "42") == "aiocqhttp:42"
    assert derive_person_id("aiocqhttp", None, "小明").startswith("aiocqhttp:name:")


@pytest.mark.asyncio
async def test_observe_creates_person_and_collects_aliases(store):
    now = int(time.time())
    await store.observe_batch([_observation("aiocqhttp:42", "Alice", now, count=3)])

    person = await store.get(SESSION, "aiocqhttp:42")
    assert person is not None
    assert person["display_name"] == "Alice"
    assert person["interactions"] == 3
    assert person["relation_score"] == 3.0

    # Decorations are stripped, so a group card rename keeps the clean name.
    await store.observe_batch(
        [_observation("aiocqhttp:42", "Alice（管理）", now + 10, count=1)]
    )
    unchanged = await store.get(SESSION, "aiocqhttp:42")
    assert unchanged["display_name"] == "Alice"
    assert unchanged["aliases"] == ["Alice"]

    # A real rename is kept as an alias and becomes the new display name.
    await store.observe_batch(
        [_observation("aiocqhttp:42", "Alice Smith", now + 20, count=1)]
    )

    updated = await store.get(SESSION, "aiocqhttp:42")
    assert updated["display_name"] == "Alice Smith"
    assert updated["aliases"] == ["Alice", "Alice Smith"]
    assert updated["interactions"] == 5


@pytest.mark.asyncio
async def test_relation_score_decays_with_idle_time(store):
    now = int(time.time())
    await store.observe_batch(
        [_observation("aiocqhttp:42", "Alice", now - 60 * 86400, count=10)]
    )

    await store.observe_batch([_observation("aiocqhttp:42", "Alice", now, count=1)])

    person = await store.get(SESSION, "aiocqhttp:42")
    # 10 * 0.5^2 = 2.5, plus the new message.
    assert person["relation_score"] == pytest.approx(3.5, abs=0.01)


@pytest.mark.asyncio
async def test_find_by_name_alias_and_id(store):
    await store.observe_batch(
        [_observation("aiocqhttp:42", "Alice", int(time.time()), count=5)]
    )

    assert (await store.find(SESSION, "Alice"))["person_id"] == "aiocqhttp:42"
    assert (await store.find(SESSION, "alice"))["person_id"] == "aiocqhttp:42"
    assert (await store.find(SESSION, "aiocqhttp:42"))["person_id"] == "aiocqhttp:42"
    assert (await store.find(SESSION, "nobody")) is None


@pytest.mark.asyncio
async def test_similar_facts_conflict_and_can_be_resolved(store):
    base = {
        "session_key": SESSION,
        "person_id": "aiocqhttp:42",
        "category": "preference",
        "confidence": 0.6,
    }

    first = await store.add_facts([{**base, "content": "Alice 喜欢猫"}])
    second = await store.add_facts([{**base, "content": "Alice 喜欢狗"}])

    assert first == {"inserted": 1, "conflicts": 0}
    assert second == {"inserted": 1, "conflicts": 1}
    facts = await store.list_facts(SESSION, "aiocqhttp:42", statuses=["conflicted"])
    assert len(facts) == 2
    kept = await store.resolve_fact(int(facts[0]["id"]), keep=True)
    assert kept == 2
    active = await store.list_facts(SESSION, "aiocqhttp:42")
    assert len(active) == 1


@pytest.mark.asyncio
async def test_unrelated_facts_stay_active_and_dedupe(store):
    base = {
        "session_key": SESSION,
        "person_id": "aiocqhttp:42",
        "category": "identity",
        "confidence": 0.6,
    }

    await store.add_facts([{**base, "content": "Alice 住在杭州"}])
    duplicate = await store.add_facts([{**base, "content": "Alice 住在杭州"}])
    third = await store.add_facts([{**base, "content": "Alice 负责记忆模块"}])

    assert duplicate == {"inserted": 0, "conflicts": 0}
    assert third == {"inserted": 1, "conflicts": 0}
    stats = await store.stats()
    assert stats["facts"] == 2
    assert stats["conflicts"] == 0


@pytest.mark.asyncio
async def test_facts_decay_and_expire(store):
    store.config.min_confidence = 0.99
    await store.add_facts(
        [
            {
                "session_key": SESSION,
                "person_id": "aiocqhttp:42",
                "content": "Alice 喜欢咖啡",
                "category": "preference",
                "confidence": 0.6,
            }
        ]
    )
    facts = await store.list_facts(SESSION, "aiocqhttp:42")
    assert facts[0]["effective_confidence"] < 1.0

    expired = await store.decay()

    assert expired == 1
    assert await store.stats() == {
        "people": 0,
        "facts": 1,
        "profiles": 0,
        "conflicts": 0,
        "facts_by_status": {"expired": 1},
    }


@pytest.mark.asyncio
async def test_profile_cache_and_person_delete(store):
    await store.observe_batch([_observation("aiocqhttp:42", "Alice", int(time.time()))])
    await store.set_profile(
        SESSION,
        "aiocqhttp:42",
        "Alice 喜欢猫。",
        ["Alice 喜欢猫"],
        [1],
        "extractive",
    )

    profile = await store.get_profile(SESSION, "aiocqhttp:42")

    assert profile["summary"] == "Alice 喜欢猫。"
    assert profile["generator"] == "extractive"

    deleted = await store.delete_person(SESSION, "aiocqhttp:42")

    assert deleted == {"people": 1, "facts": 0, "profiles": 1}
    assert await store.get(SESSION, "aiocqhttp:42") is None
