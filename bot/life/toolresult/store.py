"""Bounded per-session stash of raw tool results.

Only the condensed text reaches the model history; the original stays here,
keyed by a short per-session id, so ``read_tool_result`` can pull back the part
the summary dropped. Everything is memory-only: a restart clears the stash, and
the count, session and character bounds keep it small.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field

DEFAULT_MAX_RESULTS_PER_SESSION = 20
DEFAULT_MAX_SESSIONS = 200
DEFAULT_MAX_CHARS_PER_SESSION = 200_000


@dataclass(frozen=True, slots=True)
class StoredToolResult:
    """One raw tool result kept out of the model history."""

    result_id: str
    tool_name: str
    content: str
    created_at: float = field(default_factory=time.time)

    @property
    def chars(self) -> int:
        """Return the character count of the raw result."""
        return len(self.content)


class ToolResultStore:
    """Per-session LRU stash of raw tool results."""

    def __init__(
        self,
        *,
        max_results_per_session: int = DEFAULT_MAX_RESULTS_PER_SESSION,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_chars_per_session: int = DEFAULT_MAX_CHARS_PER_SESSION,
    ) -> None:
        """Initialize an empty stash.

        Args:
            max_results_per_session: Results kept per session, oldest dropped first.
            max_sessions: Sessions kept at all, least recently used dropped first.
            max_chars_per_session: Character budget of one session's stash.
        """
        self.max_results_per_session = max(1, int(max_results_per_session))
        self.max_sessions = max(1, int(max_sessions))
        self.max_chars_per_session = max(1, int(max_chars_per_session))
        self._sessions: OrderedDict[str, OrderedDict[str, StoredToolResult]] = (
            OrderedDict()
        )
        self._counters: dict[str, int] = {}

    def save(self, session_id: str, tool_name: str, content: str) -> str:
        """Stash one raw result and return its short per-session id.

        Args:
            session_id: Unified message origin of the session.
            tool_name: Tool that produced the result.
            content: Raw result text.

        Returns:
            Per-session id such as ``r3``.
        """
        counter = self._counters.get(session_id, 0) + 1
        self._counters[session_id] = counter
        result_id = f"r{counter}"

        session = self._sessions.get(session_id)
        if session is None:
            session = OrderedDict()
            self._sessions[session_id] = session
        session[result_id] = StoredToolResult(
            result_id=result_id,
            tool_name=tool_name,
            content=content,
        )
        self._sessions.move_to_end(session_id)

        while len(session) > self.max_results_per_session:
            session.popitem(last=False)
        while (
            len(session) > 1
            and sum(stored.chars for stored in session.values())
            > self.max_chars_per_session
        ):
            session.popitem(last=False)
        while len(self._sessions) > self.max_sessions:
            oldest_session, _ = self._sessions.popitem(last=False)
            self._counters.pop(oldest_session, None)
        return result_id

    def get(self, session_id: str, result_id: str) -> StoredToolResult | None:
        """Return one stored result, or None when it was evicted or unknown.

        Args:
            session_id: Unified message origin of the session.
            result_id: Id returned by :meth:`save`.

        Returns:
            The stored result, or None.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        stored = session.get(result_id.strip())
        if stored is not None:
            self._sessions.move_to_end(session_id)
        return stored

    def ids(self, session_id: str) -> list[str]:
        """Return the ids currently kept for one session, oldest first.

        Args:
            session_id: Unified message origin of the session.

        Returns:
            Stored ids such as ``["r2", "r3"]``.
        """
        return list(self._sessions.get(session_id, ()))

    def stats(self) -> dict:
        """Return a small snapshot for the status endpoint.

        Returns:
            Session, result and character counts.
        """
        results = sum(len(session) for session in self._sessions.values())
        chars = sum(
            stored.chars
            for session in self._sessions.values()
            for stored in session.values()
        )
        return {
            "sessions": len(self._sessions),
            "results": results,
            "chars": chars,
        }
