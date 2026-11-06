"""Unit tests for long-term memory storage and retrieval."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.memory.store import MemoryStore, tokenize

SESSION = "aiocqhttp:GroupMessage:1"


@pytest_asyncio.fixture
async def store(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    memory = MemoryStore(database)
    await memory.ensure_schema()
    yield memory
    await database.engine.dispose()


def _row(content: str, kind: str = "summary", **overrides) -> dict:
    now = int(time.time())
    return {
        "kind": kind,
        "session_key": SESSION,
        "content": content,
        "ts": now,
        **overrides,
    }


def test_tokenize_uses_cjk_bigrams_and_words():
    tokens = tokenize("迁移 MaiBot 记忆层 memory")

    assert "maibot" in tokens
    assert "memory" in tokens
    assert "迁移" in tokens


@pytest.mark.asyncio
async def test_insert_is_idempotent(store):
    first = await store.insert_many([_row("用户喜欢喝美式咖啡")])
    second = await store.insert_many([_row("用户喜欢喝美式咖啡")])

    assert first == 1
    assert second == 0
    stats = await store.stats()
    assert stats["total"] == 1
    assert stats["by_kind"]["summary"] == 1


@pytest.mark.asyncio
async def test_keyword_hybrid_search_ranks_matching_memory(store):
    await store.insert_many(
        [
            _row("用户喜欢吃火锅", ts=1000),
            _row("用户在做一个机器人项目", ts=2000),
            _row("用户讨厌香菜", ts=3000),
        ]
    )

    hits = await store.search(SESSION, query="机器人项目", mode="hybrid", limit=2)

    assert hits
    assert "机器人" in hits[0]["content"]
    assert hits[0]["score"] > 0


@pytest.mark.asyncio
async def test_vector_search_uses_stored_embeddings(store):
    await store.insert_many(
        [
            _row("alpha memory", ts=1000),
            _row("beta memory", ts=2000),
            _row("gamma memory", ts=3000),
        ]
    )
    rows = await store.list_recent(limit=10)
    vectors = {
        "alpha memory": [1.0, 0.0, 0.0],
        "beta memory": [0.0, 1.0, 0.0],
        "gamma memory": [0.0, 0.0, 1.0],
    }
    for row in rows:
        await store.set_embedding(row["id"], vectors[row["content"]], "fake-model")

    hits = await store.search(
        SESSION,
        query="",
        vector=[0.9, 0.1, 0.0],
        mode="search",
        limit=1,
    )

    assert hits[0]["content"] == "alpha memory"
    assert hits[0]["score"] > 0.9


@pytest.mark.asyncio
async def test_time_window_and_aggregate(store):
    await store.insert_many(
        [
            _row("early", ts=1000),
            _row("late", ts=5000),
            _row("user fact", kind="fact", ts=5000, person_id="alice"),
        ]
    )

    window = await store.time_window(SESSION, start_ts=4000, end_ts=6000)
    aggregate = await store.aggregate(SESSION)

    assert [row["content"] for row in window] == ["late", "user fact"]
    kinds = {
        hit["meta"]["value"] for hit in aggregate if hit["meta"]["group"] == "kind"
    }
    assert kinds == {"summary", "fact"}


@pytest.mark.asyncio
async def test_episode_mode_prefers_episodes(store):
    await store.insert_many(
        [
            _row("session topic one", ts=1000),
            _row("[session] topic one and two", kind="episode", ts=2000),
        ]
    )

    hits = await store.search(SESSION, query="topic", mode="episode", limit=5)

    assert hits
    assert hits[0]["kind"] == "episode"


@pytest.mark.asyncio
async def test_global_scope_is_visible_from_other_session(store):
    await store.insert_many(
        [_row("shared preference", scope="global", session_key="other")]
    )

    hits = await store.search(SESSION, query="shared", mode="hybrid", limit=5)

    assert hits and hits[0]["content"] == "shared preference"


@pytest.mark.asyncio
async def test_cursor_roundtrip(store):
    assert await store.cursor(SESSION) == 0

    await store.set_cursor(SESSION, 1234)
    await store.set_cursor(SESSION, 5678)

    assert await store.cursor(SESSION) == 5678


@pytest.mark.asyncio
async def test_export_and_delete(store):
    await store.insert_many([_row("keep me", ts=1000), _row("drop me", ts=2000)])

    exported = await store.export(SESSION)
    deleted = await store.delete(session_key=SESSION)

    assert len(exported) == 2
    assert "embedding" not in exported[0]
    assert deleted == 2
    assert await store.stats() == {
        "total": 0,
        "embedded": 0,
        "by_kind": {},
        "latest_ts": None,
    }
