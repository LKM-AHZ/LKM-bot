"""Heart-flow decision policy: debounce a stream, then gate every turn.

The policy is shared by the runtime (one session at a time) and the offline
replay runner (a whole corpus). Sharing it is the point: the reply/no-reply
score measured on the replay set is produced by the exact code path the bot uses
at runtime, minus the language model.

The simulation keeps per-session state (last bot activity, consecutive waits) so
a corpus replays like a live session. Bot messages are passed through the
simulation: they end the current turn, update the "last bot reply" clock and
reset the wait streak.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bot.life.config import HeartFlowConfig
from bot.life.heartflow.debounce import (
    DebouncePlan,
    PendingMessage,
    plan_release,
)
from bot.life.heartflow.gates import BatchFeatures, GateContext, GateDecision, evaluate

BOT_ROLES = frozenset({"bot", "assistant", "model"})
"""Corpus roles that mean "this message was sent by the bot itself"."""


@dataclass(slots=True)
class PolicyMessage:
    """One corpus or runtime message in the shape the policy needs."""

    message_id: str
    ts: float
    sender_id: str | None = None
    sender_name: str | None = None
    text: str = ""
    is_bot: bool = False
    flags: dict = field(default_factory=dict)
    reply_to: str | None = None


@dataclass(slots=True)
class PolicyDecision:
    """Decision for one message of the stream."""

    message_id: str
    decide: str
    reason: str
    release_at: float
    tier: str = ""
    likelihood: float = 0.0
    threshold: float = 0.0
    merged: bool = False


def to_pending(
    message: PolicyMessage,
    bot_names: list[str] | None = None,
) -> PendingMessage:
    """Convert a policy message into a debounce item.

    A wake word in the text also counts as a direct address: the runtime learns
    that from ``is_wake``, the replay corpus only has the text.

    Args:
        message: Message to convert.
        bot_names: Names and wake words that address the bot.

    Returns:
        Pending message with the direct-address flag derived from flags and text.
    """
    flags = message.flags or {}
    lowered = message.text.lower()
    direct = bool(flags.get("at") or flags.get("reply_to_bot")) or any(
        name.strip().lower() in lowered for name in (bot_names or []) if name.strip()
    )
    return PendingMessage(
        message_id=message.message_id,
        ts=message.ts,
        sender_id=message.sender_id,
        direct=direct,
    )


def batch_features(
    messages: list[PolicyMessage],
    bot_names: list[str],
) -> BatchFeatures:
    """Derive gate features from one debounced turn.

    Args:
        messages: Messages merged into the turn.
        bot_names: Names and wake words that address the bot.

    Returns:
        Aggregated features.
    """
    text = " ".join(message.text for message in messages if message.text)
    lowered = text.lower()
    names = [name.strip().lower() for name in bot_names if name.strip()]
    features = BatchFeatures(message_count=len(messages), text=text)
    for message in messages:
        flags = message.flags or {}
        features.at_bot = features.at_bot or bool(flags.get("at"))
        features.reply_to_bot = features.reply_to_bot or bool(flags.get("reply_to_bot"))
        features.command = features.command or bool(flags.get("is_command"))
        features.mentions_other = features.mentions_other or bool(
            flags.get("mentions_other")
        )
    features.wake_word = any(name in lowered for name in names)
    features.bot_name = features.wake_word
    features.question = "?" in text or "？" in text
    features.sticker_only = bool(
        not text.strip()
        and any(bool((message.flags or {}).get("image")) for message in messages)
    )
    return features


class HeartFlowPolicy:
    """Debounce + gate over one session or a whole replay corpus."""

    def __init__(
        self,
        config: HeartFlowConfig,
        bot_names: list[str] | None = None,
    ) -> None:
        """Initialize the policy.

        Args:
            config: Heart-flow settings (debounce and gate).
            bot_names: Names and wake words that address the bot.
        """
        self.config = config
        self.bot_names = list(bot_names) if bot_names else []

    def decide_session(
        self,
        messages: list[PolicyMessage],
        *,
        is_private: bool = False,
    ) -> list[PolicyDecision]:
        """Replay one session through debounce and the gate.

        Args:
            messages: Session messages in arrival order.
            is_private: Whether the session is a private chat.

        Returns:
            One decision per message, bot messages included.
        """
        decisions: list[PolicyDecision] = []
        last_bot_reply_ts = 0.0
        waits = 0

        def flush(batch: list[PolicyMessage], plan: DebouncePlan) -> None:
            nonlocal last_bot_reply_ts, waits
            features = batch_features(batch, self.bot_names)
            context = GateContext(
                now=plan.release_at,
                is_private=is_private,
                last_bot_reply_ts=last_bot_reply_ts,
                consecutive_waits=waits,
            )
            decision = evaluate(features, context, self.config.gate)
            anchor = batch[-1].message_id
            for item in batch:
                if item.message_id == anchor:
                    decisions.append(
                        PolicyDecision(
                            message_id=item.message_id,
                            decide="reply" if decision.reply else "wait",
                            reason=decision.reason,
                            release_at=plan.release_at,
                            tier=plan.tier,
                            likelihood=decision.likelihood,
                            threshold=decision.threshold,
                        )
                    )
                else:
                    decisions.append(
                        PolicyDecision(
                            message_id=item.message_id,
                            decide="wait",
                            reason="merged_batch",
                            release_at=plan.release_at,
                            tier=plan.tier,
                            merged=True,
                        )
                    )
            if decision.reply:
                waits = 0
                last_bot_reply_ts = plan.release_at
            else:
                waits += 1

        batch: list[PolicyMessage] = []
        for message in messages:
            if batch:
                plan = self._plan(batch)
                if plan.release_at <= message.ts:
                    flush(batch, plan)
                    batch = []
            if message.is_bot:
                # The bot spoke: whatever is still buffered belongs to the turn
                # it just answered, so release it instead of letting it wait.
                if batch:
                    flush(batch, self._plan(batch))
                    batch = []
                decisions.append(
                    PolicyDecision(
                        message_id=message.message_id,
                        decide="wait",
                        reason="bot_message",
                        release_at=message.ts,
                    )
                )
                last_bot_reply_ts = message.ts
                waits = 0
                continue
            batch.append(message)

        if batch:
            flush(batch, self._plan(batch))
        return decisions

    def preview(
        self,
        messages: list[PolicyMessage],
        *,
        is_private: bool = False,
        now: float | None = None,
    ) -> tuple[DebouncePlan | None, GateDecision | None]:
        """Return the plan and gate decision for an in-flight turn.

        Args:
            messages: Messages merged so far.
            is_private: Whether the session is a private chat.
            now: Optional clock for the gate context.

        Returns:
            ``(plan, gate_decision)``; ``(None, None)`` for an empty turn.
        """
        if not messages:
            return None, None
        plan = self._plan(messages)
        context = GateContext(
            now=now if now is not None else plan.release_at,
            is_private=is_private,
        )
        return plan, evaluate(
            batch_features(messages, self.bot_names), context, self.config.gate
        )

    def _plan(self, batch: list[PolicyMessage]) -> DebouncePlan:
        """Return the debounce plan of one batch.

        Args:
            batch: Messages merged so far.

        Returns:
            Release plan for the batch.
        """
        return plan_release(
            [to_pending(item, self.bot_names) for item in batch],
            self.config.debounce,
        )
