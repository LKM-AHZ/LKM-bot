"""Scene-aware reply policy (port of MaiBot 1.4 chat scenes).

Upstream 1.4 split the chat runtime into scenes - group, private and a fallback
- and let each scene decide whether it may answer, whether the reply may @ or
quote, and how often it quotes. The fork keeps the host pipeline but applies the
same decisions at the three places it already owns: holding an ambient turn,
composing a supplement plan and rewriting outbound replies.

This module only holds pure helpers and reads :class:`ScenesConfig`; the config
models live in :mod:`bot.life.config` to keep every switch in one file.
"""

from __future__ import annotations

from typing import Any, Literal

SceneKind = Literal["group", "private", "other"]

QUOTE_RATE_SCALE: dict[str, float] = {
    "never": 0.0,
    "occasionally": 1.0,
    "often": 2.0,
}
"""Multiplier applied to the persona's quote rate before the random draw."""


def scene_kind(umo: str, group_id: str | None = None) -> SceneKind:
    """Classify a session by its unified message origin.

    Args:
        umo: Session key, e.g. ``aiocqhttp:GroupMessage:1``.
        group_id: Optional group id from the event.

    Returns:
        ``group`` / ``private`` / ``other``.
    """
    if group_id:
        return "group"
    text = (umo or "").strip().lower()
    if "groupmessage" in text or ":group:" in text:
        return "group"
    if "friendmessage" in text or "privatemessage" in text or ":private:" in text:
        return "private"
    return "other"


def policy_for(scenes: Any, umo: str, group_id: str | None = None) -> Any:
    """Return the scene policy that governs one session.

    Args:
        scenes: ``ScenesConfig`` (or None when the feature is unavailable).
        umo: Session key.
        group_id: Optional group id.

    Returns:
        The matching ``ScenePolicyConfig``, or None when scenes are disabled.
    """
    if scenes is None or not getattr(scenes, "enabled", False):
        return None
    kind = scene_kind(umo, group_id)
    return getattr(scenes, kind, None)


def quote_rate(base_rate: float, frequency: str) -> float:
    """Scale the persona quote rate by the scene frequency.

    Args:
        base_rate: Probability from the persona profile.
        frequency: ``never`` / ``occasionally`` / ``often``.

    Returns:
        Probability clamped to ``[0, 1]``.
    """
    scale = QUOTE_RATE_SCALE.get(frequency, 1.0)
    return max(0.0, min(1.0, float(base_rate) * scale))


def reply_blocked(config: Any, umo: str, group_id: str | None = None) -> bool:
    """Return whether Life must not answer one session.

    Combines the per-session pause rules with the scene ``reply_enabled``
    switch so every answering stage uses the same decision.

    Args:
        config: ``LifeConfig`` owning the pause and scene switches.
        umo: Session key.
        group_id: Optional group id.

    Returns:
        True when the message must be archived but never answered.
    """
    pause = getattr(config, "pause", None)
    if pause is not None and pause.is_paused(umo, group_id):
        return True
    policy = policy_for(getattr(config, "scenes", None), umo, group_id)
    return bool(policy is not None and not policy.reply_enabled)
