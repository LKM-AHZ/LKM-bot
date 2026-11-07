"""Persistence for the talk value of the proactive layer.

One row per session. It survives restarts on purpose: forgetting that a session
ignored the last three openers would make the bot ping it again immediately.
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel


class LifeTalk(SQLModel, table=True):
    """Talk propensity and proactive bookkeeping of one session."""

    __tablename__ = "life_talk"

    id: int | None = Field(default=None, primary_key=True)
    session_key: str = Field(index=True, max_length=255, unique=True)
    talk_value: float = 0.5
    last_proactive_ts: float = 0.0
    """Timestamp of the last proactive message; 0 when none was sent."""

    pending_ts: float = 0.0
    """Proactive message still waiting for an answer; 0 when settled."""

    attempts: int = 0
    """Consecutive proactive messages nobody answered."""

    answered: int = 0
    """Proactive messages that did get an answer."""

    sent_day: str = Field(default="", max_length=10)
    """Local date (``YYYY-MM-DD``) of ``sent_today``."""

    sent_today: int = 0
    created_at: int = 0
    updated_at: int = 0
