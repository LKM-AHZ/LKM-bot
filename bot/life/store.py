"""Persistence helpers for the Life layer."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import col

from bot import logger
from bot.core.db import BaseDatabase
from bot.life.models import LifeMessage, LifeOutbound


class LifeStore:
    """Thin async wrapper around the Life tables.

    The store creates its tables explicitly so the Life layer can be enabled at
    runtime without relying on import order relative to the core database.
    """

    def __init__(self, db_helper: BaseDatabase, retention_days: int = 0) -> None:
        """Initialize the store.

        Args:
            db_helper: Core async database helper.
            retention_days: Retention window; 0 keeps rows forever.
        """
        self.db = db_helper
        self.retention_days = retention_days

    async def ensure_schema(self) -> None:
        """Create Life tables and indexes when they do not exist."""
        async with self.db.get_db() as session:
            connection = await session.connection()
            await connection.run_sync(
                lambda sync_connection: LifeMessage.__table__.create(  # ty: ignore[unresolved-attribute]  # SQLModel adds __table__ at runtime
                    sync_connection,
                    checkfirst=True,
                )
            )
            await connection.run_sync(
                lambda sync_connection: LifeOutbound.__table__.create(  # ty: ignore[unresolved-attribute]  # SQLModel adds __table__ at runtime
                    sync_connection,
                    checkfirst=True,
                )
            )
            await session.commit()

    async def insert_batch(self, rows: list[dict[str, Any]]) -> int:
        """Insert a batch of messages, ignoring duplicates.

        Args:
            rows: Row payloads produced by ``IngestedMessage.to_row()``.

        Returns:
            Number of rows actually inserted.
        """
        if not rows:
            return 0
        async with self.db.get_db() as session:
            statement = (
                sqlite_insert(LifeMessage)
                .values(rows)
                .on_conflict_do_nothing(
                    index_elements=["platform_id", "message_id"],
                )
            )
            result = await session.execute(statement)
            await session.commit()
            return max(int(result.rowcount or 0), 0)  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount

    async def record_outbound(
        self,
        session_key: str,
        message_id: str,
        status: str,
        platform_message_id: str | None = None,
        error: str | None = None,
        attempts: int = 0,
    ) -> None:
        """Persist one outbound attempt.

        Args:
            session_key: Target unified_msg_origin.
            message_id: Locally generated outbound message id.
            status: ``sent`` or ``failed``.
            platform_message_id: Platform-side id when available.
            error: Truncated error text for failed sends.
            attempts: Number of attempts performed.
        """
        now = int(time.time())
        row = LifeOutbound(
            session_key=session_key,
            message_id=message_id,
            status=status,
            platform_message_id=platform_message_id,
            error=(error or None) and error[:500],
            attempts=attempts,
            created_at=now,
            updated_at=now,
        )
        async with self.db.get_db() as session:
            session.add(row)
            await session.commit()

    async def stats(self) -> dict[str, Any]:
        """Return archive and outbound counters.

        Returns:
            Mapping with row counts, distinct sessions, and timestamp bounds.
        """
        async with self.db.get_db() as session:
            total = (
                await session.execute(select(func.count()).select_from(LifeMessage))
            ).scalar_one()
            sessions = (
                await session.execute(
                    select(func.count(func.distinct(LifeMessage.session_key)))
                )
            ).scalar_one()
            earliest = (
                await session.execute(select(func.min(LifeMessage.ts)))
            ).scalar_one()
            latest = (
                await session.execute(select(func.max(LifeMessage.ts)))
            ).scalar_one()
            outbound_total = (
                await session.execute(select(func.count()).select_from(LifeOutbound))
            ).scalar_one()
        return {
            "messages": int(total or 0),
            "sessions": int(sessions or 0),
            "earliest_ts": int(earliest) if earliest else None,
            "latest_ts": int(latest) if latest else None,
            "outbound": int(outbound_total or 0),
        }

    async def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the latest archived messages.

        Args:
            limit: Maximum number of rows.

        Returns:
            Row dictionaries ordered by timestamp descending.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifeMessage).order_by(desc(LifeMessage.ts)).limit(limit)  # ty: ignore[invalid-argument-type]  # SQLModel column descriptor
            )
            return [row.model_dump() for row in result.scalars().all()]

    async def fetch_range(
        self,
        session_key: str,
        since_ts: int = 0,
        until_ts: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return archived messages of one session inside a time range.

        Args:
            session_key: Session scope.
            since_ts: Inclusive lower timestamp bound.
            until_ts: Inclusive upper timestamp bound; None means now.
            limit: Maximum number of rows.

        Returns:
            Message rows ordered by timestamp ascending.
        """
        query = select(LifeMessage).where(
            LifeMessage.session_key == session_key  # ty: ignore[invalid-argument-type]  # SQLModel column descriptor
        )
        if since_ts:
            query = query.where(LifeMessage.ts > since_ts)  # ty: ignore[invalid-argument-type]  # SQLModel column descriptor
        if until_ts:
            query = query.where(LifeMessage.ts <= until_ts)  # ty: ignore[invalid-argument-type]  # SQLModel column descriptor
        async with self.db.get_db() as session:
            result = await session.execute(
                query.order_by(LifeMessage.ts).limit(max(limit, 1))  # ty: ignore[invalid-argument-type]  # SQLModel column descriptor
            )
            return [row.model_dump() for row in result.scalars().all()]

    async def active_sessions(
        self,
        since_ts: int = 0,
        limit: int = 20,
    ) -> list[str]:
        """Return sessions that archived messages after a timestamp.

        Args:
            since_ts: Inclusive lower timestamp bound.
            limit: Maximum number of sessions.

        Returns:
            Session keys ordered by most recent activity.
        """
        query = select(col(LifeMessage.session_key)).where(
            col(LifeMessage.ts) >= since_ts
        )
        async with self.db.get_db() as session:
            result = await session.execute(
                query.group_by(col(LifeMessage.session_key))
                .order_by(desc(func.max(col(LifeMessage.ts))))
                .limit(max(limit, 1))
            )
            return [str(row[0]) for row in result.all()]

    async def cleanup(self) -> int:
        """Delete messages older than the configured retention window.

        Returns:
            Number of deleted rows; 0 when retention is disabled.
        """
        if self.retention_days <= 0:
            return 0
        cutoff = int(time.time()) - self.retention_days * 86400
        async with self.db.get_db() as session:
            result = await session.execute(
                delete(LifeMessage).where(LifeMessage.ts < cutoff)  # ty: ignore[invalid-argument-type]  # SQLModel column descriptor
            )
            await session.commit()
            deleted = max(int(result.rowcount or 0), 0)  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount
        if deleted:
            logger.info(f"[life] retention cleanup removed {deleted} messages")
        return deleted
