"""Reply likelihood and the dynamic gate.

The gate answers one question per released turn: should the bot speak at all?
It is a linear model over a handful of explainable features, followed by a
sigmoid, compared against a threshold that rises while the bot is idle or has
been waiting. Every decision carries its features and threshold so a replay run
can explain itself.

Rules that bypass or veto the score, in order:

1. commands are never answered by the heart-flow (upstream already handles them);
2. a direct address (@, wake word, reply to the bot, private chat) is answered;
3. a session that has been silent for ``idle_silence_seconds`` stays silent;
4. a turn right after the bot spoke is suppressed by ``reply_cooldown_seconds``;
5. otherwise ``likelihood >= threshold`` decides.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from bot.life.config import GateConfig


@dataclass(slots=True)
class BatchFeatures:
    """Observable features of one released turn."""

    message_count: int = 1
    at_bot: bool = False
    reply_to_bot: bool = False
    wake_word: bool = False
    bot_name: bool = False
    question: bool = False
    mentions_other: bool = False
    sticker_only: bool = False
    command: bool = False
    text: str = ""

    @property
    def direct(self) -> bool:
        """Return whether the bot is directly addressed."""
        return self.at_bot or self.reply_to_bot or self.wake_word


@dataclass(slots=True)
class GateContext:
    """Per-session state the gate needs."""

    now: float
    is_private: bool = False
    last_bot_reply_ts: float = 0.0
    consecutive_waits: int = 0
    relation_score: float = 0.0


@dataclass(slots=True)
class GateDecision:
    """Outcome of one gate evaluation."""

    reply: bool
    likelihood: float
    threshold: float
    reason: str
    features: dict[str, float] = field(default_factory=dict)


def _sigmoid(value: float) -> float:
    """Return the logistic function of ``value`` without overflow.

    Args:
        value: Logit.

    Returns:
        Value in ``(0, 1)``.
    """
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp = math.exp(value)
    return exp / (1.0 + exp)


def feature_values(
    features: BatchFeatures,
    relation_score: float = 0.0,
) -> dict[str, float]:
    """Return the model input of one turn.

    This is the single source of truth for what the likelihood model sees, so a
    decision record and the score can never disagree.

    Args:
        features: Observable features of the turn.
        relation_score: 0-100 relationship score of the last sender.

    Returns:
        Mapping of feature name to numeric value.
    """
    return {
        "at_bot": 1.0 if features.at_bot else 0.0,
        "reply_to_bot": 1.0 if features.reply_to_bot else 0.0,
        "wake_word": 1.0 if features.wake_word else 0.0,
        "bot_name": 1.0 if features.bot_name else 0.0,
        "question": 1.0 if features.question else 0.0,
        "mentions_other": 1.0 if features.mentions_other else 0.0,
        "sticker_only": 1.0 if features.sticker_only else 0.0,
        "burst_length": float(max(features.message_count, 1)),
        "relation": float(max(min(relation_score, 100.0), 0.0)),
    }


def score(
    features: BatchFeatures,
    config: GateConfig,
    relation_score: float = 0.0,
) -> float:
    """Return the reply likelihood of one turn.

    Args:
        features: Observable features of the turn.
        config: Gate weights and thresholds.
        relation_score: 0-100 relationship score of the last sender.

    Returns:
        Likelihood in ``[0, 1]``.
    """
    values = feature_values(features, relation_score)
    weights = config.weights
    logit = weights.get("bias", 0.0)
    logit += sum(weights.get(name, 0.0) * value for name, value in values.items())
    return _sigmoid(logit)


def dynamic_threshold(context: GateContext, config: GateConfig) -> tuple[float, str]:
    """Return the threshold for this turn and the dominant reason.

    Idleness and consecutive waits both raise the bar: a bot that just went quiet
    should not jump into a stale conversation, and a bot that already chose to
    stay silent keeps its discipline unless something changes.

    Args:
        context: Per-session state.
        config: Gate settings.

    Returns:
        ``(threshold, reason)``; the reason is the strongest component.
    """
    threshold = config.base_threshold
    reasons: list[tuple[float, str]] = [(0.0, "base")]

    if context.last_bot_reply_ts:
        idle = max(context.now - context.last_bot_reply_ts, 0.0)
        actionable = max(config.idle_actionable_seconds, 1.0)
        if idle > actionable:
            rise = config.idle_rise_max * min((idle - actionable) / actionable, 1.0)
            threshold += rise
            reasons.append((rise, "idle"))

    if context.consecutive_waits > 0:
        penalty = min(
            context.consecutive_waits * config.backoff_step,
            config.backoff_max,
        )
        threshold += penalty
        reasons.append((penalty, "backoff"))

    top = max(reasons, key=lambda item: item[0])
    return round(min(threshold, 1.0), 4), top[1]


def evaluate(
    features: BatchFeatures,
    context: GateContext,
    config: GateConfig,
) -> GateDecision:
    """Decide whether the bot answers one released turn.

    Args:
        features: Observable features of the turn.
        context: Per-session state.
        config: Gate settings.

    Returns:
        Decision with the likelihood, the threshold in force and a reason.
    """
    likelihood = score(features, config, context.relation_score)
    threshold, threshold_reason = dynamic_threshold(context, config)
    detail = feature_values(features, context.relation_score)
    detail["message_count"] = float(max(features.message_count, 1))

    def _decision(reply: bool, reason: str, value: float) -> GateDecision:
        return GateDecision(
            reply=reply,
            likelihood=round(value, 4),
            threshold=threshold,
            reason=reason,
            features=detail,
        )

    if features.command:
        return _decision(False, "command", likelihood)

    if _in_cooldown(context, config):
        return _decision(False, "post_reply_cooldown", likelihood)

    if context.is_private or features.direct:
        # A direct address is answered; reporting threshold 0 makes the bypass
        # visible in the decision stream.
        return GateDecision(
            reply=True,
            likelihood=1.0,
            threshold=0.0,
            reason="direct_address",
            features=detail,
        )

    if config.idle_silence_seconds > 0:
        idle = (
            context.now - context.last_bot_reply_ts
            if context.last_bot_reply_ts
            else config.idle_silence_seconds
        )
        if idle >= config.idle_silence_seconds:
            return _decision(False, "idle_silence", likelihood)

    if likelihood >= threshold:
        return _decision(True, f"likelihood>=threshold({threshold_reason})", likelihood)
    return _decision(False, f"likelihood<threshold({threshold_reason})", likelihood)


def _in_cooldown(context: GateContext, config: GateConfig) -> bool:
    """Return whether the bot answered too recently to answer again.

    Args:
        context: Per-session state.
        config: Gate settings.
    """
    if not context.last_bot_reply_ts or config.reply_cooldown_seconds <= 0:
        return False
    return context.now - context.last_bot_reply_ts < config.reply_cooldown_seconds
