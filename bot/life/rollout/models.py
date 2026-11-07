"""Persistence model for the gray-rollout decision log (S7a).

One row per released turn. The row has no message and no reply text on purpose:
the plan requires production logs to stay free of raw content, and a shadow
turn never generates a reply, so there is no draft to store either.
"""

from __future__ import annotations

from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class LifeTurnDecision(SQLModel, table=True):
    """Decision and timing of one released turn."""

    __tablename__ = "life_turn_decision"

    id: int | None = Field(default=None, primary_key=True)
    ts: float = Field(default=0.0, index=True)
    session_key: str = Field(default="", index=True, max_length=255)
    messages: int = 1
    """Messages merged into the turn."""

    decision: str = Field(default="wait", max_length=8)
    """``reply`` / ``wait``."""

    action: str = Field(default="hold", max_length=8)
    """Outcome handed to the stage: ``continue`` / ``hold``."""

    shadow: bool = False
    """True when the runtime only recorded the decision instead of replying."""

    reason: str = Field(default="", max_length=64)
    likelihood: float = 0.0
    threshold: float = 0.0
    latency_ms: int = 0
    """Time from the first merged message to the decision, in milliseconds."""

    text_chars: int = 0
    """Length of the merged text; never the text itself."""

    created_at: int = 0

    __table_args__ = (Index("ix_life_turn_decision_ts_decision", "ts", "decision"),)
