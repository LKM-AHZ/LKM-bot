"""Stable bucketing for the percentage rollout.

The bucket has to be *sticky*: a group must stay inside (or outside) the
rollout across restarts, workers and messages, otherwise one conversation flips
every time a message lands. A hash of the rollout key therefore replaces a
random draw, and an optional salt lets an operator reshuffle the assignment
between two gray windows without touching the percentage.
"""

from __future__ import annotations

import hashlib

BUCKETS = 100
"""Resolution of a bucket: a percentage is simply the first N buckets."""


def rollout_key(umo: str, group_id: str | None = None) -> str:
    """Return the key a session is bucketed by.

    Group sessions are bucketed by group, not by sender: half a group inside
    the rollout would make the bot answer some messages and stay silent on the
    next ones, which is exactly the inconsistent behaviour the rollout is meant
    to avoid.

    Args:
        umo: unified_msg_origin of the session.
        group_id: Group id, or None for a private session.

    Returns:
        Bucketing key of the session.
    """
    if group_id:
        return f"group:{group_id}"
    return f"session:{umo}"


def bucket_for(key: str, salt: str = "") -> int:
    """Return the stable bucket of a key in ``[0, BUCKETS)``.

    Args:
        key: Value to bucket, usually :func:`rollout_key`.
        salt: Optional per-window salt; changing it reshuffles the assignment.

    Returns:
        Bucket index between 0 and 99.
    """
    digest = hashlib.sha256(f"{salt}:{key}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % BUCKETS


def in_rollout(percent: float, key: str, salt: str = "") -> bool:
    """Return whether a key falls inside a percentage rollout.

    Args:
        percent: Share of sessions to enable, ``0`` to ``100``; values outside
            the range are clamped.
        key: Value to bucket, usually :func:`rollout_key`.
        salt: Optional per-window salt.

    Returns:
        True when the key's bucket is below the percentage.
    """
    if percent <= 0:
        return False
    if percent >= BUCKETS:
        return True
    return bucket_for(key, salt) < percent


def enabled_by_percent(
    percent: float,
    umo: str,
    group_id: str | None = None,
    salt: str = "",
) -> bool:
    """Return whether a session is enabled by the percentage rollout.

    Args:
        percent: Share of sessions to enable, ``0`` to ``100``.
        umo: unified_msg_origin of the session.
        group_id: Group id, or None for a private session.
        salt: Optional per-window salt.

    Returns:
        True when the session's roll-out bucket is inside the window.
    """
    return in_rollout(percent, rollout_key(umo, group_id), salt)
