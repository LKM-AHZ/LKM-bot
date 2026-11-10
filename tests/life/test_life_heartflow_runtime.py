"""Unit tests for the heart-flow turn runtime."""

from __future__ import annotations

import asyncio

import pytest

from bot.life.config import HeartFlowConfig, RuntimeConfig
from bot.life.heartflow.runtime import (
    ACTION_CONTINUE,
    ACTION_HOLD,
    TurnEntry,
    TurnRuntime,
)

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


class GateClock(FakeClock):
    """Clock whose sleep only returns once the test opens the gate."""

    def __init__(self, start: float = 1000.0) -> None:
        super().__init__(start)
        self.gate = asyncio.Event()

    async def sleep(self, delay: float) -> None:
        """Wait for the test to open the gate once, then advance freely.

        Args:
            delay: Requested delay in seconds.
        """
        await self.gate.wait()
        self.now += max(delay, 0.0)


def _runtime(clock: FakeClock, **overrides) -> TurnRuntime:
    config = HeartFlowConfig(enabled=True, bot_names=["小助手"])
    config.debounce.natural_seconds = 2.5
    config.debounce.quick_seconds = 1.2
    for key, value in overrides.items():
        target = config.runtime if hasattr(config.runtime, key) else config.gate
        setattr(target, key, value)
    return TurnRuntime(config, clock=clock, sleeper=clock.sleep)


def _entry(session: str = SESSION, text: str = "随便说", ts: float | None = None):
    return TurnEntry(
        session_key=session,
        text=text,
        ts=ts if ts is not None else 0.0,
        sender_id="u1",
        sender_name="Alice",
        message_id="m1",
    )


@pytest.mark.asyncio
async def test_direct_mention_is_answered():
    clock = FakeClock()
    runtime = _runtime(clock)
    entry = _entry(text="小助手 你看这个")
    entry.ts = clock.now

    outcome = await runtime.handle(SESSION, entry)

    assert outcome.action == ACTION_CONTINUE
    assert outcome.reason == "direct_address"
    assert outcome.force_wake is True
    assert runtime.status()["counters"]["replied"] == 1


@pytest.mark.asyncio
async def test_plain_chatter_is_waited_on():
    clock = FakeClock()
    runtime = _runtime(clock)
    entry = _entry(text="今天天气不错")
    entry.ts = clock.now

    outcome = await runtime.handle(SESSION, entry)

    assert outcome.action == ACTION_HOLD
    assert outcome.reason == "idle_silence"
    assert runtime.status()["counters"]["waited"] == 1


@pytest.mark.asyncio
async def test_concurrent_messages_merge_into_one_turn():
    clock = GateClock()
    runtime = _runtime(clock)
    first = _entry(text="小助手 在吗")
    second = _entry(text="看这个图")
    first.ts = second.ts = clock.now

    leader = asyncio.create_task(runtime.handle(SESSION, first))
    await asyncio.sleep(0)
    follower = asyncio.create_task(runtime.handle(SESSION, second))
    await asyncio.sleep(0)
    clock.gate.set()
    leader_outcome, follower_outcome = await asyncio.gather(leader, follower)

    assert leader_outcome.action == ACTION_CONTINUE
    assert follower_outcome.action == ACTION_HOLD
    assert follower_outcome.reason.startswith("merged:")
    assert "Alice: 小助手 在吗" in leader_outcome.merged_text
    assert "Alice: 看这个图" in leader_outcome.merged_text
    assert leader_outcome.merged_text.splitlines() == [
        "Alice: 小助手 在吗",
        "Alice: 看这个图",
    ]
    status = runtime.status()
    assert status["counters"]["turns"] == 1
    assert status["counters"]["merged"] == 1
    assert status["recent"][0]["messages"] == 2


@pytest.mark.asyncio
async def test_shadow_records_without_continuing():
    clock = FakeClock()
    runtime = _runtime(clock, shadow=True)
    entry = _entry(text="小助手 在吗")
    entry.ts = clock.now

    outcome = await runtime.handle(SESSION, entry)

    assert outcome.action == ACTION_HOLD
    assert outcome.decision is not None and outcome.decision.reply is True
    status = runtime.status()
    assert status["counters"]["shadowed"] == 1
    assert status["counters"]["replied"] == 0
    assert status["recent"][0]["shadow"] is True


@pytest.mark.asyncio
async def test_capacity_drops_do_not_block_the_event_forever():
    clock = FakeClock()
    runtime = _runtime(clock, max_pending_turns=1)
    held = _entry(text="小助手 在吗")
    held.ts = clock.now
    task = asyncio.create_task(runtime.handle(SESSION, held))
    await asyncio.sleep(0)

    outcome = await runtime.handle(OTHER, _entry(session=OTHER))
    await task

    assert outcome.action == ACTION_HOLD
    assert outcome.reason == "capacity"
    assert runtime.status()["counters"]["capacity_drops"] == 1


@pytest.mark.asyncio
async def test_consecutive_waits_raise_the_threshold():
    clock = FakeClock()
    runtime = _runtime(clock)
    for _ in range(2):
        entry = _entry(text="嗯嗯")
        entry.ts = clock.now
        await runtime.handle(SESSION, entry)
        clock.now += 60

    recent = runtime.status()["recent"]

    assert recent[0]["threshold"] > recent[1]["threshold"]
    assert recent[0]["decide"] == "wait"


@pytest.mark.asyncio
async def test_reply_resets_the_wait_streak():
    clock = FakeClock()
    runtime = _runtime(clock)
    silent = _entry(text="嗯")
    silent.ts = clock.now
    await runtime.handle(SESSION, silent)
    clock.now += 5
    loud = _entry(text="小助手")
    loud.ts = clock.now
    await runtime.handle(SESSION, loud)

    recent = runtime.status()["recent"]

    assert recent[0]["decide"] == "reply"
    assert recent[0]["threshold"] == 0.0


@pytest.mark.asyncio
async def test_close_releases_a_waiting_follower():
    clock = GateClock()
    runtime = _runtime(clock)
    leader = asyncio.create_task(runtime.handle(SESSION, _entry()))
    await asyncio.sleep(0)
    follower = asyncio.create_task(runtime.handle(SESSION, _entry()))
    await asyncio.sleep(0)

    await runtime.close()
    outcome = await asyncio.wait_for(follower, timeout=1)
    clock.gate.set()
    await asyncio.wait_for(leader, timeout=1)

    assert outcome.action == ACTION_HOLD
    assert runtime.closed is True
    assert (await runtime.handle(OTHER, _entry(session=OTHER))).action == (
        ACTION_CONTINUE
    )


@pytest.mark.asyncio
async def test_wait_is_capped_by_max_wait_seconds():
    clock = FakeClock()
    runtime = _runtime(clock, max_wait_seconds=0.5)
    entry = _entry(text="小助手")
    entry.ts = clock.now + 600  # a future timestamp must not hold the event

    outcome = await runtime.handle(SESSION, entry)

    assert clock.now <= 1001.0
    assert outcome.action in {ACTION_CONTINUE, ACTION_HOLD}


def test_runtime_config_defaults():
    config = RuntimeConfig()

    assert config.enabled is True
    assert config.shadow is False
    assert config.max_pending_turns == 32


@pytest.mark.asyncio
async def test_hot_session_uses_the_quick_tier():
    clock = FakeClock()
    runtime = _runtime(clock)
    mention = _entry(text="小助手 在吗")
    mention.ts = clock.now
    await runtime.handle(SESSION, mention)
    replied_at = clock.now

    clock.now += 10
    second_start = clock.now
    ambient = _entry(text="今天中午大家都在吃什么")
    ambient.ts = second_start
    await runtime.handle(SESSION, ambient)

    assert replied_at == pytest.approx(1001.2)
    # Quick tier (1.2s) instead of the natural window (2.5s).
    assert clock.now == pytest.approx(second_start + 1.2)


@pytest.mark.asyncio
async def test_cold_session_uses_the_natural_tier():
    clock = FakeClock()
    runtime = _runtime(clock)
    entry = _entry(text="今天中午大家都在吃什么")
    entry.ts = clock.now

    await runtime.handle(SESSION, entry)

    assert clock.now == pytest.approx(1002.5)


@pytest.mark.asyncio
async def test_short_ping_releases_quickly():
    clock = FakeClock()
    runtime = _runtime(clock)
    entry = _entry(text="在吗")
    entry.ts = clock.now

    await runtime.handle(SESSION, entry)

    assert clock.now == pytest.approx(1001.2)


@pytest.mark.asyncio
async def test_fast_release_can_be_disabled():
    clock = FakeClock()
    runtime = _runtime(clock, fast_release=False)
    entry = _entry(text="在吗")
    entry.ts = clock.now

    await runtime.handle(SESSION, entry)

    assert clock.now == pytest.approx(1002.5)


@pytest.mark.asyncio
async def test_reply_callback_receives_the_turn():
    clock = FakeClock()
    seen: list[tuple[str, str, float]] = []
    config = HeartFlowConfig(enabled=True, bot_names=["小助手"])
    runtime = TurnRuntime(
        config,
        clock=clock,
        sleeper=clock.sleep,
        on_reply=lambda key, text, now: seen.append((key, text, now)),
    )
    entry = _entry(text="小助手 在吗")
    entry.ts = clock.now

    await runtime.handle(SESSION, entry)

    assert len(seen) == 1
    assert seen[0][0] == SESSION
    assert "小助手 在吗" in seen[0][1]
    assert seen[0][2] == pytest.approx(1001.2)


@pytest.mark.asyncio
async def test_shadow_mode_does_not_notify_the_callback():
    clock = FakeClock()
    seen: list[str] = []
    config = HeartFlowConfig(enabled=True, bot_names=["小助手"])
    config.runtime.shadow = True
    runtime = TurnRuntime(
        config,
        clock=clock,
        sleeper=clock.sleep,
        on_reply=lambda key, text, now: seen.append(key),
    )
    entry = _entry(text="小助手 在吗")
    entry.ts = clock.now

    await runtime.handle(SESSION, entry)

    assert seen == []
