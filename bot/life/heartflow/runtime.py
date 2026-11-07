"""Turn runtime: hold an ambient message, decide, then hand it back.

The runtime is the piece that makes ``heartflow.enabled`` visible to the bot. It
does not call a model and it does not send anything: it owns the *timing* of a
turn and the decision whether the turn deserves an answer at all.

Flow for one ambient group message:

1. the stage registers the message and awaits the debounce window;
2. the first message of a session becomes the *leader*: it owns the window and
   re-checks it whenever a new message extends the plan, so a burst is merged
   into one turn instead of one reply per message;
3. when the window closes the leader scores the merged turn with the same gate
   the replay set uses, records the decision, and wakes every waiter;
4. the leader either continues the pipeline (with the merged text and a forced
   wake) or stops its own event; the waiters always stop, because their text is
   already part of the merged turn.

Only ambient group messages reach this runtime: private chats, ``@``/reply
messages, wake-prefixed commands and button interactions pass straight through
to the upstream pipeline untouched.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from bot import logger
from bot.life.config import HeartFlowConfig
from bot.life.heartflow.debounce import DebouncePlan, PendingMessage, plan_release
from bot.life.heartflow.gates import GateContext, GateDecision, evaluate
from bot.life.heartflow.policy import PolicyMessage, batch_features

ACTION_CONTINUE = "continue"
ACTION_HOLD = "hold"

_MAX_TRACKED_SESSIONS = 1000


@dataclass(slots=True)
class TurnEntry:
    """One message waiting inside a turn."""

    session_key: str
    text: str
    ts: float
    sender_id: str | None = None
    sender_name: str | None = None
    message_id: str = ""
    flags: dict = field(default_factory=dict)

    def to_policy(self) -> PolicyMessage:
        """Return the entry in the shape the shared gate helpers expect.

        Returns:
            Policy message with ``is_bot=False``.
        """
        return PolicyMessage(
            message_id=self.message_id,
            ts=self.ts,
            sender_id=self.sender_id,
            sender_name=self.sender_name,
            text=self.text,
            flags=self.flags,
        )


@dataclass(slots=True)
class TurnOutcome:
    """What the stage should do with the event it is holding."""

    action: str
    reason: str
    merged_text: str = ""
    force_wake: bool = False
    decision: GateDecision | None = None


@dataclass(slots=True)
class SessionState:
    """Gate state of one session."""

    last_bot_reply_ts: float = 0.0
    waits: int = 0


class TurnBatch:
    """One in-flight debounce batch."""

    def __init__(self, session_key: str, entry: TurnEntry) -> None:
        """Create a batch with its first entry.

        Args:
            session_key: Session owning the turn.
            entry: First message of the turn.
        """
        self.session_key = session_key
        self.entries: list[TurnEntry] = [entry]
        self.released = asyncio.Event()
        self.decision: GateDecision | None = None
        self.action: str = ACTION_HOLD
        self.merged_text: str = ""

    def plan(self, config: HeartFlowConfig) -> DebouncePlan:
        """Return the current debounce plan of the batch.

        Args:
            config: Heart-flow settings.

        Returns:
            Release plan for the entries collected so far.
        """
        return plan_release(
            [
                PendingMessage(
                    message_id=entry.message_id,
                    ts=entry.ts,
                    sender_id=entry.sender_id,
                    direct=False,
                )
                for entry in self.entries
            ],
            config.debounce,
        )

    def merged(self) -> str:
        """Return the joined text of every entry, oldest first.

        Returns:
            Lines formatted as ``speaker: text``, empty when nothing was said.
        """
        return "\\n".join(
            f"{entry.sender_name or '某人'}: {entry.text}"
            for entry in self.entries
            if entry.text.strip()
        )


class TurnRuntime:
    """Owns turns, debounce windows and the gate decision at runtime."""

    def __init__(
        self,
        config: HeartFlowConfig,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], Any] | None = None,
    ) -> None:
        """Initialize the runtime without starting any task.

        Args:
            config: Heart-flow settings (runtime, debounce, gate, bot names).
            clock: Wall-clock source; tests inject a fake one.
            sleeper: Awaitable sleep; tests inject an instant one.
        """
        self.config = config
        self.runtime_config = config.runtime
        self._clock = clock or time.time
        self._sleep = sleeper or asyncio.sleep
        self._batches: dict[str, TurnBatch] = {}
        self._sessions: dict[str, SessionState] = {}
        self._recent: deque[dict] = deque(
            maxlen=max(self.runtime_config.recent_size, 1)
        )
        self._closed = False
        self._counters = {
            "turns": 0,
            "merged": 0,
            "replied": 0,
            "waited": 0,
            "shadowed": 0,
            "capacity_drops": 0,
            "aborted": 0,
        }

    @property
    def closed(self) -> bool:
        """Return whether the runtime stopped accepting turns."""
        return self._closed

    async def handle(self, session_key: str, entry: TurnEntry) -> TurnOutcome:
        """Register one message and wait for its turn to be released.

        Args:
            session_key: unified_msg_origin of the message.
            entry: Message to merge into the current turn.

        Returns:
            Outcome telling the stage to continue or stop the event.
        """
        if self._closed:
            return TurnOutcome(ACTION_CONTINUE, "runtime_closed")
        batch = self._batches.get(session_key)
        if batch is not None:
            batch.entries.append(entry)
            self._counters["merged"] += 1
            await batch.released.wait()
            return TurnOutcome(
                ACTION_HOLD,
                f"merged:{batch.decision.reason if batch.decision else 'released'}",
                batch.merged_text,
                decision=batch.decision,
            )
        if len(self._batches) >= max(self.runtime_config.max_pending_turns, 1):
            self._counters["capacity_drops"] += 1
            return TurnOutcome(ACTION_HOLD, "capacity")
        batch = TurnBatch(session_key, entry)
        self._batches[session_key] = batch
        return await self._lead(batch)

    async def _lead(self, batch: TurnBatch) -> TurnOutcome:
        """Own one turn: wait for the window, decide, then wake the waiters.

        Args:
            batch: Batch created for this turn.

        Returns:
            Outcome for the leader's event.
        """
        try:
            await self._wait_for_window(batch)
            decision = self._decide(batch)
            batch.decision = decision
            batch.merged_text = batch.merged()
            self._counters["turns"] += 1
            self._record(batch, decision)
            if decision.reply:
                if self.runtime_config.shadow:
                    self._counters["shadowed"] += 1
                    batch.action = ACTION_HOLD
                else:
                    self._counters["replied"] += 1
                    batch.action = ACTION_CONTINUE
            else:
                self._counters["waited"] += 1
                batch.action = ACTION_HOLD
            return TurnOutcome(
                batch.action,
                decision.reason,
                batch.merged_text,
                force_wake=batch.action == ACTION_CONTINUE,
                decision=decision,
            )
        except asyncio.CancelledError:
            self._counters["aborted"] += 1
            raise
        except Exception:
            logger.exception("[life] heart-flow turn failed")
            self._counters["aborted"] += 1
            return TurnOutcome(ACTION_HOLD, "turn_failed")
        finally:
            self._batches.pop(batch.session_key, None)
            batch.released.set()

    async def _wait_for_window(self, batch: TurnBatch) -> DebouncePlan:
        """Sleep until the batch is due, re-checking the plan after each wait.

        A new message extends the plan while the leader sleeps, so the leader
        loops instead of trusting the first computed timestamp. The wait is also
        capped by ``max_wait_seconds`` so a message with a future timestamp
        cannot hold an event forever.

        Args:
            batch: Batch to wait for.

        Returns:
            The plan that was due.
        """
        deadline = self._clock() + max(self.runtime_config.max_wait_seconds, 0.0)
        plan = batch.plan(self.config)
        while True:
            now = self._clock()
            if plan.release_at <= now or now >= deadline:
                return plan
            await self._sleep(min(plan.release_at, deadline) - now)
            plan = batch.plan(self.config)

    def _decide(self, batch: TurnBatch) -> GateDecision:
        """Score the merged turn with the shared gate.

        Args:
            batch: Batch holding the turn.

        Returns:
            Gate decision.
        """
        state = self._session_state(batch.session_key)
        features = batch_features(
            [entry.to_policy() for entry in batch.entries],
            self.config.bot_names,
        )
        context = GateContext(
            now=self._clock(),
            is_private=False,
            last_bot_reply_ts=state.last_bot_reply_ts,
            consecutive_waits=state.waits,
        )
        decision = evaluate(features, context, self.config.gate)
        if decision.reply:
            state.waits = 0
            state.last_bot_reply_ts = self._clock()
        else:
            state.waits += 1
        return decision

    def _session_state(self, session_key: str) -> SessionState:
        """Return the gate state of one session, creating it on first use.

        Args:
            session_key: unified_msg_origin.

        Returns:
            Session gate state.
        """
        state = self._sessions.get(session_key)
        if state is None:
            if len(self._sessions) >= _MAX_TRACKED_SESSIONS:
                self._sessions.pop(next(iter(self._sessions)), None)
            state = SessionState()
            self._sessions[session_key] = state
        return state

    def _record(self, batch: TurnBatch, decision: GateDecision) -> None:
        """Append one decision to the in-memory ring buffer.

        Args:
            batch: Released batch.
            decision: Gate decision of the turn.
        """
        self._recent.append(
            {
                "ts": round(self._clock(), 2),
                "session_key": batch.session_key,
                "messages": len(batch.entries),
                "decide": "reply" if decision.reply else "wait",
                "reason": decision.reason,
                "likelihood": decision.likelihood,
                "threshold": decision.threshold,
                "shadow": self.runtime_config.shadow,
                "text": batch.merged_text[:200],
            }
        )

    async def close(self) -> None:
        """Stop accepting turns and release every waiter."""
        self._closed = True
        for batch in list(self._batches.values()):
            batch.released.set()
        self._batches.clear()

    def status(self) -> dict[str, Any]:
        """Return a serializable status snapshot.

        Returns:
            Mapping with switches, pending turns, counters and recent decisions.
        """
        return {
            "enabled": not self._closed,
            "shadow": self.runtime_config.shadow,
            "max_pending_turns": self.runtime_config.max_pending_turns,
            "bot_names": list(self.config.bot_names),
            "pending_sessions": len(self._batches),
            "counters": dict(self._counters),
            "recent": list(reversed(self._recent)),
        }
