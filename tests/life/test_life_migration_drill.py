"""Tests for the rollback drill (S7c)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.core.db.po import PlatformMessageHistory
from bot.life.migration.drill import (
    ROLLBACK_LIMIT_SECONDS,
    build_synthetic_source,
    run_drill,
)

UMO = "aiocqhttp:GroupMessage:9001"


@pytest_asyncio.fixture
async def database(tmp_path):
    db = SQLiteDatabase(str(tmp_path / "life.db"))
    await db.initialize()
    yield db
    await db.engine.dispose()


@pytest.mark.asyncio
async def test_drill_imports_and_rolls_back(database, tmp_path):
    source = build_synthetic_source(tmp_path / "maibot", messages=5, group_id="9001")

    result = await run_drill(database, source, [UMO], tmp_path / "life")

    assert result["passed"] is True
    assert result["imported_rows"] >= 5
    assert result["deleted_rows"] >= result["imported_rows"]
    assert result["sentinel_survived"] is True
    assert result["after_cleanup"] == result["baseline"]
    assert result["rollback_seconds"] < ROLLBACK_LIMIT_SECONDS
    assert result["deleted"]["platform_message_history"] >= 5


@pytest.mark.asyncio
async def test_drill_preserves_existing_rows(database, tmp_path):
    async with database.get_db() as session:
        session.add(
            PlatformMessageHistory(
                platform_id="aiocqhttp",
                user_id=UMO,
                source_message_id="existing",
                content={
                    "type": "user",
                    "message": [],
                    "life": {"message_id": "existing", "ts": 1},
                },
            )
        )
        await session.commit()
    source = build_synthetic_source(tmp_path / "maibot", messages=3, group_id="9001")

    result = await run_drill(database, source, [UMO], tmp_path / "life")

    assert result["passed"] is True
    assert result["baseline"]["platform_message_history"] == 1
    assert result["after_cleanup"]["platform_message_history"] == 1
