"""Persistence models for the Life layer.

Tables are created explicitly by :class:`bot.life.store.LifeStore` instead of
``SQLModel.metadata.create_all`` so the Life layer does not depend on being
imported before the core database initialization.
"""

from __future__ import annotations

from sqlalchemy import JSON, Index, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class LifeMessage(SQLModel, table=True):
    """Archived inbound message with full component payload."""

    __tablename__ = "life_messages"

    id: int | None = Field(default=None, primary_key=True)
    platform_id: str = Field(index=True, max_length=64)
    session_key: str = Field(index=True, max_length=255)
    message_id: str = Field(max_length=255)
    ts: int = Field(index=True)
    sender_id: str | None = Field(default=None, max_length=255)
    sender_name: str | None = Field(default=None, max_length=255)
    group_id: str | None = Field(default=None, max_length=64)
    components: list = Field(default_factory=list, sa_type=JSON)
    plain_text: str = Field(default="", sa_type=Text)
    reply_to: str | None = Field(default=None, max_length=255)
    flags: dict = Field(default_factory=dict, sa_type=JSON)
    ingested_at: int = 0

    __table_args__ = (
        UniqueConstraint(
            "platform_id",
            "message_id",
            name="uix_life_messages_platform_message",
        ),
        Index("ix_life_messages_session_ts", "session_key", "ts"),
    )


class LifeOutbound(SQLModel, table=True):
    """Record of a proactive message sent by the Life layer."""

    __tablename__ = "life_outbound"

    id: int | None = Field(default=None, primary_key=True)
    session_key: str = Field(index=True, max_length=255)
    message_id: str = Field(max_length=64)
    status: str = Field(default="pending", max_length=16)
    platform_message_id: str | None = Field(default=None, max_length=255)
    error: str | None = Field(default=None, max_length=500)
    attempts: int = 0
    created_at: int = 0
    updated_at: int = 0

    __table_args__ = (
        Index("ix_life_outbound_session_created", "session_key", "created_at"),
    )
