"""Unit tests for focus storage: turns, digest, pruning and deletion."""

from __future__ import annotations

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.heartflow.focus.store import FocusStore

SESSION = "aiocqhttp:GroupMessage:1"
OTHER = "aiocqhttp:GroupMessage:2"


@pytest_asyncio.fixture
async def store(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    focus = FocusStore(database)
    await focus.ensure_schema()
    yield focus
    await database.engine.dispose()


def _turn(index: int, text: str = "一", status: str = "closed") -> dict:
    return {
        "index": index,
        "started_ts": float(index),
        "ended_ts": float(index) + 0.5,
        "status": status,
        "summary": "",
        "messages": [{"role": "user", "text": text, "ts": float(index)}],
    }


@pytest.mark.asyncio
async def test_ensure_schema_is_idempotent(store):
    await store.ensure_schema()
    assert await store.stats() == {"sessions": 0, "turns": 0, "digest_chars": 0}


@pytest.mark.asyncio
async def test_save_turn_upserts_by_index(store):
    await store.save_turn(SESSION, _turn(1, "一"))
    await store.save_turn(SESSION, _turn(1, "改过"))

    rows = await store.list_turns(SESSION)

    assert len(rows) == 1
    assert rows[0]["messages"][0]["text"] == "改过"


@pytest.mark.asyncio
async def test_load_returns_turns_oldest_first(store):
    for index in (1, 2, 3):
        await store.save_turn(SESSION, _turn(index))
    await store.save_head(
        SESSION,
        digest="摘要",
        compressed_turns=2,
        total_messages=7,
        last_turn_index=3,
    )

    payload = await store.load(SESSION)

    assert [turn["index"] for turn in payload["turns"]] == [1, 2, 3]
    assert payload["digest"] == "摘要"
    assert payload["compressed_turns"] == 2
    assert payload["total_messages"] == 7
    assert payload["next_index"] == 4


@pytest.mark.asyncio
async def test_load_missing_session_is_empty(store):
    payload = await store.load(SESSION)

    assert payload["turns"] == []
    assert payload["digest"] == ""
    assert payload["next_index"] == 1


@pytest.mark.asyncio
async def test_load_honours_the_limit(store):
    for index in range(1, 6):
        await store.save_turn(SESSION, _turn(index))

    payload = await store.load(SESSION, limit=2)

    assert [turn["index"] for turn in payload["turns"]] == [4, 5]
    assert payload["next_index"] == 6


@pytest.mark.asyncio
async def test_save_head_updates_existing_row(store):
    await store.save_head(
        SESSION,
        digest="一",
        compressed_turns=1,
        total_messages=1,
        last_turn_index=1,
    )
    await store.save_head(
        SESSION,
        digest="二",
        compressed_turns=2,
        total_messages=5,
        last_turn_index=4,
    )

    sessions = await store.list_sessions()
    stats = await store.stats()

    assert len(sessions) == 1
    assert sessions[0]["compressed_turns"] == 2
    assert stats["sessions"] == 1
    assert stats["digest_chars"] == len("二")


@pytest.mark.asyncio
async def test_prune_keeps_the_newest_turns(store):
    for index in range(1, 6):
        await store.save_turn(SESSION, _turn(index))

    deleted = await store.prune(SESSION, keep=2)

    assert deleted == 3
    rows = await store.list_turns(SESSION)
    assert [row["index"] for row in rows] == [5, 4]


@pytest.mark.asyncio
async def test_delete_removes_the_session(store):
    await store.save_turn(SESSION, _turn(1))
    await store.save_turn(SESSION, _turn(2))
    await store.save_head(
        SESSION,
        digest="摘要",
        compressed_turns=0,
        total_messages=2,
        last_turn_index=2,
    )

    deleted = await store.delete(SESSION)

    assert deleted == 2
    assert (await store.list_turns(SESSION)) == []
    assert (await store.load(SESSION))["digest"] == ""
    assert await store.stats() == {"sessions": 0, "turns": 0, "digest_chars": 0}


@pytest.mark.asyncio
async def test_sessions_are_isolated(store):
    await store.save_turn(SESSION, _turn(1, "这边的"))
    await store.save_turn(OTHER, _turn(1, "那边的"))

    first = await store.list_turns(SESSION)
    second = await store.list_turns(OTHER)

    assert first[0]["messages"][0]["text"] == "这边的"
    assert second[0]["messages"][0]["text"] == "那边的"
