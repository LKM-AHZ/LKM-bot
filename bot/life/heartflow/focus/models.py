"""Persistence models for the focus timeline.

Two tables: one row per session holds the folded digest and the counters, one row
per turn holds what was said and how it ended. Turns are the checkpoint: a
restart reloads them in ``turn_index`` order and the timeline is rebuilt without
replaying the archive.
"""

from __future__ import annotations

from sqlalchemy import JSON, Index, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class LifeFocus(SQLModel, table=True):
    """Folded digest and counters of one session."""

    __tablename__ = "life_focus"

    id: int | None = Field(default=None, primary_key=True)
    session_key: str = Field(index=True, max_length=255, unique=True)
    digest: str = Field(default="", sa_type=Text)
    compressed_turns: int = 0
    total_messages: int = 0
    last_turn_index: int = 0
    updated_at: int = 0


class LifeFocusTurn(SQLModel, table=True):
    """One turn of a session timeline."""

    __tablename__ = "life_focus_turn"

    id: int | None = Field(default=None, primary_key=True)
    session_key: str = Field(index=True, max_length=255)
    turn_index: int = Field(default=0, index=True)
    started_ts: float = 0.0
    ended_ts: float = 0.0
    status: str = Field(default="closed", max_length=16)
    """``open`` / ``closed`` / ``compressed``."""

    summary: str = Field(default="", sa_type=Text)
    messages: list = Field(default_factory=list, sa_type=JSON)
    created_at: int = 0
    updated_at: int = 0

    __table_args__ = (
        UniqueConstraint(
            "session_key",
            "turn_index",
            name="uix_life_focus_turn_session_index",
        ),
        Index("ix_life_focus_turn_session_status", "session_key", "status"),
    )
