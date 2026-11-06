"""Persistence models for long-term memory.

The memory tables live next to the Life archive tables and are created lazily by
:class:`bot.life.memory.store.MemoryStore`, so importing this module has no side
effects and the memory layer stays optional.
"""

from __future__ import annotations

from sqlalchemy import JSON, Index, LargeBinary, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class LifeMemory(SQLModel, table=True):
    """One long-term memory row: summary, fact, episode or manual note."""

    __tablename__ = "life_memory"

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(index=True, max_length=16)
    """``summary`` / ``fact`` / ``episode`` / ``note``."""

    session_key: str = Field(index=True, max_length=255)
    scope: str = Field(default="session", max_length=16)
    """``session`` for per-chat memories, ``global`` for cross-session ones."""

    content: str = Field(sa_type=Text)
    content_hash: str = Field(index=True, max_length=64)
    """Stable hash used to keep digest rounds idempotent."""

    keywords: str = Field(default="", sa_type=Text)
    """Space-separated search tokens built at write time."""

    person_id: str | None = Field(default=None, index=True, max_length=255)
    importance: float = 0.5
    ts: int = Field(index=True)
    """Anchor timestamp; the window end for summaries and episodes."""

    start_ts: int | None = None
    end_ts: int | None = None
    source_ids: list = Field(default_factory=list, sa_type=JSON)
    embedding: bytes | None = Field(default=None, sa_type=LargeBinary)
    embedding_model: str = Field(default="", max_length=64)
    embedding_dim: int = 0
    meta: dict = Field(default_factory=dict, sa_type=JSON)
    created_at: int = 0
    updated_at: int = 0

    __table_args__ = (
        UniqueConstraint(
            "kind",
            "session_key",
            "content_hash",
            name="uix_life_memory_kind_session_hash",
        ),
        Index("ix_life_memory_session_ts", "session_key", "ts"),
        Index("ix_life_memory_kind_ts", "kind", "ts"),
    )


class LifeMemoryCursor(SQLModel, table=True):
    """Digest progress per session, so restarts do not re-summarize messages."""

    __tablename__ = "life_memory_cursor"

    id: int | None = Field(default=None, primary_key=True)
    session_key: str = Field(index=True, max_length=255, unique=True)
    last_ts: int = 0
    updated_at: int = 0
