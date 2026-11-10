"""Attention drift and back-reference hints.

The bot should not answer the same way whether it just spoke five seconds ago or
half an hour ago. Attention is a per-session number that starts at 1 right after
the bot answers and decays with a half life; while it is in the "drifted" band
the bot is told what the conversation was about, so it can naturally pick the
thread back up instead of pretending the silence never happened.

The state is deliberately in memory: a back reference that only survives a
restart is stale, so there is nothing worth persisting here.
"""

from __future__ import annotations

import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from bot.life.common import (
    append_prompt_block,
    event_umo,
    has_prompt_block,
)
from bot.life.config import AttentionConfig

_BLOCK_TAG = "life_attention"
_SPLIT = re.compile(r"[，,。！？!?…~～、；;：:\s]+")
_MAX_TRACKED_SESSIONS = 500


@dataclass(slots=True)
class AttentionState:
    """What the bot is currently "looking at" in one session."""

    session_key: str
    topic: str = ""
    keywords: list[str] = field(default_factory=list)
    last_ts: float = 0.0
    focus: float = 0.0

    def age_minutes(self, now: float) -> float:
        """Return minutes since the bot last engaged.

        Args:
            now: Current wall-clock timestamp.

        Returns:
            Non-negative minutes; 0 when nothing was recorded yet.
        """
        if not self.last_ts:
            return 0.0
        return max(now - self.last_ts, 0.0) / 60.0


def extract_keywords(text: str, limit: int = 6) -> list[str]:
    """Return a short list of salient segments of one message.

    Args:
        text: Message text.
        limit: Maximum number of segments.

    Returns:
        Distinct segments of 2-8 characters, in order of appearance.
    """
    seen: list[str] = []
    for segment in _SPLIT.split(text):
        cleaned = segment.strip()
        if not 2 <= len(cleaned) <= 8:
            continue
        if cleaned in seen:
            continue
        seen.append(cleaned)
        if len(seen) >= limit:
            break
    return seen


def focus_at(state: AttentionState, now: float, half_life_minutes: float) -> float:
    """Return the decayed focus of one session.

    Args:
        state: Attention state.
        now: Current wall-clock timestamp.
        half_life_minutes: Minutes after which the focus halves.

    Returns:
        Focus in ``[0, 1]``.
    """
    if not state.last_ts:
        return 0.0
    if half_life_minutes <= 0:
        return 0.0
    elapsed = max(now - state.last_ts, 0.0) / 60.0
    return state.focus * math.pow(0.5, elapsed / half_life_minutes)


class AttentionRegistry:
    """Bounded per-session attention state plus the prompt hook."""

    def __init__(
        self,
        config: AttentionConfig,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the registry.

        Args:
            config: Attention settings.
            clock: Wall-clock source; tests inject a fake one.
        """
        self.config = config
        self._clock = clock or time.time
        self._states: OrderedDict[str, AttentionState] = OrderedDict()
        self._counters = {"observed": 0, "hooks": 0, "forgotten": 0}

    def observe(self, session_key: str, text: str, now: float) -> AttentionState:
        """Record that the bot just engaged with this session.

        Args:
            session_key: unified_msg_origin.
            text: Text the bot answered.
            now: Current wall-clock timestamp.

        Returns:
            Updated attention state.
        """
        state = self._states.get(session_key)
        if state is None:
            if len(self._states) >= _MAX_TRACKED_SESSIONS:
                self._states.pop(next(iter(self._states)), None)
            state = AttentionState(session_key=session_key)
            self._states[session_key] = state
        cleaned = " ".join(text.split())
        if cleaned:
            state.topic = cleaned[: self.config.hook_max_chars]
            # A keyword equal to the whole sentence just repeats the topic.
            state.keywords = [
                word
                for word in extract_keywords(cleaned, self.config.keywords)
                if word != cleaned
            ]
        state.last_ts = now
        state.focus = 1.0
        self._counters["observed"] += 1
        return state

    def hint(self, session_key: str, now: float) -> str | None:
        """Return a back-reference hint when the bot has drifted but not forgot.

        Args:
            session_key: unified_msg_origin.
            now: Current wall-clock timestamp.

        Returns:
            Hint text, or None when there is nothing to hook back to.
        """
        state = self._states.get(session_key)
        if state is None or not state.topic:
            return None
        focus = focus_at(state, now, self.config.half_life_minutes)
        if focus < self.config.min_focus:
            self._forget(session_key)
            return None
        if not (self.config.hook_focus_min <= focus <= self.config.hook_focus_max):
            return None
        age = state.age_minutes(now)
        if age < self.config.hook_min_gap_minutes:
            return None
        keywords = "、".join(state.keywords[:3])
        suffix = f"（关键词：{keywords}）" if keywords else ""
        return (
            f"你们大约 {int(age)} 分钟前聊过：{state.topic[: self.config.hook_max_chars]}"
            f"{suffix}。如果这句接得上，可以自然地回钩；接不上就别硬提。"
        )

    async def inject(self, event: Any, req: Any) -> bool:
        """Append a back-reference hint to the system prompt.

        Args:
            event: Bot message event carrying the session identity.
            req: ``ProviderRequest`` about to be sent to the model.

        Returns:
            True when the prompt was modified.
        """
        if not self.config.enabled:
            return False
        umo = event_umo(event)
        if not umo or has_prompt_block(req, _BLOCK_TAG):
            return False
        hint = self.hint(umo, self._clock())
        if not hint:
            return False
        block = f"<{_BLOCK_TAG}>\n{hint}\n</{_BLOCK_TAG}>"
        if not append_prompt_block(req, _BLOCK_TAG, block, separator="\n\n"):
            return False
        self._counters["hooks"] += 1
        return True

    def focus_of(self, session_key: str, now: float | None = None) -> float:
        """Return the current focus of one session.

        Args:
            session_key: unified_msg_origin.
            now: Current wall-clock timestamp.

        Returns:
            Focus in ``[0, 1]``.
        """
        state = self._states.get(session_key)
        if state is None:
            return 0.0
        return focus_at(state, now or self._clock(), self.config.half_life_minutes)

    def snapshot(self, now: float | None = None) -> list[dict[str, Any]]:
        """Return every tracked session for the dashboard.

        Args:
            now: Current wall-clock timestamp.

        Returns:
            Rows ordered by most recent engagement.
        """
        moment = now or self._clock()
        rows = [
            {
                "session_key": state.session_key,
                "focus": round(
                    focus_at(state, moment, self.config.half_life_minutes), 4
                ),
                "age_minutes": round(state.age_minutes(moment), 1),
                "topic": state.topic[:80],
                "keywords": state.keywords[:3],
            }
            for state in self._states.values()
        ]
        rows.sort(key=lambda row: row["age_minutes"])
        return rows

    def status(self, now: float | None = None) -> dict[str, Any]:
        """Return a serializable status block.

        Args:
            now: Current wall-clock timestamp.

        Returns:
            Mapping with the switch, tracked sessions and counters.
        """
        return {
            "enabled": self.config.enabled,
            "tracked": len(self._states),
            "half_life_minutes": self.config.half_life_minutes,
            "counters": dict(self._counters),
            "sessions": self.snapshot(now)[:20],
        }

    def _forget(self, session_key: str) -> None:
        """Drop a session whose attention is below the floor.

        Args:
            session_key: unified_msg_origin.
        """
        if self._states.pop(session_key, None) is not None:
            self._counters["forgotten"] += 1
