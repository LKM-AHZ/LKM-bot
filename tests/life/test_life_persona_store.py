"""Unit tests for persona profile storage: upsert, clamping and deletion."""

from __future__ import annotations

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import PersonaConfig
from bot.life.persona.store import PersonaStore

PERSONA = "persona-a"


@pytest_asyncio.fixture
async def store(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    persona = PersonaStore(database, PersonaConfig(enabled=True))
    await persona.ensure_schema()
    yield persona
    await database.engine.dispose()


def _payload(**overrides) -> dict:
    return {
        "persona_id": PERSONA,
        "display_name": "小助手",
        "base_prompt": "说话随意一点。",
        "group_prompt": "群里别太热情。",
        "private_prompt": "私聊可以黏人一点。",
        "quote_rate": 0.3,
        "multi_segment_prob": 0.2,
        "typo_rate": 0.01,
        "reply_max_chars": 80,
        **overrides,
    }


@pytest.mark.asyncio
async def test_upsert_creates_and_reads_back(store):
    row = await store.upsert(_payload())

    assert row["persona_id"] == PERSONA
    assert row["group_prompt"] == "群里别太热情。"
    assert row["quote_rate"] == pytest.approx(0.3)
    assert await store.get("missing") is None


@pytest.mark.asyncio
async def test_upsert_replaces_the_same_row(store):
    await store.upsert(_payload())
    row = await store.upsert(_payload(reply_max_chars=40, quote_rate=0.0))

    assert row["reply_max_chars"] == 40
    assert row["quote_rate"] == 0.0
    assert (await store.stats())["total"] == 1


@pytest.mark.asyncio
async def test_rates_are_clamped_to_unit_interval(store):
    row = await store.upsert(_payload(quote_rate=3.5, multi_segment_prob=-1))

    assert row["quote_rate"] == 1.0
    assert row["multi_segment_prob"] == 0.0
    assert row["typo_rate"] == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_non_numeric_rate_falls_back_to_default(store):
    row = await store.upsert(_payload(typo_rate="oops"))

    assert row["typo_rate"] == pytest.approx(PersonaConfig().default_typo_rate)


@pytest.mark.asyncio
async def test_delete_reports_whether_a_row_existed(store):
    await store.upsert(_payload())

    assert await store.delete(PERSONA) is True
    assert await store.delete(PERSONA) is False
    assert (await store.stats())["total"] == 0


@pytest.mark.asyncio
async def test_empty_persona_id_is_the_fallback_profile(store):
    row = await store.upsert({"persona_id": "", "base_prompt": "默认人设"})

    assert row["persona_id"] == ""
    assert (await store.get(""))["base_prompt"] == "默认人设"


@pytest.mark.asyncio
async def test_upsert_requires_a_persona_id(store):
    with pytest.raises(ValueError):
        await store.upsert({"base_prompt": "x"})


@pytest.mark.asyncio
async def test_list_profiles_returns_newest_first(store):
    await store.upsert(_payload())
    await store.upsert({"persona_id": "persona-b", "display_name": "第二个"})

    rows = await store.list_profiles()

    assert [row["persona_id"] for row in rows][0] == "persona-b"
