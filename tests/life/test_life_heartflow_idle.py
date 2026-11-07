"""Unit tests for the idle talk rules."""

from __future__ import annotations

from datetime import datetime

import pytest

from bot.life.config import IdleConfig
from bot.life.heartflow.idle import (
    DEFAULT_TALK_VALUE,
    TalkState,
    backoff_seconds,
    bump_talk,
    clamp,
    in_quiet_hours,
    is_candidate,
    local_hour,
)

NOON = datetime(2026, 1, 1, 12, 0).timestamp()
EARLY = datetime(2026, 1, 1, 3, 0).timestamp()


def _config(**overrides) -> IdleConfig:
    config = IdleConfig(enabled=True)
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_default_talk_value_is_neutral():
    assert DEFAULT_TALK_VALUE == 0.5
    assert clamp(-1) == 0.0
    assert clamp(2) == 1.0


def test_talk_value_moves_and_clamps():
    assert bump_talk(0.5, 0.1) == pytest.approx(0.6)
    assert bump_talk(0.95, 0.2) == 1.0
    assert bump_talk(0.05, -0.2) == 0.0


def test_backoff_grows_exponentially_and_caps():
    config = _config(backoff_base_minutes=120, backoff_max_minutes=360)

    assert backoff_seconds(0, config) == 0
    assert backoff_seconds(1, config) == pytest.approx(7200)
    assert backoff_seconds(2, config) == pytest.approx(14400)
    assert backoff_seconds(9, config) == pytest.approx(360 * 60)


def test_quiet_hours_simple_window():
    config = _config(quiet_hours=[0, 8])

    assert in_quiet_hours(3, config) is True
    assert in_quiet_hours(8, config) is False
    assert in_quiet_hours(23, config) is False


def test_quiet_hours_wrapping_window():
    config = _config(quiet_hours=[22, 6])

    assert in_quiet_hours(23, config) is True
    assert in_quiet_hours(5, config) is True
    assert in_quiet_hours(6, config) is False
    assert in_quiet_hours(12, config) is False


def test_quiet_hours_disabled():
    assert in_quiet_hours(3, _config(quiet_hours=[0, 0])) is False
    assert in_quiet_hours(3, _config(quiet_hours=[])) is False


def test_local_hour_uses_the_local_zone():
    assert local_hour(NOON) == 12
    assert local_hour(EARLY) == 3


def test_candidate_needs_a_quiet_session():
    config = _config(quiet_hours=[0, 0])
    state = TalkState(session_key="s", talk_value=0.8)

    assert is_candidate(state, 10, NOON, config) == (False, "too_recent")
    assert is_candidate(state, 10_000, NOON, config) == (False, "thread_dead")
    assert is_candidate(state, 60, NOON, config) == (True, "ok")


def test_candidate_respects_talk_value_and_cap():
    config = _config(quiet_hours=[0, 0])
    silent = TalkState(session_key="s", talk_value=0.2)
    capped = TalkState(session_key="s", talk_value=0.9, sent_today=2)

    assert is_candidate(silent, 60, NOON, config) == (False, "low_talk_value")
    assert is_candidate(capped, 60, NOON, config) == (False, "daily_cap")


def test_candidate_respects_quiet_hours():
    config = _config(quiet_hours=[0, 8])
    state = TalkState(session_key="s", talk_value=0.9)

    assert is_candidate(state, 60, EARLY, config) == (False, "quiet_hours")


def test_candidate_respects_backoff():
    config = _config(quiet_hours=[0, 0], backoff_base_minutes=120)
    state = TalkState(
        session_key="s",
        talk_value=0.9,
        attempts=1,
        last_proactive_ts=NOON - 3600,
    )

    assert is_candidate(state, 60, NOON, config) == (False, "backoff")
    # Two hours later the backoff expired.
    assert is_candidate(state, 60, NOON + 7200, config) == (True, "ok")
