"""Unit tests for Life archive persistence."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.store import LifeStore


def _row(message_id: str, ts: int | None = None) -> dict:
    now = int(time.time())
    return {
        "platform_id": "aiocqhttp",
        "session_key": "aiocqhttp:GroupMessage:1",
        "message_id": message_id,
        "ts": ts if ts is not None else now,
        "sender_id": "u1",
        "sender_name": "Alice",
        "group_id": "1",
        "components": [{"type": "text", "data": {"text": "hi"}}],
        "plain_text": "hi",
        "reply_to": None,
        "flags": {"at": False},
        "ingested_at": now,
    }


@pytest_asyncio.fixture
async def db(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    yield database
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_insert_is_idempotent(db):
    store = LifeStore(db)
    await store.ensure_schema()

    first = await store.insert_batch([_row("m1")])
    second = await store.insert_batch([_row("m1")])

    assert first == 1
    assert second == 0


@pytest.mark.asyncio
async def test_stats_and_recent(db):
    store = LifeStore(db)
    await store.ensure_schema()
    await store.insert_batch([_row("m1", 100), _row("m2", 200)])

    stats = await store.stats()
    recent = await store.recent(10)

    assert stats["messages"] == 2
    assert stats["sessions"] == 1
    assert stats["earliest_ts"] == 100
    assert stats["latest_ts"] == 200
    assert [item["message_id"] for item in recent] == ["m2", "m1"]


@pytest.mark.asyncio
async def test_retention_cleanup_removes_old_rows(db):
    store = LifeStore(db, retention_days=1)
    await store.ensure_schema()
    old_ts = int(time.time()) - 2 * 86400
    await store.insert_batch([_row("old", old_ts), _row("new")])

    deleted = await store.cleanup()

    assert deleted == 1
    assert (await store.stats())["messages"] == 1


@pytest.mark.asyncio
async def test_retention_disabled_keeps_rows(db):
    store = LifeStore(db, retention_days=0)
    await store.ensure_schema()
    await store.insert_batch([_row("m1", 1)])

    assert await store.cleanup() == 0
    assert (await store.stats())["messages"] == 1


@pytest.mark.asyncio
async def test_outbound_record_is_persisted(db):
    store = LifeStore(db)
    await store.ensure_schema()

    await store.record_outbound(
        session_key="aiocqhttp:GroupMessage:1",
        message_id="life-1",
        status="sent",
        attempts=1,
    )

    assert (await store.stats())["outbound"] == 1
