"""Focus runtime: per-session timelines, compression and checkpoints.

The service is the only place that knows *when* to build a timeline and when to
checkpoint it:

- the ingest flush feeds archived messages into the timeline;
- a successful proactive send closes the turn with the bot reply;
- a dedicated loop flushes dirty timelines every few minutes, so a crash loses
  at most one interval of turns;
- the prompt hook appends the folded digest of older turns.

The timeline itself is pure (``timeline.py``); everything here is scheduling,
caching and persistence.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Sequence

from bot import logger
from bot.life.config import FocusConfig, LifeConfig
from bot.life.heartflow.focus.store import FocusStore
from bot.life.heartflow.focus.timeline import (
    ROLE_BOT,
    ROLE_USER,
    FocusMessage,
    FocusTimeline,
)

_BLOCK_TAG = "life_focus"
_MEDIA_MARKERS = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
}


class FocusService:
    """Timeline cache, compression loop and prompt block."""

    def __init__(
        self,
        db_helper: Any,
        config: FocusConfig,
        life_config: LifeConfig | None = None,
    ) -> None:
        """Initialize the service without touching the database.

        Args:
            db_helper: Core async database helper.
            config: Focus settings.
            life_config: Root Life configuration used for the scope allowlist.
        """
        self.db = db_helper
        self.config = config
        self.life_config = life_config
        self.store = FocusStore(db_helper)
        self._timelines: OrderedDict[str, FocusTimeline] = OrderedDict()
        self._dirty_turns: set[tuple[str, int]] = set()
        self._dirty_sessions: set[str] = set()
        self._lock = asyncio.Lock()
        self._started = False
        self._counters = {
            "observed": 0,
            "turns": 0,
            "folded": 0,
            "checkpoints": 0,
            "injections": 0,
        }

    async def initialize(self) -> None:
        """Create the focus tables and mark the runtime ready."""
        await self.store.ensure_schema()
        self._started = True
        logger.info(
            "[life] focus ready; keep_turns=%d digest_max_chars=%d",
            self.config.keep_turns,
            self.config.digest_max_chars,
        )

    async def close(self) -> None:
        """Flush every dirty timeline and stop the runtime."""
        if not self._started:
            return
        await self.checkpoint_all()
        self._started = False

    def session_enabled(self, umo: str, group_id: str | None = None) -> bool:
        """Return whether a session is inside the Life allowlist.

        Args:
            umo: Session key.
            group_id: Group id, or None.

        Returns:
            True when the session may keep a timeline.
        """
        if self.life_config is None:
            return True
        return bool(self.life_config.session_enabled(umo, group_id))

    async def timeline(self, session_key: str) -> FocusTimeline:
        """Return the cached timeline, loading it from storage when needed.

        Args:
            session_key: unified_msg_origin.

        Returns:
            Timeline of the session, empty when it was never seen.
        """
        cached = self._timelines.get(session_key)
        if cached is not None:
            self._timelines.move_to_end(session_key)
            return cached
        payload = await self.store.load(session_key, limit=self.config.max_turns)
        timeline = FocusTimeline.from_checkpoint(
            payload,
            keep_turns=self.config.keep_turns,
            turn_max_chars=self.config.turn_max_chars,
            digest_max_chars=self.config.digest_max_chars,
            block_max_chars=self.config.block_max_chars,
        )
        if timeline is None:  # pragma: no cover - load() always sets a session
            timeline = FocusTimeline(session_key=session_key)
        if len(self._timelines) >= self.config.max_sessions:
            oldest_key, oldest = next(iter(self._timelines.items()))
            if oldest_key in self._dirty_sessions:
                await self._checkpoint(oldest_key, oldest)
            self._timelines.pop(oldest_key, None)
        self._timelines[session_key] = timeline
        return timeline

    async def observe_messages(self, batch: Sequence[Any]) -> int:
        """Feed archived messages into the timelines.

        Args:
            batch: ``IngestedMessage`` objects from the ingest flush.

        Returns:
            Number of messages recorded.
        """
        if not self._started or not self.config.enabled or not batch:
            return 0
        recorded = 0
        touched: set[str] = set()
        async with self._lock:
            for message in batch:
                session_key = str(getattr(message, "session_key", "") or "")
                if not session_key:
                    continue
                if not self.session_enabled(
                    session_key,
                    getattr(message, "group_id", None),
                ):
                    continue
                text = self._message_text(message)
                if not text:
                    continue
                timeline = await self.timeline(session_key)
                turn = timeline.add_message(
                    FocusMessage(
                        role=ROLE_USER,
                        text=text,
                        ts=float(getattr(message, "ts", 0) or 0),
                        sender_name=getattr(message, "sender_name", None),
                        sender_id=getattr(message, "sender_id", None),
                        message_id=str(getattr(message, "message_id", "") or ""),
                    ),
                    gap_seconds=self.config.gap_seconds,
                )
                self._dirty_turns.add((session_key, turn.index))
                touched.add(session_key)
                recorded += 1
            self._counters["observed"] += recorded
            for session_key in touched:
                timeline = self._timelines.get(session_key)
                if timeline is not None:
                    await self._checkpoint(session_key, timeline)
        return recorded

    async def note_reply(
        self,
        session_key: str,
        text: str,
        ts: float | None = None,
    ) -> bool:
        """Record a bot reply and close the current turn.

        Args:
            session_key: unified_msg_origin.
            text: Reply text as sent.
            ts: Send timestamp; defaults to now.

        Returns:
            True when the reply was recorded.
        """
        if not self._started or not self.config.enabled:
            return False
        if not session_key or not text.strip():
            return False
        async with self._lock:
            timeline = await self.timeline(session_key)
            turn = timeline.add_reply(
                FocusMessage(
                    role=ROLE_BOT,
                    text=text.strip(),
                    ts=float(ts if ts is not None else time.time()),
                )
            )
            # The reply closes the turn, so the row persisted as ``open`` has
            # to be written again with its new status.
            self._dirty_turns.add((session_key, turn.index))
            await self._checkpoint(session_key, timeline)
        self._counters["observed"] += 1
        return True

    async def checkpoint_all(self) -> int:
        """Flush every dirty timeline.

        Returns:
            Number of timelines flushed.
        """
        async with self._lock:
            flushed = 0
            for session_key in list(self._dirty_sessions):
                timeline = self._timelines.get(session_key)
                if timeline is None:
                    self._dirty_sessions.discard(session_key)
                    continue
                await self._checkpoint(session_key, timeline)
                flushed += 1
            return flushed

    async def compress_round(self) -> dict[str, Any]:
        """Flush every cached timeline and fold the old turns.

        Returns:
            Round counters.
        """
        if not self._started:
            return {"enabled": False}
        folded = 0
        flushed = 0
        async with self._lock:
            for session_key, timeline in list(self._timelines.items()):
                before = timeline.compressed_turns
                await self._checkpoint(session_key, timeline)
                folded += timeline.compressed_turns - before
                flushed += 1
        return {"enabled": True, "sessions": flushed, "folded": folded}

    async def inject(self, event: Any, req: Any) -> bool:
        """Append the folded digest to the system prompt.

        Only folded turns are injected: the recent conversation is already part
        of the model context, so repeating it would waste tokens.

        Args:
            event: Bot message event carrying the session identity.
            req: ``ProviderRequest`` about to be sent to the model.

        Returns:
            True when the prompt was modified.
        """
        if not self._started or not self.config.enabled or not self.config.inject:
            return False
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return False
        getter = getattr(event, "get_group_id", None)
        group_id = getter() if callable(getter) else None
        if not self.session_enabled(umo, group_id):
            return False
        system_prompt = getattr(req, "system_prompt", "") or ""
        if f"<{_BLOCK_TAG}" in system_prompt:
            return False
        timeline = await self.timeline(umo)
        block = timeline.render_digest_block()
        if not block:
            return False
        req.system_prompt = f"{system_prompt}{block}"
        self._counters["injections"] += 1
        return True

    async def session_detail(self, session_key: str) -> dict[str, Any]:
        """Return the stored state of one session.

        Args:
            session_key: unified_msg_origin.

        Returns:
            Mapping with the digest and the recent turns.
        """
        payload = await self.store.load(session_key, limit=self.config.max_turns)
        payload["enabled"] = self._started
        return payload

    async def delete(self, session_key: str) -> int:
        """Forget the timeline of one session.

        Args:
            session_key: unified_msg_origin.

        Returns:
            Number of turn rows deleted.
        """
        async with self._lock:
            self._timelines.pop(session_key, None)
            self._dirty_sessions.discard(session_key)
            self._dirty_turns = {
                item for item in self._dirty_turns if item[0] != session_key
            }
            return await self.store.delete(session_key)

    async def status(self) -> dict[str, Any]:
        """Return a serializable status snapshot.

        Returns:
            Mapping with storage counters, cache size and runtime counters.
        """
        storage = await self.store.stats()
        if self._timelines:
            storage["cached"] = len(self._timelines)
        return {
            "enabled": self._started,
            "configured": self.config.enabled,
            "inject": self.config.inject,
            "keep_turns": self.config.keep_turns,
            "digest_max_chars": self.config.digest_max_chars,
            "storage": storage,
            "dirty": len(self._dirty_sessions),
            "counters": dict(self._counters),
        }

    async def _checkpoint(
        self,
        session_key: str,
        timeline: FocusTimeline,
    ) -> None:
        """Persist one timeline: dirty turns, folded turns and the digest.

        Args:
            session_key: unified_msg_origin.
            timeline: Timeline to persist.
        """
        folded = timeline.compress(self.config.keep_turns)
        for turn in folded:
            # A folded turn is persisted in compressed form; drop the pending
            # dirty marker so the bookkeeping cannot leak.
            self._dirty_turns.discard((session_key, turn.index))
        written = [
            turn
            for turn in timeline.turns
            if (session_key, turn.index) in self._dirty_turns
        ]
        for turn in written:
            self._dirty_turns.discard((session_key, turn.index))
        for turn in folded + written:
            await self.store.save_turn(session_key, turn.to_dict())
        self._counters["folded"] += len(folded)
        await self.store.save_head(
            session_key,
            digest=timeline.digest,
            compressed_turns=timeline.compressed_turns,
            total_messages=timeline.total_messages,
            last_turn_index=max(
                (turn.index for turn in timeline.turns),
                default=0,
            ),
        )
        await self.store.prune(session_key, self.config.max_turns)
        self._dirty_sessions.discard(session_key)
        self._counters["checkpoints"] += 1

    @staticmethod
    def _message_text(message: Any) -> str:
        """Return the textual form of one archived message.

        Args:
            message: ``IngestedMessage`` from the ingest flush.

        Returns:
            Plain text, a media marker, or an empty string for empty messages.
        """
        text = str(getattr(message, "plain_text", "") or "").strip()
        if text:
            return text
        flags = getattr(message, "flags", None) or {}
        for kind, marker in _MEDIA_MARKERS.items():
            if flags.get(kind):
                return marker
        return ""
