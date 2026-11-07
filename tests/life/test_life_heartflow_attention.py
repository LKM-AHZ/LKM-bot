"""Unit tests for attention drift and back-reference hints."""

from __future__ import annotations

import pytest

from bot.life.config import AttentionConfig
from bot.life.heartflow.attention import (
    AttentionRegistry,
    AttentionState,
    extract_keywords,
    focus_at,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class FakeRequest:
    def __init__(self) -> None:
        self.prompt = "接着说"
        self.system_prompt = "You are a group member."


class FakeEvent:
    def __init__(self, umo: str = "s") -> None:
        self.unified_msg_origin = umo


def _registry(**overrides) -> tuple[AttentionRegistry, FakeClock]:
    clock = FakeClock()
    config = AttentionConfig()
    for key, value in overrides.items():
        setattr(config, key, value)
    return AttentionRegistry(config, clock=clock), clock


def test_focus_halves_after_the_half_life():
    state = AttentionState(session_key="s", last_ts=1000.0, focus=1.0)

    assert focus_at(state, 1000.0, 40.0) == pytest.approx(1.0)
    assert focus_at(state, 1000.0 + 40 * 60, 40.0) == pytest.approx(0.5)
    assert focus_at(state, 1000.0 + 80 * 60, 40.0) == pytest.approx(0.25)


def test_focus_without_engagement_is_zero():
    state = AttentionState(session_key="s")

    assert focus_at(state, 1000.0, 40.0) == 0.0
    assert focus_at(state, 1000.0, 0.0) == 0.0


def test_keywords_keep_short_distinct_segments():
    assert extract_keywords("周末去爬山吗？南山上周刚去过") == [
        "周末去爬山吗",
        "南山上周刚去过",
    ]
    assert extract_keywords("啊 好 嗯") == []
    assert extract_keywords("很长" * 20) == []


def test_observe_records_topic_and_resets_focus():
    registry, clock = _registry()
    registry.observe("s", "周末去南山爬山", clock.now)
    clock.now += 40 * 60

    assert registry.focus_of("s") == pytest.approx(0.5)

    registry.observe("s", "改去北山", clock.now)

    assert registry.focus_of("s") == pytest.approx(1.0)
    assert registry.status()["counters"]["observed"] == 2


def test_hint_appears_only_in_the_drifted_band():
    registry, clock = _registry()
    registry.observe("s", "周末去南山爬山", clock.now)

    # Still focused: no hint yet.
    clock.now += 60
    assert registry.hint("s", clock.now) is None

    # Drifted: a hint with the topic and the elapsed minutes.
    clock.now += 40 * 60
    hint = registry.hint("s", clock.now)
    assert hint is not None
    assert "周末去南山爬山" in hint
    assert "分钟前聊过" in hint


def test_hint_requires_a_minimum_gap():
    registry, clock = _registry(hook_min_gap_minutes=60.0)
    registry.observe("s", "周末去南山爬山", clock.now)
    clock.now += 40 * 60

    assert registry.hint("s", clock.now) is None


def test_focus_below_the_floor_forgets_the_topic():
    registry, clock = _registry()
    registry.observe("s", "周末去南山爬山", clock.now)
    clock.now += 40 * 60 * 10

    assert registry.hint("s", clock.now) is None
    assert registry.status()["counters"]["forgotten"] == 1
    assert registry.status()["tracked"] == 0


@pytest.mark.asyncio
async def test_inject_is_idempotent_and_optional():
    registry, clock = _registry()
    registry.observe("s", "周末去南山爬山", clock.now)
    clock.now += 40 * 60
    request = FakeRequest()

    assert registry.hint("s", clock.now) is not None
    assert await registry.inject(FakeEvent(), request) is True
    assert "<life_attention>" in request.system_prompt
    assert await registry.inject(FakeEvent(), request) is False

    disabled, _ = _registry(enabled=False)
    disabled.observe("s", "周末去南山爬山", clock.now)
    assert await disabled.inject(FakeEvent(), FakeRequest()) is False


def test_status_lists_sessions_by_recency():
    registry, clock = _registry()
    registry.observe("a", "第一件事", clock.now)
    clock.now += 120
    registry.observe("b", "第二件事", clock.now)

    sessions = registry.status()["sessions"]

    assert [row["session_key"] for row in sessions] == ["b", "a"]
    assert sessions[0]["age_minutes"] == pytest.approx(0.0)
