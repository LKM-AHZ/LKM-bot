"""Session registry for the Life layer."""

from __future__ import annotations

from dataclasses import dataclass, field

from bot.life.ingest import IngestedMessage

_MAX_TRACKED_SESSIONS = 2000


@dataclass(slots=True)
class SessionSnapshot:
    """Lightweight runtime state of one session."""

    session_key: str
    platform_id: str = ""
    group_id: str | None = None
    message_count: int = 0
    last_message_ts: int = 0
    last_sender_name: str | None = None
    flags: dict = field(default_factory=dict)


class SessionRegistry:
    """Track sessions seen by the Life layer.

    The registry is intentionally bounded: the full heart-flow runtime arrives
    in a later stage, and this only keeps the counters the ingester needs.
    """

    def __init__(self, max_sessions: int = _MAX_TRACKED_SESSIONS) -> None:
        """Initialize the registry.

        Args:
            max_sessions: Maximum tracked sessions before the oldest is evicted.
        """
        self.max_sessions = max_sessions
        self._sessions: dict[str, SessionSnapshot] = {}

    def touch(self, message: IngestedMessage) -> SessionSnapshot:
        """Record one message and return the updated session snapshot.

        Args:
            message: Normalized message.

        Returns:
            Snapshot of the owning session.
        """
        snapshot = self._sessions.get(message.session_key)
        if snapshot is None:
            if len(self._sessions) >= self.max_sessions:
                oldest_key = min(
                    self._sessions,
                    key=lambda key: self._sessions[key].last_message_ts,
                )
                self._sessions.pop(oldest_key, None)
            snapshot = SessionSnapshot(session_key=message.session_key)
            self._sessions[message.session_key] = snapshot
        snapshot.platform_id = message.platform_id
        snapshot.group_id = message.group_id
        snapshot.message_count += 1
        snapshot.last_message_ts = max(snapshot.last_message_ts, message.ts)
        snapshot.last_sender_name = message.sender_name
        snapshot.flags = message.flags
        return snapshot

    def get(self, session_key: str) -> SessionSnapshot | None:
        """Return one session snapshot.

        Args:
            session_key: unified_msg_origin.

        Returns:
            Snapshot or None when the session was never seen.
        """
        return self._sessions.get(session_key)

    def snapshot(self) -> list[dict]:
        """Return all tracked sessions as serializable dictionaries."""
        return [
            {
                "session_key": item.session_key,
                "platform_id": item.platform_id,
                "group_id": item.group_id,
                "message_count": item.message_count,
                "last_message_ts": item.last_message_ts,
                "last_sender_name": item.last_sender_name,
            }
            for item in self._sessions.values()
        ]
