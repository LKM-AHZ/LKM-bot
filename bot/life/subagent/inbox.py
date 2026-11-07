"""Delivery of finished sub-agent conclusions into a session.

A background task cannot start a new turn on its own, so the conclusion is
either appended to a run that is still active (a real "wake") or kept in a
per-session inbox and injected into the next prompt as ``<life_subagent>``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from bot.life.subagent.models import SubAgentTask


@dataclass(frozen=True, slots=True)
class SubAgentNotice:
    """One conclusion waiting to be shown to the model."""

    session_id: str
    task_id: str
    description: str
    conclusion: str
    finished_at: float = field(default_factory=time.time)


class SubAgentInbox:
    """Bounded per-session list of undelivered conclusions."""

    def __init__(
        self,
        *,
        max_per_session: int = 10,
        max_age_minutes: int = 120,
    ) -> None:
        """Initialize an empty inbox.

        Args:
            max_per_session: Conclusions kept per session, oldest dropped first.
            max_age_minutes: Undelivered conclusions older than this are dropped.
        """
        self.max_per_session = max(1, int(max_per_session))
        self.max_age_minutes = max(0, int(max_age_minutes))
        self._pending: dict[str, list[SubAgentNotice]] = {}

    def add(self, notice: SubAgentNotice) -> None:
        """Append one conclusion for a session.

        Args:
            notice: Conclusion to deliver at the next turn.
        """
        pending = self._pending.setdefault(notice.session_id, [])
        pending.append(notice)
        del pending[: max(0, len(pending) - self.max_per_session)]

    def take(self, session_id: str, limit: int = 0) -> list[SubAgentNotice]:
        """Remove and return up to ``limit`` pending conclusions.

        Args:
            session_id: Unified message origin of the session.
            limit: Maximum conclusions returned; 0 returns all pending.

        Returns:
            Pending conclusions, oldest first.
        """
        pending = self._pending.get(session_id, [])
        if self.max_age_minutes:
            cutoff = time.time() - self.max_age_minutes * 60
            pending = [notice for notice in pending if notice.finished_at >= cutoff]
        if limit > 0:
            taken, rest = pending[:limit], pending[limit:]
        else:
            taken, rest = pending, []
        if rest:
            self._pending[session_id] = rest
        else:
            self._pending.pop(session_id, None)
        return taken

    def stats(self) -> dict:
        """Return counters for the status endpoint.

        Returns:
            Session count and pending conclusion count.
        """
        return {
            "sessions": len(self._pending),
            "pending": sum(len(items) for items in self._pending.values()),
        }


def notice_from_task(task: SubAgentTask, max_chars: int) -> SubAgentNotice:
    """Build a notice from a finished task.

    Args:
        task: Finished task.
        max_chars: Cap of the stored conclusion.

    Returns:
        Notice ready for the inbox.
    """
    conclusion = task.conclusion.strip()
    if max_chars > 0 and len(conclusion) > max_chars:
        conclusion = conclusion[:max_chars].rstrip() + "…"
    return SubAgentNotice(
        session_id=task.session_id,
        task_id=task.task_id,
        description=task.description.strip(),
        conclusion=conclusion,
    )
