"""Unit tests for the proactive talk service."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import IdleConfig, LifeConfig
from bot.life.heartflow.idle import TalkState
from bot.life.heartflow.proactive import ProactiveService

SESSION = "aiocqhttp:GroupMessage:1"
NOON = datetime(2026, 1, 1, 12, 0).timestamp()


class FakeArchive:
    def __init__(self) -> None:
        self.last_ts = NOON - 3600
        self.rows: list[dict] = []

    async def session_last_ts(self, since_ts: int = 0, limit: int = 50) -> list[dict]:
        return [{"session_key": SESSION, "last_ts": self.last_ts, "messages": 3}]

    async def fetch_range(self, session_key, since_ts=0, until_ts=None, limit=200):
        return list(self.rows)


class FakeSender:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, session_key: str, text: str):
        self.calls.append((session_key, text))
        return SimpleNamespace(ok=self.ok)


@pytest_asyncio.fixture
async def service(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "talk.db"))
    await database.initialize()
    life_config = LifeConfig(enabled=True)
    life_config.scope.mode = "all"
    archive = FakeArchive()
    archive.rows = [
        {"sender_name": "Alice", "plain_text": "周末去南山爬山"},
        {"sender_name": "Bob", "plain_text": "我可能没空"},
    ]
    sender = FakeSender()

    async def summarizer(system_prompt: str, user_prompt: str) -> str | None:
        return "南山那趟还去吗？"

    instance = ProactiveService(
        config=IdleConfig(
            enabled=True,
            min_idle_minutes=45,
            quiet_hours=[0, 0],
            daily_cap=2,
        ),
        db_helper=database,
        archive=archive,
        send=sender,
        life_config=life_config,
        summarizer=summarizer,
        clock=lambda: NOON,
    )
    await instance.initialize()
    # A session only earns the right to be spoken to first by engaging first.
    await instance.store.save(TalkState(session_key=SESSION, talk_value=0.7))
    yield instance, sender, archive
    await instance.close()
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_round_sends_one_opener(service):
    instance, sender, _archive = service

    result = await instance.run_round(NOON)

    assert result["sent"] == 1
    assert sender.calls == [(SESSION, "南山那趟还去吗？")]
    state = await instance.session_state(SESSION)
    assert state["last_proactive_ts"] == NOON
    assert state["sent_today"] == 1
    assert (await instance.status(NOON))["pending_answers"] == 1


@pytest.mark.asyncio
async def test_round_waits_for_an_answer_before_speaking_again(service):
    instance, sender, _archive = service
    await instance.run_round(NOON)

    again = await instance.run_round(NOON + 600)

    assert again["sent"] == 0
    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_answer_clears_attempts_and_raises_talk_value(service):
    instance, sender, _archive = service
    await instance.run_round(NOON)

    answered = await instance.observe_messages(
        [SimpleNamespace(session_key=SESSION, ts=NOON + 120)]
    )
    state = await instance.session_state(SESSION)

    assert answered == 1
    assert state["attempts"] == 0
    assert state["answered"] == 1
    assert state["talk_value"] > 0.5
    assert (await instance.status(NOON))["counters"]["answered"] == 1


@pytest.mark.asyncio
async def test_ignored_opener_lowers_talk_value_and_backs_off(service):
    instance, sender, _archive = service
    await instance.run_round(NOON)

    late = NOON + 31 * 60
    result = await instance.run_round(late)
    state = await instance.session_state(SESSION)

    assert result["sent"] == 0
    assert state["attempts"] == 1
    assert state["talk_value"] <= 0.5
    assert len(sender.calls) == 1
    # The backoff keeps it silent for the next base interval.
    assert (await instance.run_round(late + 60))["sent"] == 0


@pytest.mark.asyncio
async def test_daily_cap_stops_the_round(service):
    instance, sender, archive = service
    await instance.run_round(NOON)
    assert sender.calls

    await instance.observe_messages(
        [SimpleNamespace(session_key=SESSION, ts=NOON + 60)]
    )
    # The archive keeps the session quiet, so idleness keeps growing.
    second = await instance.run_round(NOON + 3600)
    await instance.observe_messages(
        [SimpleNamespace(session_key=SESSION, ts=NOON + 3660)]
    )
    third = await instance.run_round(NOON + 7200)

    assert second["sent"] == 1
    assert third["sent"] == 0
    assert len(sender.calls) == 2


@pytest.mark.asyncio
async def test_disabled_service_never_sends(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "off.db"))
    await database.initialize()
    sender = FakeSender()
    instance = ProactiveService(
        config=IdleConfig(enabled=False),
        db_helper=database,
        archive=FakeArchive(),
        send=sender,
    )
    await instance.initialize()

    result = await instance.run_round(NOON)

    assert result == {"enabled": False}
    assert sender.calls == []
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_missing_provider_skips_the_round(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "nollm.db"))
    await database.initialize()
    sender = FakeSender()
    instance = ProactiveService(
        config=IdleConfig(enabled=True, quiet_hours=[0, 0]),
        db_helper=database,
        archive=FakeArchive(),
        send=sender,
    )
    await instance.initialize()

    result = await instance.run_round(NOON)

    assert result["skipped"] == "no_provider"
    assert sender.calls == []
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_failed_send_is_not_recorded_as_sent(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "fail.db"))
    await database.initialize()
    life_config = LifeConfig(enabled=True)
    life_config.scope.mode = "all"
    sender = FakeSender(ok=False)

    async def summarizer(system_prompt: str, user_prompt: str) -> str | None:
        return "在吗"

    instance = ProactiveService(
        config=IdleConfig(enabled=True, quiet_hours=[0, 0]),
        db_helper=database,
        archive=FakeArchive(),
        send=sender,
        life_config=life_config,
        summarizer=summarizer,
        clock=lambda: NOON,
    )
    await instance.initialize()

    result = await instance.run_round(NOON)
    state = await instance.session_state(SESSION)

    assert result["sent"] == 0
    assert state["last_proactive_ts"] == 0.0
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_scope_outside_the_allowlist_is_skipped(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "scope.db"))
    await database.initialize()
    life_config = LifeConfig(enabled=True)
    sender = FakeSender()

    async def summarizer(system_prompt: str, user_prompt: str) -> str | None:
        return "在吗"

    instance = ProactiveService(
        config=IdleConfig(enabled=True, quiet_hours=[0, 0]),
        db_helper=database,
        archive=FakeArchive(),
        send=sender,
        life_config=life_config,
        summarizer=summarizer,
        clock=lambda: NOON,
    )
    await instance.initialize()

    assert await instance.scan(NOON) == []
    await database.engine.dispose()
