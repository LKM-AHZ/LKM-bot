"""Unit tests for jargon storage: dedupe, conflicts, review and cursor."""

from __future__ import annotations

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import JargonConfig
from bot.life.jargon.store import JargonStore, term_checksum

SESSION = "aiocqhttp:GroupMessage:1"


@pytest_asyncio.fixture
async def store(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    jargon = JargonStore(database, JargonConfig(enabled=True))
    await jargon.ensure_schema()
    yield jargon
    await database.engine.dispose()


def _row(term: str, meaning: str, **overrides) -> dict:
    return {
        "session_key": SESSION,
        "term": term,
        "meaning": meaning,
        "confidence": 0.75,
        "source": "heuristic",
        **overrides,
    }


def test_checksum_is_case_insensitive():
    assert term_checksum("YYDS") == term_checksum("yyds")


@pytest.mark.asyncio
async def test_upsert_inserts_and_dedupes(store):
    first = await store.upsert_many([_row("绷不住", "想笑但憋着")])
    second = await store.upsert_many([_row("绷不住", "想笑但憋着")])

    assert first == {"inserted": 1, "updated": 0, "conflicts": 0}
    assert second == {"inserted": 0, "updated": 1, "conflicts": 0}
    row = await store.get_by_term(SESSION, "绷不住")
    assert row["meaning"] == "想笑但憋着"


@pytest.mark.asyncio
async def test_second_meaning_goes_pending(store):
    await store.upsert_many([_row("典", "很典型的例子")])

    result = await store.upsert_many([_row("典", "很搞笑的意思")])

    assert result["conflicts"] == 1
    row = await store.get_by_term(SESSION, "典")
    assert row["status"] == "pending"
    assert row["confidence"] <= 0.5
    assert row["meaning"] == "很典型的例子"


@pytest.mark.asyncio
async def test_review_activates_and_corrects(store):
    await store.upsert_many([_row("典", "很典型的例子"), _row("典", "很搞笑的意思")])
    row = await store.get_by_term(SESSION, "典")

    updated = await store.review(int(row["id"]), "active", "形容事情很典型")

    assert updated is True
    refreshed = await store.get_by_term(SESSION, "典")
    assert refreshed["status"] == "active"
    assert refreshed["meaning"] == "形容事情很典型"
    assert refreshed["source"] == "manual"
    assert refreshed["confidence"] >= 0.9


@pytest.mark.asyncio
async def test_status_filter_and_usage(store):
    await store.upsert_many([_row("绷不住", "想笑但憋着")])
    await store.upsert_many(
        [
            _row("典", "很典型"),
            {
                "session_key": SESSION,
                "term": "yyds",
                "meaning": "永远的神",
                "status": "rejected",
            },
        ]
    )

    active = await store.list_terms(SESSION, statuses=["active"])
    rejected = await store.list_terms(SESSION, statuses=["rejected"])

    assert {row["term"] for row in active} == {"绷不住", "典"}
    assert [row["term"] for row in rejected] == ["yyds"]

    bumped = await store.bump_usage([int(active[0]["id"])])

    assert bumped == 1
    refreshed = await store.get_by_term(SESSION, active[0]["term"])
    assert refreshed["usage_count"] == 1


@pytest.mark.asyncio
async def test_cursor_and_delete(store):
    assert await store.cursor(SESSION) == 0
    await store.set_cursor(SESSION, 1700)
    await store.set_cursor(SESSION, 1800)
    assert await store.cursor(SESSION) == 1800

    await store.upsert_many([_row("绷不住", "想笑但憋着")])
    row = await store.get_by_term(SESSION, "绷不住")

    assert await store.delete(jargon_ids=[int(row["id"])]) == 1
    assert await store.list_terms(SESSION) == []
    assert await store.stats() == {"total": 0, "by_status": {}}


@pytest.mark.asyncio
async def test_evidence_is_bounded(store):
    for index in range(10):
        await store.upsert_many(
            [_row("绷不住", "想笑但憋着", evidence=[f"样本 {index}"])]
        )

    row = await store.get_by_term(SESSION, "绷不住")

    assert len(row["evidence"]) <= 6
    assert row["last_seen_ts"] > 0
