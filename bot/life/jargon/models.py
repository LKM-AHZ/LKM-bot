"""Persistence models for group slang.

One term per session. A second, different explanation for the same term does not
overwrite the first: both are kept and the term goes to ``pending`` so a human
can pick. That is the cheapest way to avoid "learned the wrong meaning and
confidently uses it".
"""

from __future__ import annotations

from sqlalchemy import JSON, Index, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class LifeJargon(SQLModel, table=True):
    """One slang term and what it means in a session."""

    __tablename__ = "life_jargon"

    id: int | None = Field(default=None, primary_key=True)
    session_key: str = Field(index=True, max_length=255)
    term: str = Field(max_length=64)
    meaning: str = Field(default="", sa_type=Text)
    confidence: float = 0.6
    status: str = Field(default="active", max_length=16)
    """``active`` / ``pending`` / ``rejected``."""

    source: str = Field(default="heuristic", max_length=16)
    """``heuristic`` / ``llm`` / ``manual``."""

    evidence: list = Field(default_factory=list, sa_type=JSON)
    """Sample messages the term was mined from."""

    checksum: str = Field(index=True, max_length=64)
    usage_count: int = 0
    last_seen_ts: int = 0
    created_at: int = 0
    updated_at: int = 0

    __table_args__ = (
        UniqueConstraint(
            "session_key",
            "checksum",
            name="uix_life_jargon_session_checksum",
        ),
        Index("ix_life_jargon_session_status", "session_key", "status"),
    )


class LifeJargonCursor(SQLModel, table=True):
    """How far jargon learning has consumed the archive, per session."""

    __tablename__ = "life_jargon_cursor"

    id: int | None = Field(default=None, primary_key=True)
    session_key: str = Field(index=True, max_length=255, unique=True)
    last_ts: int = 0
    updated_at: int = 0
