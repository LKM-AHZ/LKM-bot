"""Unit tests for expression storage."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.expression.store import ExpressionStore, expression_checksum

SESSION = "aiocqhttp:GroupMessage:1"


@pytest_asyncio.fixture
async def store(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    expressions = ExpressionStore(database)
    await expressions.ensure_schema()
    yield expressions
    await database.engine.dispose()


def _row(style: str, situation: str = "日常闲聊", **overrides) -> dict:
    return {
        "session_key": SESSION,
        "situation": situation,
        "style": style,
        "scene": "group",
        "weight": 1.0,
        "source": "heuristic",
        **overrides,
    }


def test_checksum_is_stable_per_situation_and_style():
    assert expression_checksum("情景", "说法") == expression_checksum("情景", "说法")
    assert expression_checksum("情景", "说法") != expression_checksum("情景2", "说法")


@pytest.mark.asyncio
async def test_upsert_inserts_then_reinforces(store):
    first = await store.upsert_many([_row("笑死")])
    second = await store.upsert_many([_row("笑死", weight=2.0)])

    assert first == {"inserted": 1, "merged": 0}
    assert second == {"inserted": 0, "merged": 1}
    rows = await store.list_expressions(SESSION)
    assert len(rows) == 1
    assert rows[0]["weight"] == 3.0


@pytest.mark.asyncio
async def test_bump_usage_updates_weight_and_timestamp(store):
    await store.upsert_many([_row("确实")])
    rows = await store.list_expressions(SESSION)

    updated = await store.bump_usage([int(rows[0]["id"])])

    assert updated == 1
    refreshed = await store.list_expressions(SESSION)
    assert refreshed[0]["usage_count"] == 1
    assert refreshed[0]["last_used_ts"] > 0
    assert refreshed[0]["weight"] == 2.0


@pytest.mark.asyncio
async def test_scene_filter_keeps_any(store):
    await store.upsert_many(
        [
            _row("群里专用", scene="group"),
            _row("私聊专用", scene="private"),
            _row("都能用", scene="any"),
        ]
    )

    group_rows = await store.list_expressions(SESSION, scene="group")

    assert {row["style"] for row in group_rows} == {"群里专用", "都能用"}


@pytest.mark.asyncio
async def test_vector_state_and_pending_rows(store):
    await store.upsert_many([_row("哈哈"), _row("麻了")])

    pending = await store.pending_for_index()

    assert len(pending) == 2
    await store.mark_vector_state([int(row["id"]) for row in pending], "indexed")
    assert await store.pending_for_index() == []
    stats = await store.stats()
    assert stats["indexed"] == 2


@pytest.mark.asyncio
async def test_cursor_roundtrip(store):
    assert await store.cursor(SESSION) == 0

    await store.set_cursor(SESSION, 1700)
    await store.set_cursor(SESSION, 1800)

    assert await store.cursor(SESSION) == 1800


@pytest.mark.asyncio
async def test_delete_by_id_and_session(store):
    await store.upsert_many([_row("哈哈"), _row("麻了")])
    rows = await store.list_expressions(SESSION)

    one = await store.delete(expression_ids=[int(rows[0]["id"])])
    rest = await store.delete(session_key=SESSION)

    assert one == 1
    assert rest == 1
    assert await store.list_expressions(SESSION) == []


@pytest.mark.asyncio
async def test_insert_rows_ignores_duplicates(store):
    now = int(time.time())
    row = {
        "session_key": SESSION,
        "scene": "group",
        "situation": "情景",
        "style": "说法",
        "checksum": expression_checksum("情景", "说法"),
        "weight": 1.0,
        "source": "manual",
        "status": "active",
        "vector_state": "pending",
        "tags": [],
        "created_at": now,
        "updated_at": now,
    }

    assert await store.insert_rows([row]) == 1
    assert await store.insert_rows([row]) == 0
