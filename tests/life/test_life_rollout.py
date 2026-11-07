"""Tests for the gray rollout: bucketing, config, storage and observation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import HeartFlowConfig, LifeConfig
from bot.life.heartflow.runtime import (
    ACTION_CONTINUE,
    ACTION_HOLD,
    TurnEntry,
    TurnRuntime,
)
from bot.life.rollout.buckets import (
    BUCKETS,
    bucket_for,
    enabled_by_percent,
    in_rollout,
    rollout_key,
)
from bot.life.rollout.service import RolloutService
from bot.life.rollout.store import TurnDecisionStore

SESSION = "aiocqhttp:GroupMessage:1"
OTHER = "aiocqhttp:GroupMessage:2"


class FakeClock:
    """Deterministic wall clock that advances instead of sleeping."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        """Advance the clock instead of sleeping.

        Args:
            delay: Requested delay in seconds.
        """
        await asyncio.sleep(0)
        self.now += max(delay, 0.0)


def test_bucket_is_stable_and_bounded():
    first = bucket_for("group:1", "salt")
    assert first == bucket_for("group:1", "salt")
    assert 0 <= first < BUCKETS
    assert bucket_for("group:1", "salt") != bucket_for("group:2", "salt")


def test_percent_window_is_sticky_and_ordered():
    keys = [f"group:{index}" for index in range(500)]

    inside = [key for key in keys if in_rollout(30, key)]
    again = [key for key in keys if in_rollout(30, key)]

    assert inside == again
    assert 110 <= len(inside) <= 190
    assert in_rollout(0, "group:1") is False
    assert in_rollout(100, "group:1") is True
    assert all(key in inside for key in keys if bucket_for(key) < 30)


def test_salt_reshuffles_the_assignment():
    keys = [f"group:{index}" for index in range(200)]
    plain = {key for key in keys if in_rollout(20, key)}
    salted = {key for key in keys if in_rollout(20, key, "round-2")}

    assert plain != salted


def test_group_sessions_bucket_by_group():
    assert rollout_key(SESSION, "42") == rollout_key(OTHER, "42")
    assert rollout_key(SESSION) == f"session:{SESSION}"
    assert enabled_by_percent(100, SESSION, "42") is True


def test_percentage_enables_sessions():
    config = LifeConfig(enabled=True)
    config.scope.rollout_percent = 100

    assert config.session_enabled("aiocqhttp:GroupMessage:any")
    assert config.session_enabled("aiocqhttp:FriendMessage:any")

    config.scope.rollout_percent = 0
    assert not config.session_enabled("aiocqhttp:GroupMessage:any")


def test_explicit_allowlist_and_denylist_win_over_percentage():
    config = LifeConfig(enabled=True)
    config.scope.rollout_percent = 100
    config.scope.disabled_umos = [SESSION]
    config.scope.enabled_umos = [OTHER]
    config.scope.enabled_group_ids = ["7"]

    assert not config.session_enabled(SESSION, "1")
    assert config.session_enabled(OTHER)
    assert config.session_enabled("aiocqhttp:GroupMessage:9", "7")


def test_percentage_is_clamped_by_validation():
    from bot.life.config import ScopeConfig

    with pytest.raises(ValueError):
        ScopeConfig(rollout_percent=101.0)


def _row(
    ts: float,
    *,
    session: str = SESSION,
    decision: str = "reply",
    shadow: bool = False,
    latency_ms: int = 1200,
    text_chars: int = 5,
) -> dict:
    return {
        "ts": ts,
        "session_key": session,
        "messages": 1,
        "decision": decision,
        "action": "continue" if decision == "reply" else "hold",
        "shadow": shadow,
        "reason": "direct_address" if decision == "reply" else "likelihood<threshold",
        "likelihood": 0.7,
        "threshold": 0.55,
        "latency_ms": latency_ms,
        "text_chars": text_chars,
    }


@pytest_asyncio.fixture
async def database(tmp_path):
    db = SQLiteDatabase(str(tmp_path / "life.db"))
    await db.initialize()
    yield db
    await db.engine.dispose()


@pytest_asyncio.fixture
async def store(database):
    decision_store = TurnDecisionStore(database)
    await decision_store.ensure_schema()
    return decision_store


@pytest.mark.asyncio
async def test_report_counts_replies_waits_and_shadow(store):
    await store.insert(_row(100.0))
    await store.insert(_row(200.0, decision="wait"))
    await store.insert(_row(300.0, shadow=True))

    report = await store.report(1, now=400.0)

    assert report["turns"] == 3
    assert report["reply"] == 2
    assert report["wait"] == 1
    assert report["shadowed"] == 1
    assert report["live"] == 1
    assert report["reply_rate"] == 0.667
    assert [row["ts"] for row in report["recent"]] == [300.0, 200.0, 100.0]


@pytest.mark.asyncio
async def test_report_computes_latency_percentiles(store):
    for index, latency in enumerate((100, 200, 300, 400, 500), start=1):
        await store.insert(_row(float(index), latency_ms=latency))

    report = await store.report(1, now=10.0)

    assert report["latency_ms"] == {"p50": 300, "p90": 500, "max": 500}


@pytest.mark.asyncio
async def test_report_filters_the_window_and_ranks_sessions(store):
    await store.insert(_row(100.0, session=SESSION))
    await store.insert(_row(101.0, session=SESSION))
    await store.insert(_row(102.0, session=OTHER, decision="wait"))

    week = await store.report(1, now=200.0)
    empty = await store.report(1, now=100.0 + 2 * 86400)

    assert week["turns"] == 3
    assert week["sessions"][0] == {
        "session_key": SESSION,
        "turns": 2,
        "reply": 2,
        "last_ts": 101.0,
    }
    assert empty["turns"] == 0
    assert empty["reply_rate"] == 0.0
    assert empty["latency_ms"] == {"p50": 0, "p90": 0, "max": 0}


@pytest.mark.asyncio
async def test_prune_removes_rows_older_than_retention(store):
    await store.insert(_row(100.0))
    await store.insert(_row(100.0 - 10 * 86400, session=OTHER))

    deleted = await store.prune(7, now=100.0)
    report = await store.report(90, now=100.0)

    assert deleted == 1
    assert report["turns"] == 1
    assert await store.prune(0, now=100.0) == 0


@pytest_asyncio.fixture
async def service(database):
    config = LifeConfig(enabled=True)
    config.heartflow.enabled = True
    config.scope.rollout_percent = 25
    rollout = RolloutService(database, config, clock=lambda: 5000.0)
    await rollout.initialize()
    yield rollout


@pytest.mark.asyncio
async def test_service_observes_and_reports(service):
    await service.observe(_row(4000.0))
    await service.observe(_row(4500.0, decision="wait"))

    report = await service.report(7)
    status = await service.status()

    assert report["turns"] == 2
    assert status["enabled"] is True
    assert status["counters"]["observed"] == 2
    assert status["rollout"] == {
        "mode": "allowlist",
        "percent": 25.0,
        "salt_set": False,
    }
    assert status["window"]["turns"] == 2
    assert status["window"]["reply"] == 1


@pytest.mark.asyncio
async def test_service_swallows_storage_failures(service, monkeypatch):
    async def boom(row):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(service.store, "insert", boom)

    await service.observe(_row(4000.0))

    status = await service.status()
    assert status["counters"] == {"observed": 0, "failed": 1, "pruned": 0}
    assert status["window"]["turns"] == 0


@pytest.mark.asyncio
async def test_service_prune_counts_deleted_rows(service):
    await service.observe(_row(5000.0 - 100 * 86400))

    deleted = await service.prune()

    assert deleted == 1
    assert (await service.report(7))["turns"] == 0


@pytest.mark.asyncio
async def test_runtime_observes_released_turn():
    clock = FakeClock()
    seen: list[dict] = []

    async def on_turn(payload: dict) -> None:
        seen.append(payload)

    config = HeartFlowConfig(enabled=True, bot_names=["小助手"])
    runtime = TurnRuntime(
        config,
        clock=clock,
        sleeper=clock.sleep,
        on_turn=on_turn,
    )
    entry = TurnEntry(
        session_key=SESSION,
        text="小助手 在吗",
        ts=clock.now,
        sender_id="u1",
        sender_name="Alice",
        message_id="m1",
    )

    outcome = await runtime.handle(SESSION, entry)
    await asyncio.sleep(0)

    assert outcome.action == ACTION_CONTINUE
    assert len(seen) == 1
    payload = seen[0]
    assert payload["decision"] == "reply"
    assert payload["action"] == ACTION_CONTINUE
    assert payload["shadow"] is False
    assert payload["session_key"] == SESSION
    assert payload["latency_ms"] >= 1000
    assert payload["text_chars"] == len(outcome.merged_text)
    assert "text" not in payload


@pytest.mark.asyncio
async def test_shadow_runtime_records_without_continuing():
    clock = FakeClock()
    seen: list[dict] = []

    async def on_turn(payload: dict) -> None:
        seen.append(payload)

    config = HeartFlowConfig(enabled=True, bot_names=["小助手"])
    config.runtime.shadow = True
    runtime = TurnRuntime(
        config,
        clock=clock,
        sleeper=clock.sleep,
        on_turn=on_turn,
    )
    entry = TurnEntry(
        session_key=SESSION,
        text="小助手 在吗",
        ts=clock.now,
        sender_id="u1",
        sender_name="Alice",
        message_id="m1",
    )

    outcome = await runtime.handle(SESSION, entry)
    await asyncio.sleep(0)

    assert outcome.action == ACTION_HOLD
    assert seen[0]["shadow"] is True
    assert seen[0]["decision"] == "reply"
    assert seen[0]["action"] == ACTION_HOLD


@pytest.mark.asyncio
async def test_rollout_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.dashboard.api import life as life_api
    import bot.life.service as life_service_module

    async def report(days: int) -> dict:
        return {"window_days": days, "turns": 3}

    service = SimpleNamespace(rollout=SimpleNamespace(report=report))
    monkeypatch.setattr(life_service_module, "get_life_service", lambda: service)

    payload = await life_api.life_rollout(days=7, _auth=None)

    assert payload["status"] == "ok"
    assert payload["data"]["enabled"] is True
    assert payload["data"]["window_days"] == 7
    assert payload["data"]["turns"] == 3


@pytest.mark.asyncio
async def test_rollout_endpoint_without_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bot.dashboard.api import life as life_api
    import bot.life.service as life_service_module

    monkeypatch.setattr(life_service_module, "get_life_service", lambda: None)

    payload = await life_api.life_rollout(days=7, _auth=None)

    assert payload["data"]["enabled"] is False
    assert payload["data"]["turns"] == 0
