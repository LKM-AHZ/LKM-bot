"""Message debounce for the heart-flow runtime.

The bot should answer a *turn*, not a keystroke. Debounce collects the messages
of one session for a quiet window and releases them together:

- three tiers: quick (direct address), natural (default) and calm (busy turn);
- a hard cap of ``hard_cap_seconds`` measured from the first merged message, so
  a person who never stops typing cannot hold the turn forever;
- one person sending several messages in a row extends the window once, so the
  first half of a sentence does not trigger a reply;
- the same person continuing after the window still merges into the turn.

Everything here is pure and clock-free: callers pass timestamps and read
``release_at``. The stateful accumulator only stores messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bot.life.config import DebounceConfig

TIER_QUICK = "quick"
TIER_NATURAL = "natural"
TIER_CALM = "calm"

CALM_MESSAGE_COUNT = 3
"""A turn of this many messages is already busy; wait for it to settle."""


@dataclass(slots=True)
class PendingMessage:
    """One message waiting inside a debounce batch."""

    message_id: str
    ts: float
    sender_id: str | None = None
    direct: bool = False
    """True when the message addresses the bot (@, wake word, reply to the bot)."""


@dataclass(slots=True)
class DebouncePlan:
    """When the current batch should be released."""

    tier: str
    release_at: float
    reason: str
    direct: bool
    message_count: int = 0
    message_ids: list[str] = field(default_factory=list)


def plan_release(
    messages: list[PendingMessage], config: DebounceConfig
) -> DebouncePlan:
    """Compute the release time of one debounce batch.

    The release time is a wall-clock timestamp; when it already lies in the past
    the batch is due immediately (the hard cap fired while messages kept coming).

    Args:
        messages: Messages merged so far, in arrival order.
        config: Debounce settings.

    Returns:
        Plan with the chosen tier, release timestamp and a human-readable reason.

    Raises:
        ValueError: When ``messages`` is empty.
    """
    if not messages:
        raise ValueError("cannot plan an empty debounce batch")
    first = messages[0]
    last = messages[-1]
    direct = any(message.direct for message in messages)
    count = len(messages)
    if not config.enabled:
        return DebouncePlan(
            tier=TIER_QUICK,
            release_at=last.ts,
            reason="debounce_disabled",
            direct=direct,
            message_count=count,
            message_ids=[message.message_id for message in messages],
        )

    if direct:
        tier = TIER_QUICK
    elif count >= CALM_MESSAGE_COUNT:
        tier = TIER_CALM
    else:
        tier = TIER_NATURAL
    window = {
        TIER_QUICK: config.quick_seconds,
        TIER_NATURAL: config.natural_seconds,
        TIER_CALM: config.calm_seconds,
    }[tier]

    # One person typing several lines is still the same turn: wait a bit longer
    # once, instead of dropping the second half of the sentence.
    senders = {message.sender_id for message in messages}
    # Unknown senders must not be merged as if they were one person.
    same_sender = len(senders) == 1 and None not in senders and count > 1
    if same_sender:
        gaps = [later.ts - earlier.ts for earlier, later in zip(messages, messages[1:])]
        if max(gaps, default=0.0) <= config.same_sender_gap_seconds:
            window += config.same_sender_extra_seconds

    release_at = min(last.ts + window, first.ts + config.hard_cap_seconds)
    if direct:
        # A direct address should never wait for the calm tier to expire.
        release_at = min(release_at, last.ts + config.quick_seconds)

    reason = f"tier={tier};same_sender={same_sender}"
    if release_at >= first.ts + config.hard_cap_seconds:
        reason = f"{reason};hard_cap"
    return DebouncePlan(
        tier=tier,
        release_at=release_at,
        reason=reason,
        direct=direct,
        message_count=count,
        message_ids=[message.message_id for message in messages],
    )


class DebounceAccumulator:
    """Per-session debounce buffer used by the runtime.

    The accumulator is intentionally small: it stores messages and answers three
    questions — is the turn due, what would be released, and what was released.
    """

    def __init__(self, config: DebounceConfig) -> None:
        """Initialize the accumulator.

        Args:
            config: Debounce settings.
        """
        self.config = config
        self.messages: list[PendingMessage] = []

    def __bool__(self) -> bool:
        """Return whether the buffer holds messages."""
        return bool(self.messages)

    def add(self, message: PendingMessage) -> DebouncePlan:
        """Append one message and recompute the release plan.

        Args:
            message: Message to merge into the turn.

        Returns:
            Updated plan.
        """
        self.messages.append(message)
        return plan_release(self.messages, self.config)

    def plan(self) -> DebouncePlan | None:
        """Return the current plan, or None when the buffer is empty."""
        if not self.messages:
            return None
        return plan_release(self.messages, self.config)

    def due(self, now: float) -> bool:
        """Return whether the buffered turn should be released at ``now``.

        Args:
            now: Current wall-clock timestamp.
        """
        plan = self.plan()
        return plan is not None and plan.release_at <= now

    def pop(self) -> tuple[list[PendingMessage], DebouncePlan | None]:
        """Remove and return every buffered message.

        Returns:
            ``(messages, plan)``; both empty/None when nothing was buffered.
        """
        messages = self.messages
        plan = plan_release(messages, self.config) if messages else None
        self.messages = []
        return messages, plan
