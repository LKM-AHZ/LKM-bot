"""Storage for Life persona profiles."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, desc, func, select
from sqlmodel import col

from bot.core.db import BaseDatabase
from bot.life.config import PersonaConfig
from bot.life.persona.models import LifePersonaProfile

_TEXT_FIELDS = ("base_prompt", "group_prompt", "private_prompt")


def _clamp(value: Any, low: float, high: float, fallback: float) -> float:
    """Clamp a numeric payload field.

    Args:
        value: Raw payload value.
        low: Lower bound.
        high: Upper bound.
        fallback: Value used when the payload is not a number.

    Returns:
        The clamped float.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return min(max(number, low), high)


class PersonaStore:
    """Async SQLite store for persona profiles."""

    def __init__(
        self, db_helper: BaseDatabase, config: PersonaConfig | None = None
    ) -> None:
        """Initialize the store.

        Args:
            db_helper: Core async database helper.
            config: Persona settings holding fallback style values.
        """
        self.db = db_helper
        self.config = config or PersonaConfig()

    async def ensure_schema(self) -> None:
        """Create the persona table when missing."""
        async with self.db.get_db() as session:
            connection = await session.connection()
            await connection.run_sync(
                lambda sync_connection: LifePersonaProfile.__table__.create(  # ty: ignore[unresolved-attribute]  # SQLModel adds __table__ at runtime
                    sync_connection,
                    checkfirst=True,
                )
            )
            await session.commit()

    async def get(self, persona_id: str) -> dict[str, Any] | None:
        """Return one profile.

        Args:
            persona_id: Upstream persona id; empty for the fallback profile.

        Returns:
            Row dictionary, or None when missing.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifePersonaProfile).where(
                    col(LifePersonaProfile.persona_id) == persona_id
                )
            )
            row = result.scalars().first()
            return row.model_dump() if row else None

    async def upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create or replace one profile.

        Args:
            payload: Fields to store; ``persona_id`` is required.

        Returns:
            The stored row.

        Raises:
            ValueError: When ``persona_id`` is missing from the payload.
        """
        if "persona_id" not in payload:
            raise ValueError("persona_id is required")
        persona_id = str(payload.get("persona_id") or "").strip()[:128]
        now = int(time.time())
        values: dict[str, Any] = {
            "display_name": str(payload.get("display_name") or "").strip()[:64],
            "quote_rate": _clamp(
                payload.get("quote_rate"),
                0.0,
                1.0,
                self.config.default_quote_rate,
            ),
            "multi_segment_prob": _clamp(
                payload.get("multi_segment_prob"),
                0.0,
                1.0,
                self.config.default_multi_segment_prob,
            ),
            "typo_rate": _clamp(
                payload.get("typo_rate"),
                0.0,
                1.0,
                self.config.default_typo_rate,
            ),
            "reply_max_chars": int(
                _clamp(
                    payload.get("reply_max_chars"),
                    1,
                    2000,
                    self.config.reply_max_chars,
                )
            ),
            "allow_markdown": bool(payload.get("allow_markdown", False)),
            "extra": dict(payload.get("extra") or {}),
            "updated_at": now,
        }
        for field in _TEXT_FIELDS:
            values[field] = str(payload.get(field) or "").strip()[:4000]
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifePersonaProfile).where(
                    col(LifePersonaProfile.persona_id) == persona_id
                )
            )
            row = result.scalars().first()
            if row is None:
                row = LifePersonaProfile(persona_id=persona_id, created_at=now)
            for key, value in values.items():
                setattr(row, key, value)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.model_dump()

    async def delete(self, persona_id: str) -> bool:
        """Delete one profile.

        Args:
            persona_id: Upstream persona id; empty for the fallback profile.

        Returns:
            True when a row was removed.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                delete(LifePersonaProfile).where(
                    col(LifePersonaProfile.persona_id) == persona_id
                )
            )
            await session.commit()
            return bool(result.rowcount)  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount

    async def list_profiles(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return profiles, newest first.

        Args:
            limit: Maximum rows.

        Returns:
            Row dictionaries.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifePersonaProfile)
                .order_by(
                    desc(col(LifePersonaProfile.updated_at)),
                    desc(col(LifePersonaProfile.id)),
                )
                .limit(max(int(limit), 1))
            )
            return [row.model_dump() for row in result.scalars().all()]

    async def stats(self) -> dict[str, Any]:
        """Return profile counters.

        Returns:
            Mapping with the total row count.
        """
        async with self.db.get_db() as session:
            total = (
                await session.execute(
                    select(func.count()).select_from(LifePersonaProfile)
                )
            ).scalar_one()
        return {"total": int(total or 0)}
