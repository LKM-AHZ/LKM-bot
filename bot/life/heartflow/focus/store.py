"""Storage for the focus timeline: digest, turns and checkpoints."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, desc, func, select
from sqlmodel import col

from bot.core.db import BaseDatabase
from bot.life.heartflow.focus.models import LifeFocus, LifeFocusTurn


class FocusStore:
    """Async SQLite store for session timelines."""

    def __init__(self, db_helper: BaseDatabase) -> None:
        """Initialize the store.

        Args:
            db_helper: Core async database helper.
        """
        self.db = db_helper

    async def ensure_schema(self) -> None:
        """Create the focus tables when missing."""
        async with self.db.get_db() as session:
            connection = await session.connection()
            for model in (LifeFocus, LifeFocusTurn):
                await connection.run_sync(
                    lambda sync_connection, model=model: model.__table__.create(  # ty: ignore[unresolved-attribute]  # SQLModel adds __table__ at runtime
                        sync_connection,
                        checkfirst=True,
                    )
                )
            await session.commit()

    async def load(
        self,
        session_key: str,
        limit: int = 60,
    ) -> dict[str, Any]:
        """Load the digest and the most recent turns of one session.

        Args:
            session_key: unified_msg_origin.
            limit: Maximum number of turns to return.

        Returns:
            Mapping with ``digest``, ``compressed_turns``, ``total_messages``,
            ``next_index`` and ``turns`` (oldest first, serialized form).
        """
        async with self.db.get_db() as session:
            head = (
                (
                    await session.execute(
                        select(LifeFocus).where(
                            col(LifeFocus.session_key) == session_key
                        )
                    )
                )
                .scalars()
                .first()
            )
            rows = (
                (
                    await session.execute(
                        select(LifeFocusTurn)
                        .where(col(LifeFocusTurn.session_key) == session_key)
                        .order_by(desc(col(LifeFocusTurn.turn_index)))
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        turns = [self._turn_payload(row) for row in reversed(list(rows))]
        return {
            "session_key": session_key,
            "digest": head.digest if head else "",
            "compressed_turns": head.compressed_turns if head else 0,
            "total_messages": head.total_messages if head else 0,
            "next_index": max((turn["index"] for turn in turns), default=0) + 1,
            "turns": turns,
        }

    async def save_turn(
        self,
        session_key: str,
        turn: dict[str, Any],
    ) -> None:
        """Insert or update one turn.

        Args:
            session_key: unified_msg_origin.
            turn: Turn payload from ``FocusTurn.to_dict()``.
        """
        now = int(time.time())
        index = int(turn.get("index") or 0)
        async with self.db.get_db() as session:
            row = (
                (
                    await session.execute(
                        select(LifeFocusTurn).where(
                            col(LifeFocusTurn.session_key) == session_key,
                            col(LifeFocusTurn.turn_index) == index,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                row = LifeFocusTurn(
                    session_key=session_key,
                    turn_index=index,
                    created_at=now,
                )
                session.add(row)
            row.started_ts = float(turn.get("started_ts") or 0.0)
            row.ended_ts = float(turn.get("ended_ts") or 0.0)
            row.status = str(turn.get("status") or "closed")
            row.summary = str(turn.get("summary") or "")
            row.messages = list(turn.get("messages") or [])
            row.updated_at = now
            await session.commit()

    async def save_head(
        self,
        session_key: str,
        *,
        digest: str,
        compressed_turns: int,
        total_messages: int,
        last_turn_index: int,
    ) -> None:
        """Insert or update the digest row of one session.

        Args:
            session_key: unified_msg_origin.
            digest: Folded conversation digest.
            compressed_turns: Turns folded so far.
            total_messages: Messages observed so far.
            last_turn_index: Highest turn index written.
        """
        now = int(time.time())
        async with self.db.get_db() as session:
            row = (
                (
                    await session.execute(
                        select(LifeFocus).where(
                            col(LifeFocus.session_key) == session_key
                        )
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                row = LifeFocus(session_key=session_key)
                session.add(row)
            row.digest = digest
            row.compressed_turns = compressed_turns
            row.total_messages = total_messages
            row.last_turn_index = last_turn_index
            row.updated_at = now
            await session.commit()

    async def prune(self, session_key: str, keep: int) -> int:
        """Delete the oldest turns beyond ``keep``.

        Args:
            session_key: unified_msg_origin.
            keep: Turns to retain.

        Returns:
            Number of deleted rows.
        """
        if keep <= 0:
            return 0
        async with self.db.get_db() as session:
            rows = (
                (
                    await session.execute(
                        select(col(LifeFocusTurn.turn_index))
                        .where(col(LifeFocusTurn.session_key) == session_key)
                        .order_by(desc(col(LifeFocusTurn.turn_index)))
                        .offset(keep)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return 0
            await session.execute(
                delete(LifeFocusTurn).where(
                    col(LifeFocusTurn.session_key) == session_key,
                    col(LifeFocusTurn.turn_index).in_(list(rows)),
                )
            )
            await session.commit()
        return len(rows)

    async def delete(self, session_key: str) -> int:
        """Remove the timeline of one session.

        Args:
            session_key: unified_msg_origin.

        Returns:
            Number of turn rows deleted.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                delete(LifeFocusTurn).where(
                    col(LifeFocusTurn.session_key) == session_key
                )
            )
            await session.execute(
                delete(LifeFocus).where(col(LifeFocus.session_key) == session_key)
            )
            await session.commit()
        return int(result.rowcount or 0)  # ty: ignore[unresolved-attribute]

    async def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """List sessions that have a timeline.

        Args:
            limit: Maximum number of sessions.

        Returns:
            Session summaries, most recently updated first.
        """
        async with self.db.get_db() as session:
            rows = (
                (
                    await session.execute(
                        select(LifeFocus)
                        .order_by(desc(col(LifeFocus.updated_at)))
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [
            {
                "session_key": row.session_key,
                "digest_chars": len(row.digest or ""),
                "compressed_turns": row.compressed_turns,
                "total_messages": row.total_messages,
                "last_turn_index": row.last_turn_index,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]

    async def stats(self) -> dict[str, Any]:
        """Return storage counters.

        Returns:
            Mapping with session count, turn count and digest size.
        """
        async with self.db.get_db() as session:
            sessions = (
                await session.execute(select(func.count()).select_from(LifeFocus))
            ).scalar_one()
            turns = (
                await session.execute(select(func.count()).select_from(LifeFocusTurn))
            ).scalar_one()
            chars = (
                await session.execute(
                    select(func.coalesce(func.sum(func.length(LifeFocus.digest)), 0))
                )
            ).scalar_one()
        return {
            "sessions": int(sessions or 0),
            "turns": int(turns or 0),
            "digest_chars": int(chars or 0),
        }

    async def list_turns(
        self,
        session_key: str,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """List the stored turns of one session.

        Args:
            session_key: unified_msg_origin.
            limit: Maximum number of turns.

        Returns:
            Turns, newest first.
        """
        async with self.db.get_db() as session:
            rows = (
                (
                    await session.execute(
                        select(LifeFocusTurn)
                        .where(col(LifeFocusTurn.session_key) == session_key)
                        .order_by(desc(col(LifeFocusTurn.turn_index)))
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [self._turn_payload(row) for row in rows]

    @staticmethod
    def _turn_payload(row: LifeFocusTurn) -> dict[str, Any]:
        """Return the serializable form of one turn row.

        Args:
            row: Stored turn.

        Returns:
            Mapping compatible with ``FocusTurn.from_dict``.
        """
        return {
            "index": row.turn_index,
            "started_ts": row.started_ts,
            "ended_ts": row.ended_ts,
            "status": row.status,
            "summary": row.summary,
            "messages": list(row.messages or []),
        }
