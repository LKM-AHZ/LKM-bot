"""Persistence for Life persona profiles.

One row per upstream ``persona_id``. An empty ``persona_id`` is the fallback
profile: it applies to every session that has no dedicated row, which keeps the
"no persona configured" case (most deployments) usable without editing upstream
data.
"""

from __future__ import annotations

from sqlalchemy import JSON, Text
from sqlmodel import Field, SQLModel


class LifePersonaProfile(SQLModel, table=True):
    """Style extension of one upstream persona."""

    __tablename__ = "life_persona_profile"

    id: int | None = Field(default=None, primary_key=True)
    persona_id: str = Field(default="", index=True, max_length=128, unique=True)
    """Upstream persona id; empty for the fallback profile."""

    display_name: str = Field(default="", max_length=64)
    base_prompt: str = Field(default="", sa_type=Text)
    """Extra instructions applied in every scene."""

    group_prompt: str = Field(default="", sa_type=Text)
    """Extra instructions applied in group chats only."""

    private_prompt: str = Field(default="", sa_type=Text)
    """Extra instructions applied in private chats only."""

    quote_rate: float = 0.2
    """Target share of replies that quote the message they answer."""

    multi_segment_prob: float = 0.15
    """Target share of replies split into several messages."""

    typo_rate: float = 0.02
    """Target share of replies that receive a deliberate typo."""

    reply_max_chars: int = 120
    """Soft upper bound of one reply, in characters."""

    allow_markdown: bool = False
    """Whether Markdown survives post-processing."""

    extra: dict = Field(default_factory=dict, sa_type=JSON)
    """Free-form knobs reserved for later post-processing stages."""

    created_at: int = 0
    updated_at: int = 0
