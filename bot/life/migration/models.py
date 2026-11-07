"""Persistence model for migration runs (S7b).

One row per real import. It records the row-id watermark of every touched table
before and after the run, which is all a rollback needs: the importer only
appends, so deleting ``before < id <= after`` removes exactly the migrated rows
and leaves live traffic alone.
"""

from __future__ import annotations

from sqlalchemy import JSON
from sqlmodel import Field, SQLModel


class LifeMigrationRun(SQLModel, table=True):
    """One import batch and its rollback watermark."""

    __tablename__ = "life_migration_run"

    id: int | None = Field(default=None, primary_key=True)
    batch_id: str = Field(index=True, max_length=32, unique=True)
    source: str = Field(default="", max_length=1024)
    """MaiBot database the batch was read from."""

    status: str = Field(default="running", max_length=16)
    """``running`` / ``completed`` / ``failed`` / ``rolled_back``."""

    dry_run: bool = False
    datasets: list = Field(default_factory=list, sa_type=JSON)
    counts: dict = Field(default_factory=dict, sa_type=JSON)
    marks: dict = Field(default_factory=dict, sa_type=JSON)
    """Table name to ``{"before": id, "after": id}``."""

    options: dict = Field(default_factory=dict, sa_type=JSON)
    error: str = Field(default="", max_length=500)
    created_at: int = 0
    finished_at: int = 0
    rolled_back_at: int = 0
