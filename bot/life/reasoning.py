"""Reply reasoning records (port of MaiBot 1.4 reasoning-process log).

Upstream records the request snapshot and the generated reply of every turn and
exposes them in the panel. The fork has no planner snapshot, but it already owns
two seams per request: prompt assembly before the provider call and outbound
post-processing after the reply. Both are keyed by ``event.trace.trace_id``, so
one compact row per turn is enough to browse "what was asked, what was said".

Nothing here may break a reply: every write is best-effort and bounded.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import JSON, Text
from sqlmodel import Field, SQLModel, col, delete, select

from bot import logger
from bot.core.db import BaseDatabase


class LifeReasoning(SQLModel, table=True):
    """One turn's request/reply snapshot."""

    __tablename__ = "life_reasoning"

    id: int | None = Field(default=None, primary_key=True)
    trace_id: str = Field(index=True, max_length=64, unique=True)
    session_key: str = Field(index=True, max_length=255)
    created_at: int = Field(default=0, index=True)
    updated_at: int = 0
    model: str = Field(default="", max_length=128)
    tools: list = Field(default_factory=list, sa_type=JSON)
    request_system: str = Field(default="", sa_type=Text)
    request_prompt: str = Field(default="", sa_type=Text)
    reply_text: str = Field(default="", sa_type=Text)
    reply_chars: int = 0


class ReasoningRecorder:
    """Bounded writer/reader of the reasoning table."""

    def __init__(self, db: BaseDatabase, config: Any) -> None:
        """Initialize the recorder.

        Args:
            db: Core async database helper.
            config: ``ReasoningConfig`` with bounds and retention.
        """
        self.db = db
        self.config = config

    async def ensure_schema(self) -> None:
        """Create the table and drop expired rows."""
        async with self.db.get_db() as session:
            connection = await session.connection()
            await connection.run_sync(
                lambda sync_connection: LifeReasoning.__table__.create(  # ty: ignore[unresolved-attribute]  # SQLModel adds __table__ at runtime
                    sync_connection,
                    checkfirst=True,
                )
            )
            await session.commit()
        await self.prune()

    def _bounded(self, text: Any) -> str:
        """Truncate one field to the configured size."""
        value = "" if text is None else str(text)
        limit = max(int(getattr(self.config, "max_chars", 4000)), 0)
        return value[:limit] if limit else value

    async def prune(self) -> int:
        """Delete rows older than the retention window.

        Returns:
            Number of removed rows.
        """
        days = int(getattr(self.config, "retention_days", 7) or 0)
        if days <= 0:
            return 0
        cutoff = int(time.time()) - days * 86400
        async with self.db.get_db() as session:
            result = await session.execute(
                delete(LifeReasoning).where(col(LifeReasoning.created_at) < cutoff)
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    async def remember_request(self, event: Any, req: Any) -> None:
        """Insert or update the request half of one turn.

        Args:
            event: Event whose ``trace.trace_id`` keys the row.
            req: ``ProviderRequest`` about to be sent.
        """
        trace_id = self._trace_id(event)
        if not trace_id:
            return
        tools: list[str] = []
        try:
            tool_set = getattr(req, "func_tool", None)
            tools = list(tool_set.names()) if tool_set is not None else []
        except Exception:
            tools = []
        session_key = str(getattr(event, "unified_msg_origin", "") or "")[:255]
        model = self._bounded(getattr(req, "model", "") or "")[:128]
        tool_names = [str(name)[:64] for name in tools[:64]]
        system_prompt = self._bounded(getattr(req, "system_prompt", ""))
        prompt = self._bounded(getattr(req, "prompt", ""))
        now = int(time.time())
        try:
            async with self.db.get_db() as session:
                rows = (
                    (
                        await session.execute(
                            select(LifeReasoning)
                            .where(col(LifeReasoning.trace_id) == trace_id)
                            .limit(1)
                        )
                    )
                    .scalars()
                    .all()
                )
                row = rows[0] if rows else None
                if row is None:
                    row = LifeReasoning(
                        trace_id=trace_id,
                        session_key=session_key,
                        created_at=now,
                        updated_at=now,
                        model=model,
                        tools=tool_names,
                        request_system=system_prompt,
                        request_prompt=prompt,
                    )
                    session.add(row)
                else:
                    row.session_key = session_key
                    row.model = model
                    row.tools = tool_names
                    row.request_system = system_prompt
                    row.request_prompt = prompt
                    row.updated_at = now
                await session.commit()
        except Exception:
            logger.exception("[life] reasoning request record failed")

    async def remember_reply(self, event: Any, text: str) -> None:
        """Attach the final reply text to the turn's row.

        Args:
            event: The same event that produced the request.
            text: Final outbound text after every rewrite.
        """
        trace_id = self._trace_id(event)
        if not trace_id:
            return
        body = self._bounded(text)
        try:
            async with self.db.get_db() as session:
                rows = (
                    (
                        await session.execute(
                            select(LifeReasoning)
                            .where(col(LifeReasoning.trace_id) == trace_id)
                            .limit(1)
                        )
                    )
                    .scalars()
                    .all()
                )
                row = rows[0] if rows else None
                if row is None:
                    row = LifeReasoning(
                        trace_id=trace_id,
                        session_key=str(getattr(event, "unified_msg_origin", "") or "")[
                            :255
                        ],
                        created_at=int(time.time()),
                    )
                    session.add(row)
                row.reply_text = body
                row.reply_chars = len(body)
                row.updated_at = int(time.time())
                await session.commit()
        except Exception:
            logger.exception("[life] reasoning reply record failed")

    async def list_recent(
        self,
        limit: int = 20,
        session_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return compact recent records, newest first.

        Args:
            limit: Maximum rows.
            session_key: Optional session filter.

        Returns:
            Summary rows without the full prompt and reply bodies.
        """
        query = select(LifeReasoning).order_by(col(LifeReasoning.id).desc())
        if session_key:
            query = query.where(col(LifeReasoning.session_key) == session_key)
        async with self.db.get_db() as session:
            rows = (await session.execute(query.limit(max(limit, 1)))).scalars().all()
            return [
                {
                    "id": row.id,
                    "trace_id": row.trace_id,
                    "session_key": row.session_key,
                    "created_at": row.created_at,
                    "model": row.model,
                    "tools": list(row.tools or []),
                    "reply_chars": row.reply_chars,
                    "request_excerpt": (row.request_prompt or "")[:200],
                    "reply_excerpt": (row.reply_text or "")[:200],
                }
                for row in rows
            ]

    async def get(self, record_id: int) -> dict[str, Any] | None:
        """Return one full record.

        Args:
            record_id: Row id.

        Returns:
            Full row, or None when it does not exist.
        """
        async with self.db.get_db() as session:
            row = await session.get(LifeReasoning, record_id)
            if row is None:
                return None
            return {
                "id": row.id,
                "trace_id": row.trace_id,
                "session_key": row.session_key,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "model": row.model,
                "tools": list(row.tools or []),
                "request_system": row.request_system,
                "request_prompt": row.request_prompt,
                "reply_text": row.reply_text,
                "reply_chars": row.reply_chars,
            }

    @staticmethod
    def _trace_id(event: Any) -> str:
        """Return the trace id that keys one turn, or empty."""
        trace = getattr(event, "trace", None)
        return str(getattr(trace, "trace_id", "") or "")[:64]
