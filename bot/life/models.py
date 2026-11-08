"""Persistence models for the Life layer.

Inbound messages are stored in the core ``platform_message_history`` table; only
Life-specific tables live here. They are created explicitly by
:class:`bot.life.store.LifeStore` instead of ``SQLModel.metadata.create_all`` so
the Life layer does not depend on being imported before the core database
initialization.
"""

from __future__ import annotations

from sqlalchemy import Index
from sqlmodel import Field, SQLModel


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
