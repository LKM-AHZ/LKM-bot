"""Unit tests for heart-flow debounce."""

from __future__ import annotations

import pytest

from bot.life.config import DebounceConfig
from bot.life.heartflow.debounce import (
    CALM_MESSAGE_COUNT,
    TIER_CALM,
    TIER_NATURAL,
    TIER_QUICK,
    DebounceAccumulator,
    PendingMessage,
    plan_release,
)


def _message(ts: float, sender: str | None = "u1", direct: bool = False):
    return PendingMessage(
        message_id=f"m{int(ts * 10)}",
        ts=ts,
        sender_id=sender,
        direct=direct,
    )


def test_direct_address_uses_the_quick_tier():
    plan = plan_release([_message(100.0, direct=True)], DebounceConfig())

    assert plan.tier == TIER_QUICK
    assert plan.release_at == pytest.approx(101.2)
    assert plan.direct is True


def test_single_message_uses_the_natural_tier():
    plan = plan_release([_message(100.0)], DebounceConfig())

    assert plan.tier == TIER_NATURAL
    assert plan.release_at == pytest.approx(102.5)


def test_busy_turn_uses_the_calm_tier():
    messages = [
        _message(100.0, "u1"),
        _message(100.5, "u2"),
        _message(101.0, "u3"),
    ]

    plan = plan_release(messages, DebounceConfig())

    assert CALM_MESSAGE_COUNT == 3
    assert plan.tier == TIER_CALM
    assert plan.release_at == pytest.approx(105.0)


def test_hard_cap_bounds_the_wait():
    messages = [
        _message(100.0, "u1"),
        _message(102.0, "u2"),
        _message(104.0, "u3"),
        _message(106.0, "u4"),
    ]

    plan = plan_release(messages, DebounceConfig())

    # 106 + 4.0 would be 110s, but the turn started at 100s and cannot exceed 8s.
    assert plan.release_at == pytest.approx(108.0)
    assert "hard_cap" in plan.reason


def test_same_sender_continues_extends_the_window_once():
    messages = [_message(100.0, "u1"), _message(101.0, "u1")]

    plan = plan_release(messages, DebounceConfig())

    # natural 2.5 + same-sender 1.0
    assert plan.release_at == pytest.approx(104.5)


def test_different_senders_do_not_extend_the_window():
    messages = [_message(100.0, "u1"), _message(101.0, "u2")]

    plan = plan_release(messages, DebounceConfig())

    assert plan.release_at == pytest.approx(103.5)


def test_direct_address_caps_the_window_at_quick():
    messages = [
        _message(100.0, "u1", direct=True),
        _message(100.6, "u2", direct=False),
        _message(101.0, "u3", direct=False),
    ]

    plan = plan_release(messages, DebounceConfig())

    assert plan.tier == TIER_QUICK
    assert plan.release_at == pytest.approx(102.2)


def test_disabled_debounce_releases_immediately():
    plan = plan_release(
        [_message(100.0), _message(100.5)],
        DebounceConfig(enabled=False),
    )

    assert plan.reason == "debounce_disabled"
    assert plan.release_at == pytest.approx(100.5)


def test_empty_batch_is_rejected():
    with pytest.raises(ValueError):
        plan_release([], DebounceConfig())


def test_accumulator_tracks_due_and_pop():
    accumulator = DebounceAccumulator(DebounceConfig())

    assert not accumulator
    assert accumulator.plan() is None
    assert accumulator.pop() == ([], None)

    plan = accumulator.add(_message(100.0))
    assert plan.release_at == pytest.approx(102.5)
    assert not accumulator.due(102.0)
    assert accumulator.due(102.5)

    messages, popped = accumulator.pop()
    assert [item.message_id for item in messages] == ["m1000"]
    assert popped is not None and popped.tier == TIER_NATURAL
    assert not accumulator
