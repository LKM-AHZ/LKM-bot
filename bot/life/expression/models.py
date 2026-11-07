"""Persistence models for learned expressions.

An expression is a ``(situation, style)`` pair: *when* something is said and
*how* it is phrased. The table is the source of truth; the FAISS index is a
derived, rebuildable structure on top of it.
"""

from __future__ import annotations

from sqlalchemy import JSON, Index, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class LifeExpression(SQLModel, table=True):
    """One learned way of phrasing something."""

    __tablename__ = "life_expression"

    id: int | None = Field(default=None, primary_key=True)
    session_key: str = Field(index=True, max_length=255)
    scene: str = Field(default="any", max_length=16)
    """``any`` / ``group`` / ``private``."""

    situation: str = Field(default="", sa_type=Text)
    """When the expression applies."""

    style: str = Field(max_length=255)
    """The phrasing itself."""

    tags: list = Field(default_factory=list, sa_type=JSON)
    checksum: str = Field(index=True, max_length=64)
    weight: float = 1.0
    usage_count: int = 0
    last_used_ts: int = 0
    source: str = Field(default="heuristic", max_length=16)
    """``heuristic`` / ``llm`` / ``manual``."""

    status: str = Field(default="active", max_length=16)
    """``active`` / ``rejected``."""

    vector_state: str = Field(default="pending", max_length=16)
    """``pending`` / ``indexed`` / ``skipped``."""

    created_at: int = 0
    updated_at: int = 0

    __table_args__ = (
        UniqueConstraint(
            "session_key",
            "checksum",
            name="uix_life_expression_session_checksum",
        ),
        Index("ix_life_expression_session_weight", "session_key", "weight"),
        Index("ix_life_expression_vector_state", "vector_state"),
    )


class LifeExpressionCursor(SQLModel, table=True):
    """How far expression learning has consumed the archive, per session."""

    __tablename__ = "life_expression_cursor"

    id: int | None = Field(default=None, primary_key=True)
    session_key: str = Field(index=True, max_length=255, unique=True)
    last_ts: int = 0
    updated_at: int = 0
