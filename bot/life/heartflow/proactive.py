"""Proactive talk: speak first only when the numbers say so.

This is the most conservative piece of the layer. A round runs every
``scan_interval_minutes`` and does one thing: look for sessions that have gone
quiet, whose talk value is healthy, whose daily budget is free, whose local hour
is not in quiet hours and whose last attempt was long enough ago. Only then does
it ask the chat model for a one-line opener and send it through the same sender
the rest of the Life layer uses.

Every veto leaves a reason, so "why did the bot stay silent" is always answerable
from the status block.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Sequence

from bot import logger
from bot.life.common import SessionScoped
from bot.life.config import IdleConfig, LifeConfig
from bot.life.heartflow.idle import (
    Candidate,
    TalkState,
    bump_talk,
    is_candidate,
    local_hour,
)
from bot.life.heartflow.store import TalkStore

_MAX_OPENER_CHARS = 60

_SYSTEM_PROMPT = (
    "你在一个聊天群里，想主动开个话头接上之前的对话。"
    "只输出一句中文，口语、简短（不超过 25 字），不要自我介绍，"
    "不要 Markdown，不要问“在吗”，不要重复别人的原话。"
)


class ProactiveService(SessionScoped):
    """Idle scan, opener generation and the talk-value bookkeeping."""

    def __init__(
        self,
        config: IdleConfig,
        db_helper: Any,
        archive: Any,
        send: Callable[[str, str], Awaitable[Any]],
        life_config: LifeConfig | None = None,
        summarizer: Callable[[str, str], Awaitable[str | None]] | None = None,
        focus: Any = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the service without touching the database.

        Args:
            config: Idle settings.
            db_helper: Core async database helper.
            archive: ``LifeStore`` used for session activity and context.
            send: ``(session_key, text) -> SendResult`` sender.
            life_config: Root Life configuration used for the scope allowlist.
            summarizer: Optional LLM call producing the opener.
            focus: Optional ``FocusService`` whose digest seeds the opener.
            clock: Wall-clock source; tests inject a fake one.
        """
        self.config = config
        self.store = TalkStore(db_helper)
        self.archive = archive
        self.send = send
        self.life_config = life_config
        self.summarizer = summarizer
        self.focus = focus
        self._clock = clock or time.time
        self._started = False
        self._pending: dict[str, float] = {}
        """Session key -> timestamp of the proactive message awaiting an answer."""

        self._pending_engagement: set[str] = set()
        """Sessions that answered the bot and need a talk-value bonus."""

        self._counters = {
            "rounds": 0,
            "candidates": 0,
            "sent": 0,
            "answered": 0,
            "ignored": 0,
            "skipped": 0,
            "failed": 0,
        }

    async def initialize(self) -> None:
        """Create the talk table and mark the service ready."""
        await self.store.ensure_schema()
        self._started = True
        logger.info(
            "[life] proactive talk ready; idle=%d-%d min daily_cap=%d",
            self.config.min_idle_minutes,
            self.config.max_idle_minutes,
            self.config.daily_cap,
        )

    async def close(self) -> None:
        """Stop the service."""
        self._started = False

    async def scan(self, now: float | None = None) -> list[Candidate]:
        """Return the sessions that may be spoken to first.

        Args:
            now: Current wall-clock timestamp.

        Returns:
            Candidates ordered by idleness (oldest first).
        """
        moment = now or self._clock()
        await self.flush_engagement()
        since = int(moment - max(self.config.max_idle_minutes, 1) * 60)
        rows = await self.archive.session_last_ts(
            since_ts=since,
            limit=self.config.max_scan_sessions,
        )
        keys = [str(row["session_key"]) for row in rows]
        states = await self.store.get_many(keys)
        candidates: list[Candidate] = []
        for row in rows:
            session_key = str(row["session_key"])
            if not self.session_enabled(session_key):
                continue
            state = states.get(session_key) or TalkState(session_key=session_key)
            if state.pending_ts and session_key not in self._pending:
                # Rebuilt after a restart: the row remembers the attempt.
                self._pending[session_key] = state.pending_ts
            await self._settle_pending(session_key, state, moment)
            if session_key in self._pending:
                # Still waiting for an answer to the previous opener.
                continue
            idle_minutes = max(moment - float(row["last_ts"]), 0.0) / 60.0
            allowed, _reason = is_candidate(state, idle_minutes, moment, self.config)
            if not allowed:
                continue
            candidates.append(
                Candidate(
                    session_key=session_key,
                    idle_minutes=idle_minutes,
                    talk_value=state.talk_value,
                    attempts=state.attempts,
                )
            )
        candidates.sort(key=lambda item: item.idle_minutes, reverse=True)
        self._counters["candidates"] += len(candidates)
        return candidates

    async def run_round(self, now: float | None = None) -> dict[str, Any]:
        """Scan once and send the allowed proactive messages.

        Args:
            now: Current wall-clock timestamp.

        Returns:
            Round counters.
        """
        if not self._started or not self.config.enabled:
            return {"enabled": False}
        moment = now or self._clock()
        if self.summarizer is None:
            return {"enabled": True, "skipped": "no_provider"}
        self._counters["rounds"] += 1
        candidates = await self.scan(moment)
        sent = 0
        for candidate in candidates[: max(self.config.max_per_round, 1)]:
            text = await self._compose(candidate)
            if not text:
                self._counters["skipped"] += 1
                continue
            try:
                result = await self.send(candidate.session_key, text)
            except Exception:
                logger.exception("[life] proactive send failed")
                self._counters["failed"] += 1
                continue
            if not getattr(result, "ok", False):
                self._counters["failed"] += 1
                continue
            await self._mark_sent(candidate.session_key, moment)
            sent += 1
        return {
            "enabled": True,
            "candidates": len(candidates),
            "sent": sent,
        }

    async def observe_messages(self, batch: Sequence[Any]) -> int:
        """Mark pending proactive messages as answered.

        Args:
            batch: ``IngestedMessage`` objects from the ingest flush.

        Returns:
            Number of sessions marked answered.
        """
        if not self._started or not self._pending:
            return 0
        answered = 0
        for message in batch:
            session_key = str(getattr(message, "session_key", "") or "")
            pending_ts = self._pending.get(session_key)
            if not pending_ts:
                continue
            ts = float(getattr(message, "ts", 0) or 0)
            if ts < pending_ts:
                continue
            state = await self.store.get(session_key)
            state.answered += 1
            state.attempts = 0
            state.pending_ts = 0.0
            state.talk_value = bump_talk(state.talk_value, self.config.answer_bonus)
            await self.store.save(state)
            self._pending.pop(session_key, None)
            self._counters["answered"] += 1
            answered += 1
        return answered

    def note_engagement(self, session_key: str) -> None:
        """Queue a talk-value bonus for one answered turn.

        Args:
            session_key: Session the bot answered in.
        """
        if not self._started or not self.config.enabled:
            return
        self._pending_engagement.add(session_key)

    async def flush_engagement(self) -> int:
        """Persist queued engagement bonuses.

        Returns:
            Number of sessions updated.
        """
        if not self._pending_engagement:
            return 0
        keys = list(self._pending_engagement)
        self._pending_engagement.clear()
        states = await self.store.get_many(keys)
        for key in keys:
            state = states.get(key) or TalkState(session_key=key)
            state.talk_value = bump_talk(state.talk_value, self.config.answer_bonus)
            await self.store.save(state)
        return len(keys)

    async def status(self, now: float | None = None) -> dict[str, Any]:
        """Return a serializable status block.

        Args:
            now: Current wall-clock timestamp.

        Returns:
            Mapping with switches, counters, storage and parked sessions.
        """
        moment = now or self._clock()
        storage = await self.store.stats()
        return {
            "enabled": self._started and self.config.enabled,
            "configured": self.config.enabled,
            "idle_window_minutes": [
                self.config.min_idle_minutes,
                self.config.max_idle_minutes,
            ],
            "daily_cap": self.config.daily_cap,
            "min_talk_value": self.config.min_talk_value,
            "quiet_hours": list(self.config.quiet_hours),
            "local_hour": local_hour(moment),
            "pending_answers": len(self._pending),
            "storage": storage,
            "counters": dict(self._counters),
        }

    async def session_state(self, session_key: str) -> dict[str, Any]:
        """Return the stored talk state of one session.

        Args:
            session_key: unified_msg_origin.

        Returns:
            Serialized talk state.
        """
        state = await self.store.get(session_key)
        return {
            "session_key": state.session_key,
            "talk_value": round(state.talk_value, 3),
            "attempts": state.attempts,
            "answered": state.answered,
            "last_proactive_ts": state.last_proactive_ts,
            "pending_ts": state.pending_ts,
            "sent_today": state.sent_today,
        }

    async def _settle_pending(
        self,
        session_key: str,
        state: TalkState,
        now: float,
    ) -> None:
        """Punish a proactive message that nobody answered in time.

        Args:
            session_key: Session to settle.
            state: Current talk state.
            now: Current wall-clock timestamp.
        """
        pending_ts = self._pending.get(session_key)
        if not pending_ts:
            return
        window = max(self.config.answer_window_minutes, 1) * 60
        if now - pending_ts < window:
            return
        state.attempts += 1
        state.pending_ts = 0.0
        state.talk_value = bump_talk(state.talk_value, -self.config.ignore_penalty)
        await self.store.save(state)
        self._pending.pop(session_key, None)
        self._counters["ignored"] += 1

    async def _mark_sent(self, session_key: str, now: float) -> None:
        """Record one sent proactive message.

        Args:
            session_key: Session the message went to.
            now: Current wall-clock timestamp.
        """
        state = await self.store.get(session_key)
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if state.sent_day != day:
            state.sent_day = day
            state.sent_today = 0
        state.sent_today += 1
        state.last_proactive_ts = now
        state.pending_ts = now
        await self.store.save(state)
        self._pending[session_key] = now
        self._counters["sent"] += 1

    async def _compose(self, candidate: Candidate) -> str | None:
        """Ask the chat model for a one-line opener.

        Args:
            candidate: Session to open a conversation in.

        Returns:
            Opener text, or None when the model is unavailable or empty.
        """
        if self.summarizer is None:
            return None
        parts: list[str] = []
        if self.focus is not None:
            try:
                timeline = await self.focus.timeline(candidate.session_key)
                if timeline.digest:
                    parts.append(f"更早的对话摘要：{timeline.digest[:400]}")
            except Exception:
                logger.exception("[life] proactive digest lookup failed")
        try:
            rows = await self.archive.fetch_range(
                candidate.session_key,
                since_ts=int(self._clock() - 6 * 3600),
                limit=8,
            )
        except Exception:
            rows = []
        lines = [
            f"{row.get('sender_name') or '某人'}: {str(row.get('plain_text') or '')[:80]}"
            for row in rows
            if str(row.get("plain_text") or "").strip()
        ]
        if not lines and not parts:
            return None
        payload = "\n".join(parts + ["最近的对话："] + lines)
        try:
            text = await self.summarizer(_SYSTEM_PROMPT, payload)
        except Exception:
            logger.exception("[life] proactive opener failed")
            return None
        if not text:
            return None
        cleaned = " ".join(text.replace('"', "").replace("“", "").split())
        return cleaned[:_MAX_OPENER_CHARS] or None
