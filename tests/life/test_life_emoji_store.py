"""Unit tests for the sticker store."""

from __future__ import annotations

import hashlib
import time

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.emoji.store import EmojiStore

SESSION = "aiocqhttp:GroupMessage:1"


@pytest_asyncio.fixture
async def store(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    emoji = EmojiStore(database)
    await emoji.ensure_schema()
    yield emoji
    await database.engine.dispose()


def _row(digest: str, path: str = "/tmp/a.png", **overrides) -> dict:
    now = int(time.time())
    return {
        "session_key": SESSION,
        "sha256": digest,
        "file_path": path,
        "mime": "image/png",
        "size_bytes": 100,
        "tags": ["笑"],
        "caption": "笑死",
        "created_at": now,
        "updated_at": now,
        **overrides,
    }


@pytest.mark.asyncio
async def test_insert_dedupes_by_content_hash(store):
    digest = hashlib.sha256(b"a").hexdigest()

    first = await store.insert_many([_row(digest)])
    second = await store.insert_many([_row(digest)])

    assert first == 1
    assert second == 0
    row = await store.get_by_hash(SESSION, digest)
    assert row["file_path"] == "/tmp/a.png"


@pytest.mark.asyncio
async def test_list_orders_by_usage(store):
    await store.insert_many(
        [
            _row("h1", "/tmp/1.png", usage_count=0),
            _row("h2", "/tmp/2.png", usage_count=5),
        ]
    )

    rows = await store.list_emojis(SESSION)

    assert [row["file_path"] for row in rows] == ["/tmp/2.png", "/tmp/1.png"]


@pytest.mark.asyncio
async def test_update_tags_and_status(store):
    await store.insert_many([_row("h1")])
    row = await store.get_by_hash(SESSION, "h1")

    updated = await store.update_tags(
        int(row["id"]),
        tags=["离谱"],
        status="rejected",
        caption="新说明",
    )

    assert updated is True
    refreshed = await store.get_by_hash(SESSION, "h1")
    assert refreshed["tags"] == ["离谱"]
    assert refreshed["status"] == "rejected"
    assert refreshed["caption"] == "新说明"
    assert await store.list_emojis(SESSION, status="active") == []


@pytest.mark.asyncio
async def test_embedding_and_pending_rows(store):
    await store.insert_many([_row("h1")])
    row = await store.get_by_hash(SESSION, "h1")

    pending = await store.pending_embeddings()
    assert len(pending) == 1

    await store.set_embedding(int(row["id"]), b"\x00\x00\x80\x3f", "fake", 1)

    assert await store.pending_embeddings() == []
    refreshed = await store.get_by_hash(SESSION, "h1")
    assert refreshed["embedding_model"] == "fake"
    assert refreshed["embedding_dim"] == 1


@pytest.mark.asyncio
async def test_bump_usage_and_delete_returns_paths(store):
    await store.insert_many([_row("h1", "/tmp/x.png")])
    row = await store.get_by_hash(SESSION, "h1")

    await store.bump_usage(int(row["id"]))
    refreshed = await store.get_by_hash(SESSION, "h1")
    assert refreshed["usage_count"] == 1
    assert refreshed["last_used_ts"] > 0

    paths = await store.delete(emoji_ids=[int(row["id"])])

    assert paths == ["/tmp/x.png"]
    assert await store.get_by_hash(SESSION, "h1") is None


@pytest.mark.asyncio
async def test_cursor_stats_and_prune(tmp_path):
    store_path = tmp_path / "life.db"
    database = SQLiteDatabase(str(store_path))
    await database.initialize()
    emoji = EmojiStore(database)
    await emoji.ensure_schema()
    try:
        assert await emoji.cursor(SESSION) == 0
        await emoji.set_cursor(SESSION, 1700)
        await emoji.set_cursor(SESSION, 1800)
        assert await emoji.cursor(SESSION) == 1800

        await emoji.insert_many([_row("h1", size_bytes=10), _row("h2", size_bytes=20)])
        stats = await emoji.stats()
        assert stats == {"total": 2, "bytes": 30, "embedded": 0, "used": 0}

        directory = tmp_path / "files"
        directory.mkdir()
        for index in range(5):
            (directory / f"{index}.png").write_bytes(b"x")
        removed = emoji.prune_files(directory, keep=2)
        assert removed == 3
        assert len(list(directory.iterdir())) == 2
    finally:
        await database.engine.dispose()
