"""Persistence models for the sticker library.

Images are deduplicated by content hash inside a session, so the same meme
forwarded ten times costs one row and one file.
"""

from __future__ import annotations

from sqlalchemy import JSON, Index, LargeBinary, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class LifeEmoji(SQLModel, table=True):
    """One collected sticker."""

    __tablename__ = "life_emoji"

    id: int | None = Field(default=None, primary_key=True)
    session_key: str = Field(index=True, max_length=255)
    sha256: str = Field(index=True, max_length=64)
    file_path: str = Field(max_length=1024)
    mime: str = Field(default="image/jpeg", max_length=64)
    size_bytes: int = 0
    width: int = 0
    height: int = 0
    tags: list = Field(default_factory=list, sa_type=JSON)
    """Semantic labels used for retrieval and explanation."""

    caption: str = Field(default="", sa_type=Text)
    embedding: bytes | None = Field(default=None, sa_type=LargeBinary)
    embedding_model: str = Field(default="", max_length=64)
    embedding_dim: int = 0
    usage_count: int = 0
    last_used_ts: int = 0
    status: str = Field(default="active", max_length=16)
    """``active`` / ``rejected``."""

    source: str = Field(default="archive", max_length=16)
    created_at: int = 0
    updated_at: int = 0

    __table_args__ = (
        UniqueConstraint(
            "session_key",
            "sha256",
            name="uix_life_emoji_session_sha",
        ),
        Index("ix_life_emoji_session_usage", "session_key", "usage_count"),
    )


class LifeEmojiCursor(SQLModel, table=True):
    """How far sticker collection has consumed the archive, per session."""

    __tablename__ = "life_emoji_cursor"

    id: int | None = Field(default=None, primary_key=True)
    session_key: str = Field(index=True, max_length=255, unique=True)
    last_ts: int = 0
    updated_at: int = 0
