"""Talk value, idle backoff and the rules for speaking first.

Talking without being asked is the riskiest thing this layer can do, so every
rule here is a *veto*: the bot speaks first only when the session is quiet, the
last attempt was long enough ago, the daily budget is not spent, the local hour
is not in quiet hours, and the relationship has earned it.

Nothing here touches storage or the network; ``proactive.py`` does that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from bot.life.config import IdleConfig

DEFAULT_TALK_VALUE = 0.5
"""Talk value of a session that was never seen before."""


@dataclass(slots=True)
class TalkState:
    """Per-session talk propensity and proactive bookkeeping."""

    session_key: str
    talk_value: float = DEFAULT_TALK_VALUE
    last_proactive_ts: float = 0.0
    """Timestamp of the last proactive message, kept for the backoff."""

    pending_ts: float = 0.0
    """Timestamp of the proactive message still waiting for an answer."""

    attempts: int = 0
    answered: int = 0
    sent_day: str = ""
    sent_today: int = 0


@dataclass(slots=True)
class Candidate:
    """Session that may receive a proactive message."""

    session_key: str
    idle_minutes: float
    talk_value: float
    attempts: int


def clamp(value: float) -> float:
    """Return a talk value clamped to ``[0, 1]``.

    Args:
        value: Raw value.

    Returns:
        Clamped value.
    """
    return max(0.0, min(1.0, value))


def bump_talk(value: float, delta: float) -> float:
    """Apply one talk-value change.

    Args:
        value: Current talk value.
        delta: Signed change.

    Returns:
        Clamped new value.
    """
    return clamp(value + delta)


def backoff_seconds(attempts: int, config: IdleConfig) -> float:
    """Return the minimum gap before the next proactive attempt.

    Args:
        attempts: Consecutive ignored attempts.
        config: Idle settings.

    Returns:
        Seconds; 0 when there were no attempts yet.
    """
    if attempts <= 0:
        return 0.0
    base = max(config.backoff_base_minutes, 1.0) * 60.0
    ceiling = max(config.backoff_max_minutes, config.backoff_base_minutes) * 60.0
    return min(base * (2 ** (attempts - 1)), ceiling)


def in_quiet_hours(hour: int, config: IdleConfig) -> bool:
    """Return whether a local hour is inside the quiet window.

    Args:
        hour: Local hour in ``0..23``.
        config: Idle settings with ``[start, end)`` quiet hours.

    Returns:
        True when the bot must stay silent.
    """
    if not config.quiet_hours or len(config.quiet_hours) < 2:
        return False
    start, end = int(config.quiet_hours[0]), int(config.quiet_hours[1])
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def local_hour(ts: float) -> int:
    """Return the local hour of a timestamp.

    Args:
        ts: Epoch seconds.

    Returns:
        Local hour in ``0..23``.
    """
    return datetime.fromtimestamp(ts).hour


def is_candidate(
    state: TalkState,
    idle_minutes: float,
    now: float,
    config: IdleConfig,
) -> tuple[bool, str]:
    """Decide whether one session may be spoken to first.

    Args:
        state: Talk state of the session.
        idle_minutes: Minutes since the last archived message.
        now: Current wall-clock timestamp.
        config: Idle settings.

    Returns:
        ``(allowed, reason)``; the reason explains a veto.
    """
    if idle_minutes < config.min_idle_minutes:
        return False, "too_recent"
    if idle_minutes > config.max_idle_minutes:
        return False, "thread_dead"
    if state.talk_value < config.min_talk_value:
        return False, "low_talk_value"
    if in_quiet_hours(local_hour(now), config):
        return False, "quiet_hours"
    if state.sent_today >= config.daily_cap:
        return False, "daily_cap"
    gap = backoff_seconds(state.attempts, config)
    if state.last_proactive_ts and now - state.last_proactive_ts < gap:
        return False, "backoff"
    return True, "ok"
