"""Persistence models for people, facts and profiles.

Rows are created lazily by :class:`bot.life.person.store.PersonStore`, so the
person layer stays optional and never touches the database on import.
"""

from __future__ import annotations

from sqlalchemy import JSON, Index, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


class LifePerson(SQLModel, table=True):
    """One known person inside one session."""

    __tablename__ = "life_person"

    id: int | None = Field(default=None, primary_key=True)
    session_key: str = Field(index=True, max_length=255)
    person_id: str = Field(index=True, max_length=255)
    display_name: str = Field(default="", max_length=255)
    normalized_name: str = Field(default="", index=True, max_length=255)
    aliases: list = Field(default_factory=list, sa_type=JSON)
    group_id: str | None = Field(default=None, max_length=64)
    relation_score: float = 0.0
    interactions: int = 0
    last_seen_ts: int = 0
    created_at: int = 0
    updated_at: int = 0

    __table_args__ = (
        UniqueConstraint(
            "session_key",
            "person_id",
            name="uix_life_person_session_person",
        ),
        Index("ix_life_person_session_relation", "session_key", "relation_score"),
    )


class LifePersonFact(SQLModel, table=True):
    """One durable fact about a person, with confidence and lifecycle state."""

    __tablename__ = "life_person_fact"

    id: int | None = Field(default=None, primary_key=True)
    session_key: str = Field(index=True, max_length=255)
    person_id: str = Field(index=True, max_length=255)
    content: str = Field(sa_type=Text)
    fact_hash: str = Field(max_length=64)
    category: str = Field(default="other", max_length=32)
    confidence: float = 0.6
    status: str = Field(default="active", max_length=16)
    """``active`` / ``conflicted`` / ``rejected`` / ``expired``."""

    conflict_group: str = Field(default="", max_length=64)
    source_memory_id: int | None = None
    verified_at: int = 0
    created_at: int = 0
    updated_at: int = 0

    __table_args__ = (
        UniqueConstraint(
            "session_key",
            "person_id",
            "fact_hash",
            name="uix_life_person_fact_hash",
        ),
        Index(
            "ix_life_person_fact_person_status", "session_key", "person_id", "status"
        ),
    )


class LifePersonProfile(SQLModel, table=True):
    """Cached profile summary of one person."""

    __tablename__ = "life_person_profile"

    id: int | None = Field(default=None, primary_key=True)
    session_key: str = Field(index=True, max_length=255)
    person_id: str = Field(index=True, max_length=255)
    summary: str = Field(default="", sa_type=Text)
    highlight: list = Field(default_factory=list, sa_type=JSON)
    fact_ids: list = Field(default_factory=list, sa_type=JSON)
    generator: str = Field(default="extractive", max_length=32)
    updated_at: int = 0

    __table_args__ = (
        UniqueConstraint(
            "session_key",
            "person_id",
            name="uix_life_person_profile_person",
        ),
    )
