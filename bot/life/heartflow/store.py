"""Storage for the talk values of the proactive layer."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, desc, func, select
from sqlmodel import col

from bot.core.db import BaseDatabase
from bot.life.heartflow.idle import TalkState
from bot.life.heartflow.models import LifeTalk


class TalkStore:
    """Async SQLite store for ``life_talk``."""

    def __init__(self, db_helper: BaseDatabase) -> None:
        """Initialize the store.

        Args:
            db_helper: Core async database helper.
        """
        self.db = db_helper

    async def ensure_schema(self) -> None:
        """Create the talk table when missing."""
        async with self.db.get_db() as session:
            connection = await session.connection()
            await connection.run_sync(
                lambda sync_connection: LifeTalk.__table__.create(  # ty: ignore[unresolved-attribute]  # SQLModel adds __table__ at runtime
                    sync_connection,
                    checkfirst=True,
                )
            )
            await session.commit()

    async def get(self, session_key: str) -> TalkState:
        """Return the talk state of one session.

        Args:
            session_key: unified_msg_origin.

        Returns:
            Stored state, or a default one when the session is new.
        """
        async with self.db.get_db() as session:
            row = (
                (
                    await session.execute(
                        select(LifeTalk).where(col(LifeTalk.session_key) == session_key)
                    )
                )
                .scalars()
                .first()
            )
        return self._state(session_key, row)

    async def get_many(self, session_keys: list[str]) -> dict[str, TalkState]:
        """Return the talk state of several sessions.

        Args:
            session_keys: Sessions to load.

        Returns:
            Mapping of session key to state; missing sessions get defaults.
        """
        if not session_keys:
            return {}
        async with self.db.get_db() as session:
            rows = (
                (
                    await session.execute(
                        select(LifeTalk).where(
                            col(LifeTalk.session_key).in_(session_keys)
                        )
                    )
                )
                .scalars()
                .all()
            )
        found = {row.session_key: row for row in rows}
        return {key: self._state(key, found.get(key)) for key in session_keys}

    async def save(self, state: TalkState) -> None:
        """Insert or update one talk state.

        Args:
            state: State to persist.
        """
        now = int(time.time())
        async with self.db.get_db() as session:
            row = (
                (
                    await session.execute(
                        select(LifeTalk).where(
                            col(LifeTalk.session_key) == state.session_key
                        )
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                row = LifeTalk(session_key=state.session_key, created_at=now)
                session.add(row)
            row.talk_value = state.talk_value
            row.last_proactive_ts = state.last_proactive_ts
            row.pending_ts = state.pending_ts
            row.attempts = state.attempts
            row.answered = state.answered
            row.sent_day = state.sent_day
            row.sent_today = state.sent_today
            row.updated_at = now
            await session.commit()

    async def list_talk(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return sessions ordered by talk value.

        Args:
            limit: Maximum number of rows.

        Returns:
            Serialized rows.
        """
        async with self.db.get_db() as session:
            rows = (
                (
                    await session.execute(
                        select(LifeTalk)
                        .order_by(desc(col(LifeTalk.talk_value)))
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [
            {
                "session_key": row.session_key,
                "talk_value": row.talk_value,
                "attempts": row.attempts,
                "answered": row.answered,
                "last_proactive_ts": row.last_proactive_ts,
                "pending_ts": row.pending_ts,
                "sent_today": row.sent_today,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]

    async def delete(self, session_key: str) -> int:
        """Remove one session.

        Args:
            session_key: unified_msg_origin.

        Returns:
            Number of deleted rows.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                delete(LifeTalk).where(col(LifeTalk.session_key) == session_key)
            )
            await session.commit()
        return int(result.rowcount or 0)  # ty: ignore[unresolved-attribute]

    async def stats(self) -> dict[str, Any]:
        """Return storage counters.

        Returns:
            Mapping with row count and the average talk value.
        """
        async with self.db.get_db() as session:
            count = (
                await session.execute(select(func.count()).select_from(LifeTalk))
            ).scalar_one()
            average = (
                await session.execute(select(func.avg(col(LifeTalk.talk_value))))
            ).scalar_one()
        return {
            "sessions": int(count or 0),
            "avg_talk_value": round(float(average or 0.0), 3),
        }

    @staticmethod
    def _state(session_key: str, row: LifeTalk | None) -> TalkState:
        """Convert one row into a talk state.

        Args:
            session_key: Session the state belongs to.
            row: Stored row, or None for a new session.

        Returns:
            Talk state.
        """
        if row is None:
            return TalkState(session_key=session_key)
        return TalkState(
            session_key=session_key,
            talk_value=row.talk_value,
            last_proactive_ts=row.last_proactive_ts,
            pending_ts=row.pending_ts,
            attempts=row.attempts,
            answered=row.answered,
            sent_day=row.sent_day,
            sent_today=row.sent_today,
        )
