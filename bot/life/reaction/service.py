"""Keyword reactions: fixed replies to known triggers (S6c).

A rule matches a message, the bot sends a canned reply and the event stops
there, so the model never sees it. Cooldowns are per rule per session and live
in memory; the point is to keep a familiar joke or a routine question from
re-entering the planner every time.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from bot.life.config import ReactionConfig, ReactionRule

SendText = Callable[[str, str], Awaitable[Any]]


class ReactionService:
    """Rule matching, cooldowns and delivery of fixed replies."""

    def __init__(
        self,
        config: ReactionConfig,
        send: SendText,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the service.

        Args:
            config: Rules, reply cap and switch.
            send: Coroutine sending one plain-text message to a session.
            clock: Wall-clock source; tests inject a fake one.
        """
        self.config = config
        self._send = send
        self._clock = clock or time.time
        self._last: dict[tuple[str, int], float] = {}
        self._counters = {"matched": 0, "sent": 0, "cooled": 0, "failed": 0}

    def match(self, text: str) -> tuple[int, ReactionRule] | None:
        """Return the first rule whose keyword matches the text.

        Args:
            text: Inbound message text.

        Returns:
            ``(rule_index, rule)`` or None.
        """
        normalized = (text or "").strip().lower()
        if not normalized:
            return None
        for index, rule in enumerate(self.config.rules):
            if not rule.reply.strip():
                continue
            for keyword in rule.keywords:
                needle = str(keyword).strip().lower()
                if not needle:
                    continue
                hit = normalized == needle if rule.exact else needle in normalized
                if hit:
                    return index, rule
        return None

    async def try_react(self, session_id: str, text: str) -> bool:
        """Send a fixed reply when a rule matches and its cooldown expired.

        Args:
            session_id: Unified message origin of the session.
            text: Inbound message text.

        Returns:
            True when a reply was sent (and the caller should stop the event).
        """
        matched = self.match(text)
        if matched is None:
            return False
        index, rule = matched
        self._counters["matched"] += 1
        now = self._clock()
        key = (session_id, index)
        last = self._last.get(key)
        if last is not None and now - last < max(0, rule.cooldown_seconds):
            self._counters["cooled"] += 1
            return False
        reply = rule.reply.strip()[: max(1, self.config.max_reply_chars)]
        result = await self._send(session_id, reply)
        if not getattr(result, "ok", False):
            self._counters["failed"] += 1
            return False
        self._last[key] = now
        self._counters["sent"] += 1
        if len(self._last) > 1000:
            horizon = now - 3600
            self._last = {
                key: timestamp
                for key, timestamp in self._last.items()
                if timestamp >= horizon
            }
        return True

    def status(self) -> dict:
        """Return counters and rule count for the status endpoint.

        Returns:
            Reaction counters and the configured rule count.
        """
        return {
            "enabled": True,
            "rules": len(self.config.rules),
            "counters": dict(self._counters),
        }
