"""Tests for the Life ingest service and pipeline stage."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import LifeConfig
from bot.life.heartflow.runtime import TurnRuntime
from bot.life.pipeline import LifeIngestStage
from bot.life.service import LifeService, set_life_service


class FakeComponent:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def toDict(self) -> dict:
        return self._payload


class FakeEvent:
    unified_msg_origin = "aiocqhttp:GroupMessage:1"
    is_at_or_wake_command = False
    is_wake = False
    created_at = time.time()

    def __init__(self, message_id: str = "m1") -> None:
        self.message_obj = SimpleNamespace(message_id=message_id, timestamp=1700000000)
        self._components = [FakeComponent({"type": "text", "data": {"text": "hi"}})]

    def get_messages(self) -> list:
        return self._components

    def get_message_str(self) -> str:
        return "hi"

    def get_message_type(self):
        return SimpleNamespace(value="GroupMessage")

    def get_platform_id(self) -> str:
        return "aiocqhttp"

    def get_group_id(self) -> str:
        return "1"

    def get_sender_id(self) -> str:
        return "u1"

    def get_sender_name(self) -> str:
        return "Alice"


@pytest_asyncio.fixture
async def db(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    yield database
    await database.engine.dispose()


@pytest_asyncio.fixture
async def service(db, tmp_path):
    config = LifeConfig(enabled=True)
    config.scope.mode = "all"
    instance = LifeService(
        db,
        config=config,
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )
    await instance.initialize()
    yield instance
    await instance.stop()
    set_life_service(None)


async def _wait_for_written(service: LifeService, expected: int) -> None:
    for _ in range(100):
        if service._counters["written"] >= expected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"archive did not reach {expected} rows")


@pytest.mark.asyncio
async def test_ingest_archives_and_deduplicates(service):
    await service.ingest(FakeEvent("m1"))
    await service.ingest(FakeEvent("m1"))
    await _wait_for_written(service, 1)

    status = await service.status()
    assert status["enabled"] is True
    assert status["storage"]["messages"] == 1
    assert status["counters"]["duplicates"] == 1


@pytest.mark.asyncio
async def test_scope_blocks_other_sessions(db, tmp_path):
    config = LifeConfig(enabled=True)
    config.scope.enabled_umos = ["aiocqhttp:GroupMessage:other"]
    instance = LifeService(
        db,
        config=config,
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )
    await instance.initialize()
    try:
        await instance.ingest(FakeEvent("m1"))
        await asyncio.sleep(0.05)
        assert instance._counters["ingested"] == 0
    finally:
        await instance.stop()
        set_life_service(None)


@pytest.mark.asyncio
async def test_disabled_service_is_noop(db, tmp_path):
    instance = LifeService(
        db,
        config=LifeConfig(enabled=False),
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )
    await instance.initialize()

    await instance.ingest(FakeEvent("m1"))

    assert instance.running is False
    assert instance.store is None


@pytest.mark.asyncio
async def test_send_text_records_failed_attempt(service):
    result = await service.send_text("aiocqhttp:GroupMessage:1", "hello")

    assert result.ok is False
    assert (await service.status())["storage"]["outbound"] == 1


@pytest.mark.asyncio
async def test_stage_feeds_service(monkeypatch):
    calls: list[FakeEvent] = []

    class RecordingService:
        async def ingest(self, event):
            calls.append(event)

    monkeypatch.setattr(
        "bot.life.service.get_life_service",
        lambda: RecordingService(),
    )
    stage = LifeIngestStage()
    await stage.initialize(None)
    event = FakeEvent("m1")

    await stage.process(event)

    assert calls == [event]


@pytest.mark.asyncio
async def test_stage_is_noop_without_service(monkeypatch):
    monkeypatch.setattr("bot.life.service.get_life_service", lambda: None)
    stage = LifeIngestStage()

    await stage.initialize(None)
    await stage.process(FakeEvent("m1"))


@pytest.mark.asyncio
async def test_focus_layer_tracks_the_archive(db, tmp_path):
    config = LifeConfig(enabled=True)
    config.scope.mode = "all"
    config.heartflow.enabled = True
    config.heartflow.focus.enabled = True
    instance = LifeService(
        db,
        config=config,
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )
    await instance.initialize()
    try:
        assert instance.focus is not None
        await instance.ingest(FakeEvent("m1"))
        await _wait_for_written(instance, 1)
        detail: dict = {"turns": []}
        for _ in range(100):
            detail = await instance.focus.session_detail("aiocqhttp:GroupMessage:1")
            if detail["turns"]:
                break
            await asyncio.sleep(0.02)
        assert detail["turns"][0]["messages"][0]["text"] == "hi"
        status = await instance.status()
        assert status["focus"]["enabled"] is True
        assert status["focus"]["storage"]["turns"] == 1
    finally:
        await instance.stop()
        set_life_service(None)


@pytest.mark.asyncio
async def test_heartflow_runtime_is_wired_when_enabled(db, tmp_path):
    config = LifeConfig(enabled=True)
    config.scope.mode = "all"
    config.heartflow.enabled = True
    instance = LifeService(
        db,
        config=config,
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )
    await instance.initialize()
    try:
        assert isinstance(instance.heartflow, TurnRuntime)
        status = await instance.status()
        assert status["heartflow"]["enabled"] is True
        assert status["heartflow"]["shadow"] is False
        assert status["enabled"] is True
    finally:
        await instance.stop()
        set_life_service(None)
    assert instance.heartflow.closed is True


@pytest.mark.asyncio
async def test_heartflow_runtime_is_absent_by_default(service):
    assert service.heartflow is None
    status = await service.status()
    assert status["heartflow"]["enabled"] is False


@pytest.mark.asyncio
async def test_attention_and_proactive_are_wired_when_enabled(db, tmp_path):
    config = LifeConfig(enabled=True)
    config.scope.mode = "all"
    config.heartflow.enabled = True
    config.heartflow.idle.enabled = True
    instance = LifeService(
        db,
        config=config,
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )
    await instance.initialize()
    try:
        assert instance.attention is not None
        assert instance.proactive is not None
        status = await instance.status()
        assert status["attention"]["enabled"] is True
        assert status["proactive"]["configured"] is True
        assert status["proactive"]["daily_cap"] == 2
    finally:
        await instance.stop()
        set_life_service(None)


@pytest.mark.asyncio
async def test_proactive_stays_off_without_the_idle_switch(service):
    assert service.proactive is None
    status = await service.status()
    assert status["proactive"]["enabled"] is False
