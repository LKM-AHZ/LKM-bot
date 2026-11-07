"""Background sub-agent tasks for the Life layer (S6b).

A sub-agent runs one bounded tool loop without the parent conversation, and its
conclusion is delivered into the session instead of being sent as a message.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Literal

SubAgentTaskStatus = Literal["running", "completed", "failed", "stopped"]


@dataclass(slots=True)
class SubAgentOutcome:
    """What one sub-agent run produced."""

    conclusion: str
    tool_usage: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class SubAgentTask:
    """One registered background task."""

    task_id: str
    session_id: str
    description: str
    granted_tools: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: SubAgentTaskStatus = "running"
    conclusion: str = ""
    error: str = ""
    tool_usage: dict[str, int] = field(default_factory=dict)
    asyncio_task: asyncio.Task | None = None

    @property
    def elapsed_seconds(self) -> float:
        """Return how long the task has run (or ran) in seconds."""
        return (self.finished_at or time.time()) - self.started_at

    def summary(self) -> dict:
        """Return a serializable view for tools and the status endpoint.

        Returns:
            Task fields with the elapsed time and a capped conclusion.
        """
        return {
            "task_id": self.task_id,
            "status": self.status,
            "description": self.description,
            "granted_tools": list(self.granted_tools),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "conclusion": self.conclusion,
            "error": self.error,
            "tool_usage": dict(self.tool_usage),
        }
