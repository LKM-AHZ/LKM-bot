import asyncio
import time

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.core.utils.shared_preferences import SharedPreferences


@pytest_asyncio.fixture
async def preferences(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "preferences.db"))
    await database.initialize()
    store = SharedPreferences(database, tmp_path / "preferences.json")
    await store.initialize()
    try:
        yield store, database
    finally:
        await store.close()
        await database.engine.dispose()




@pytest.mark.asyncio
async def test_async_put_waits_for_persistence(preferences):
    store, database = preferences

    await store.put_async("global", "global", "theme", "dark")

    persisted = await database.get_preference("global", "global", "theme")
    assert persisted is not None
    assert persisted.value == {"val": "dark"}








@pytest.mark.asyncio
async def test_initialize_does_not_load_all_preferences(tmp_path, monkeypatch):
    """Startup must not materialize the whole preferences table in memory."""
    database = SQLiteDatabase(str(tmp_path / "preload.db"))
    await database.initialize()

    def fail_on_unfiltered_load(scope=None, scope_id=None, key=None):
        if scope is None and scope_id is None and key is None:
            raise AssertionError("initialize() must not load all preferences")
        return original_get_preferences(scope, scope_id, key)

    original_get_preferences = database.get_preferences
    monkeypatch.setattr(database, "get_preferences", fail_on_unfiltered_load)

    store = SharedPreferences(database, tmp_path / "preferences.json")
    try:
        await store.initialize()
        assert store._cache == {}
    finally:
        await store.close()
        await database.engine.dispose()






@pytest.mark.asyncio
async def test_get_async_falls_back_to_database_without_caching(preferences):
    store, database = preferences
    await database.insert_preference_or_update(
        "plugin",
        "heavy_plugin",
        "blob",
        {"val": {"payload": [1, 2, 3]}},
    )

    assert store._cache == {}
    value = await store.get_async("plugin", "heavy_plugin", "blob")
    assert value == {"payload": [1, 2, 3]}
    assert await store.get_async("plugin", "heavy_plugin", "missing", "d") == "d"
    # Reads must not grow the in-memory overlay.
    assert store._cache == {}


