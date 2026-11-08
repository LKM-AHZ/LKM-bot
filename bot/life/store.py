"""Persistence helpers for the Life layer.

Inbound messages live in the core ``platform_message_history`` table: it is the
single archive used by the dashboard, the LTM context and every Life learner.
Rows written here carry a normalized message chain for other consumers plus a
``life`` metadata block (timestamp, group, components) and a ``source_message_id``
so the same platform message is stored exactly once.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import col

from bot import logger
from bot.core.db import BaseDatabase
from bot.core.db.po import PlatformMessageHistory
from bot.life.common import ensure_tables
from bot.life.models import LifeOutbound

_MEDIA_TYPES = {"image", "record", "video", "file"}


def _parts_from_components(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Life component payloads into the core history format.

    Args:
        components: Normalized component dicts from ``normalize_event``.

    Returns:
        Parts accepted by the dashboard and LTM history readers.
    """
    parts: list[dict[str, Any]] = []
    for component in components:
        kind = str(component.get("type") or "unknown")
        raw_data = component.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        if kind in {"text", "plain"}:
            text = str(data.get("text") or "")
            if text:
                parts.append({"type": "plain", "text": text})
        elif kind == "at":
            parts.append(
                {
                    "type": "at",
                    "user_id": str(data.get("qq") or data.get("user_id") or ""),
                    "name": str(data.get("name") or ""),
                }
            )
        elif kind == "reply":
            parts.append(
                {
                    "type": "reply",
                    "message_id": str(data.get("id") or ""),
                    "sender_name": str(data.get("sender_nickname") or ""),
                    "text": str(data.get("message_str") or ""),
                }
            )
        elif kind in _MEDIA_TYPES:
            parts.append({"type": "plain", "text": f"[{kind}]"})
        else:
            parts.append({"type": "plain", "text": f"[{kind}]"})
    return parts


def _group_id_from_umo(user_id: str) -> str | None:
    """Return the group id encoded in a unified message origin.

    Args:
        user_id: Unified message origin.

    Returns:
        Group id, or None for private sessions.
    """
    parts = user_id.split(":")
    if len(parts) >= 3 and parts[1].lower() == "groupmessage":
        return parts[2] or None
    return None


def _int_ts(value: Any) -> int:
    """Convert a datetime or numeric timestamp to Unix seconds.

    Args:
        value: Datetime or numeric value.

    Returns:
        Unix timestamp; 0 when the value is unusable.
    """
    if isinstance(value, datetime):
        return int(value.replace(tzinfo=timezone.utc).timestamp())
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class LifeStore:
    """Thin async wrapper around the unified message archive."""

    def __init__(self, db_helper: BaseDatabase, retention_days: int = 0) -> None:
        """Initialize the store.

        Args:
            db_helper: Core async database helper.
            retention_days: Retention window; 0 keeps rows forever.
        """
        self.db = db_helper
        self.retention_days = retention_days

    async def ensure_schema(self) -> None:
        """Create the outbound table when it is missing."""
        await ensure_tables(self.db, LifeOutbound)

    def _content(
        self,
        row: dict[str, Any],
        life_batch: str | None = None,
    ) -> dict[str, Any]:
        """Build the stored content for one archived message.

        Args:
            row: ``IngestedMessage.to_row()`` payload.
            life_batch: Migration batch id, or None for live traffic.

        Returns:
            Core history content with the Life metadata block.
        """
        components = list(row.get("components") or [])
        meta = {
            "message_id": row.get("message_id"),
            "ts": row.get("ts"),
            "group_id": row.get("group_id"),
            "reply_to": row.get("reply_to"),
            "flags": row.get("flags") or {},
            "plain_text": row.get("plain_text") or "",
            "components": components,
            "ingested_at": row.get("ingested_at") or 0,
        }
        if life_batch:
            meta["batch"] = life_batch
        return {
            "type": "user",
            "message": _parts_from_components(components),
            "life": meta,
        }

    async def insert_batch(
        self,
        rows: list[dict[str, Any]],
        *,
        life_batch: str | None = None,
    ) -> int:
        """Insert archived inbound messages, ignoring duplicates.

        Args:
            rows: Row payloads produced by ``IngestedMessage.to_row()``.
            life_batch: Migration batch id; None for live traffic.

        Returns:
            Number of rows actually inserted.
        """
        if not rows:
            return 0
        values = [
            {
                "platform_id": row["platform_id"],
                "user_id": row["session_key"],
                "content": self._content(row, life_batch),
                "sender_id": row.get("sender_id"),
                "sender_name": row.get("sender_name"),
                "source_message_id": row.get("message_id") or None,
                "life_batch": life_batch,
            }
            for row in rows
        ]
        async with self.db.get_db() as session:
            statement = (
                sqlite_insert(PlatformMessageHistory)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=["platform_id", "source_message_id"],
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

    @staticmethod
    def _row(row: PlatformMessageHistory) -> dict[str, Any]:
        """Convert one core history row into a Life archive row.

        Args:
            row: Core history record.

        Returns:
            Row dictionary with the keys Life consumers expect.
        """
        content: dict[str, Any] = (
            row.content if isinstance(row.content, dict) else {}
        )
        raw_life = content.get("life")
        life: dict[str, Any] = raw_life if isinstance(raw_life, dict) else {}
        ts = _int_ts(life.get("ts")) or _int_ts(row.created_at)
        plain_text = str(life.get("plain_text") or "")
        if not plain_text:
            parts = content.get("message")
            if isinstance(parts, list):
                plain_text = "".join(
                    str(part.get("text") or "")
                    for part in parts
                    if isinstance(part, dict) and part.get("type") == "plain"
                )
        return {
            "id": row.id,
            "platform_id": row.platform_id,
            "session_key": row.user_id,
            "message_id": row.source_message_id or life.get("message_id") or "",
            "ts": ts,
            "sender_id": row.sender_id,
            "sender_name": row.sender_name,
            "group_id": life.get("group_id") or _group_id_from_umo(row.user_id),
            "components": life.get("components") or [],
            "plain_text": plain_text,
            "reply_to": life.get("reply_to"),
            "flags": life.get("flags") or {},
            "ingested_at": life.get("ingested_at") or ts,
            "life_batch": row.life_batch,
        }

    async def stats(self) -> dict[str, Any]:
        """Return archive and outbound counters.

        Returns:
            Mapping with row counts, distinct sessions, and timestamp bounds.
        """
        async with self.db.get_db() as session:
            archived = col(PlatformMessageHistory.source_message_id).is_not(None)
            ts_expr = func.json_extract(
                col(PlatformMessageHistory.content), "$.life.ts"
            )
            total = (
                await session.execute(
                    select(func.count())
                    .select_from(PlatformMessageHistory)
                    .where(archived)
                )
            ).scalar_one()
            sessions = (
                await session.execute(
                    select(
                        func.count(func.distinct(PlatformMessageHistory.user_id))
                    ).where(archived)
                )
            ).scalar_one()
            bounds = (
                await session.execute(
                    select(func.min(ts_expr), func.max(ts_expr)).where(archived)
                )
            ).one()
            earliest, latest = bounds
            outbound_total = (
                await session.execute(select(func.count()).select_from(LifeOutbound))
            ).scalar_one()
        return {
            "messages": int(total or 0),
            "sessions": int(sessions or 0),
            "earliest_ts": _int_ts(earliest) or None,
            "latest_ts": _int_ts(latest) or None,
            "outbound": int(outbound_total or 0),
        }

    async def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the latest archived messages.

        Args:
            limit: Maximum number of rows.

        Returns:
            Row dictionaries ordered by id descending.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                select(PlatformMessageHistory)
                .where(col(PlatformMessageHistory.source_message_id).is_not(None))
                .order_by(desc(col(PlatformMessageHistory.id)))
                .limit(max(limit, 1))
            )
            return [self._row(row) for row in result.scalars().all()]

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
            since_ts: Exclusive lower timestamp bound.
            until_ts: Inclusive upper timestamp bound; None means now.
            limit: Maximum number of rows.

        Returns:
            Message rows ordered by timestamp ascending.
        """
        async with self.db.get_db() as session:
            query = select(PlatformMessageHistory).where(
                col(PlatformMessageHistory.user_id) == session_key,
                col(PlatformMessageHistory.source_message_id).is_not(None),
            )
            if since_ts:
                query = query.where(
                    col(PlatformMessageHistory.created_at)
                    > datetime.fromtimestamp(since_ts, timezone.utc).replace(
                        tzinfo=None
                    )
                )
            if until_ts:
                query = query.where(
                    col(PlatformMessageHistory.created_at)
                    <= datetime.fromtimestamp(until_ts, timezone.utc).replace(
                        tzinfo=None
                    )
                )
            result = await session.execute(
                query.order_by(col(PlatformMessageHistory.id))
            )
            rows = [self._row(row) for row in result.scalars().all()]
        if since_ts:
            rows = [row for row in rows if row["ts"] > since_ts]
        if until_ts:
            rows = [row for row in rows if row["ts"] <= until_ts]
        return rows[: max(limit, 1)]

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
        async with self.db.get_db() as session:
            query = (
                select(col(PlatformMessageHistory.user_id))
                .where(col(PlatformMessageHistory.source_message_id).is_not(None))
                .group_by(col(PlatformMessageHistory.user_id))
                .order_by(desc(func.max(col(PlatformMessageHistory.id))))
                .limit(max(limit, 1))
            )
            if since_ts:
                query = query.where(
                    col(PlatformMessageHistory.created_at)
                    >= datetime.fromtimestamp(since_ts, timezone.utc).replace(
                        tzinfo=None
                    )
                )
            result = await session.execute(query)
            return [str(row[0]) for row in result.all()]

    async def session_last_ts(
        self,
        since_ts: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return the last activity of each session.

        Args:
            since_ts: Inclusive lower timestamp bound.
            limit: Maximum number of sessions.

        Returns:
            Rows with ``session_key``, ``last_ts`` and ``messages``, ordered by
            most recent activity.
        """
        async with self.db.get_db() as session:
            query = (
                select(
                    col(PlatformMessageHistory.user_id),
                    func.max(col(PlatformMessageHistory.created_at)),
                    func.count(),
                )
                .where(col(PlatformMessageHistory.source_message_id).is_not(None))
                .group_by(col(PlatformMessageHistory.user_id))
                .order_by(desc(func.max(col(PlatformMessageHistory.created_at))))
                .limit(max(limit, 1))
            )
            if since_ts:
                query = query.where(
                    col(PlatformMessageHistory.created_at)
                    >= datetime.fromtimestamp(since_ts, timezone.utc).replace(
                        tzinfo=None
                    )
                )
            result = await session.execute(query)
            rows = result.all()
        return [
            {
                "session_key": str(row[0]),
                "last_ts": _int_ts(row[1]),
                "messages": int(row[2] or 0),
            }
            for row in rows
        ]

    async def cleanup(self) -> int:
        """Delete archived messages older than the retention window.

        Returns:
            Number of deleted rows; 0 when retention is disabled.
        """
        if self.retention_days <= 0:
            return 0
        cutoff = int(time.time()) - self.retention_days * 86400
        ts_expr = func.json_extract(col(PlatformMessageHistory.content), "$.life.ts")
        async with self.db.get_db() as session:
            result = await session.execute(
                delete(PlatformMessageHistory).where(
                    col(PlatformMessageHistory.source_message_id).is_not(None),
                    ts_expr < cutoff,
                )
            )
            await session.commit()
            deleted = max(int(result.rowcount or 0), 0)  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount
        if deleted:
            logger.info(f"[life] retention cleanup removed {deleted} messages")
        return deleted
