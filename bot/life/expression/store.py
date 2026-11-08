"""Storage for learned expressions and the learning cursor."""

from __future__ import annotations

import hashlib
import time
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import col

from bot.core.db import BaseDatabase
from bot.life.common import CursorStoreMixin, ensure_tables
from bot.life.expression.models import LifeExpression, LifeExpressionCursor


def expression_checksum(situation: str, style: str) -> str:
    """Return the dedupe hash of an expression.

    Args:
        situation: When the expression applies.
        style: The phrasing itself.

    Returns:
        Hex digest used by the unique constraint.
    """
    payload = f"{situation.strip()}\x00{style.strip()}".encode()
    return hashlib.sha256(payload).hexdigest()


class ExpressionStore(CursorStoreMixin):
    """Async SQLite store for learned expressions."""

    cursor_model = LifeExpressionCursor

    def __init__(self, db_helper: BaseDatabase) -> None:
        """Initialize the store.

        Args:
            db_helper: Core async database helper.
        """
        self.db = db_helper

    async def ensure_schema(self) -> None:
        """Create the expression tables and indexes when missing."""
        await ensure_tables(self.db, LifeExpression, LifeExpressionCursor)

    async def upsert_many(self, rows: Sequence[dict[str, Any]]) -> dict[str, int]:
        """Insert new expressions and reinforce existing ones.

        Args:
            rows: Payloads with ``session_key``, ``situation``, ``style``,
                ``scene``, ``weight``, ``source`` and optional ``tags``.

        Returns:
            Counters with ``inserted`` and ``merged``.
        """
        if not rows:
            return {"inserted": 0, "merged": 0}
        now = int(time.time())
        inserted = 0
        merged = 0
        async with self.db.get_db() as session:
            for row in rows:
                situation = str(row.get("situation") or "").strip()
                style = str(row.get("style") or "").strip()
                if not style:
                    continue
                session_key = str(row["session_key"])
                checksum = expression_checksum(situation, style)
                result = await session.execute(
                    select(LifeExpression).where(
                        col(LifeExpression.session_key) == session_key,
                        col(LifeExpression.checksum) == checksum,
                    )
                )
                existing = result.scalars().first()
                weight = float(row.get("weight") or 1.0)
                if existing is None:
                    session.add(
                        LifeExpression(
                            session_key=session_key,
                            scene=str(row.get("scene") or "any"),
                            situation=situation,
                            style=style,
                            tags=list(row.get("tags") or []),
                            checksum=checksum,
                            weight=weight,
                            source=str(row.get("source") or "heuristic"),
                            status="active",
                            vector_state="pending",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    inserted += 1
                    continue
                existing.weight = float(existing.weight or 0.0) + weight
                existing.updated_at = now
                existing.status = "active"
                if row.get("tags"):
                    tags = list(existing.tags or [])
                    for tag in row["tags"]:
                        if tag not in tags:
                            tags.append(tag)
                    existing.tags = tags
                if existing.vector_state == "skipped":
                    existing.vector_state = "pending"
                merged += 1
            await session.commit()
        return {"inserted": inserted, "merged": merged}

    async def list_expressions(
        self,
        session_key: str,
        status: str = "active",
        scene: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return expressions of one session.

        Args:
            session_key: Session scope.
            status: Lifecycle status filter.
            scene: Optional scene filter; ``any`` expressions always match.
            limit: Maximum rows.

        Returns:
            Row dictionaries ordered by weight.
        """
        query = select(LifeExpression).where(
            col(LifeExpression.session_key) == session_key,
            col(LifeExpression.status) == status,
        )
        if scene:
            query = query.where(col(LifeExpression.scene).in_((scene, "any")))
        async with self.db.get_db() as session:
            result = await session.execute(
                query.order_by(desc(col(LifeExpression.weight))).limit(max(limit, 1))
            )
            return [row.model_dump() for row in result.scalars().all()]

    async def get(self, expression_id: int) -> dict[str, Any] | None:
        """Return one expression row.

        Args:
            expression_id: Row id.

        Returns:
            Row dictionary, or None when missing.
        """
        async with self.db.get_db() as session:
            row = await session.get(LifeExpression, expression_id)
            return row.model_dump() if row else None

    async def bump_usage(self, expression_ids: Iterable[int]) -> int:
        """Record that expressions were used in a reply.

        Args:
            expression_ids: Rows that were injected.

        Returns:
            Number of rows updated.
        """
        ids = [int(value) for value in expression_ids]
        if not ids:
            return 0
        now = int(time.time())
        updated = 0
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifeExpression).where(col(LifeExpression.id).in_(ids))
            )
            for row in result.scalars().all():
                row.usage_count = int(row.usage_count or 0) + 1
                row.last_used_ts = now
                row.weight = float(row.weight or 0.0) + 1.0
                row.updated_at = now
                updated += 1
            await session.commit()
        return updated

    async def pending_for_index(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return expressions that are not in the vector index yet.

        Args:
            limit: Maximum rows.

        Returns:
            Row dictionaries.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifeExpression)
                .where(col(LifeExpression.vector_state) == "pending")
                .order_by(col(LifeExpression.id))
                .limit(max(limit, 1))
            )
            return [row.model_dump() for row in result.scalars().all()]

    async def mark_vector_state(
        self, expression_ids: Iterable[int], state: str
    ) -> None:
        """Update the vector index state of expressions.

        Args:
            expression_ids: Rows to update.
            state: ``pending`` / ``indexed`` / ``skipped``.
        """
        ids = [int(value) for value in expression_ids]
        if not ids:
            return
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifeExpression).where(col(LifeExpression.id).in_(ids))
            )
            for row in result.scalars().all():
                row.vector_state = state
            await session.commit()

    async def delete(
        self,
        expression_ids: Iterable[int] | None = None,
        session_key: str | None = None,
    ) -> int:
        """Delete expressions by id or session.

        Args:
            expression_ids: Explicit row ids.
            session_key: Delete every expression of one session.

        Returns:
            Number of deleted rows.

        Raises:
            ValueError: When neither filter is given.
        """
        if not expression_ids and not session_key:
            raise ValueError("delete requires expression_ids or session_key")
        async with self.db.get_db() as session:
            if expression_ids:
                result = await session.execute(
                    delete(LifeExpression).where(
                        col(LifeExpression.id).in_(tuple(expression_ids))
                    )
                )
            else:
                result = await session.execute(
                    delete(LifeExpression).where(
                        col(LifeExpression.session_key) == session_key
                    )
                )
            await session.commit()
            return max(int(result.rowcount or 0), 0)  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount

    async def stats(self) -> dict[str, Any]:
        """Return expression counters.

        Returns:
            Mapping with totals, per-source and per-vector-state counts.
        """
        async with self.db.get_db() as session:
            total = (
                await session.execute(select(func.count()).select_from(LifeExpression))
            ).scalar_one()
            indexed = (
                await session.execute(
                    select(func.count())
                    .select_from(LifeExpression)
                    .where(col(LifeExpression.vector_state) == "indexed")
                )
            ).scalar_one()
            by_source = await session.execute(
                select(col(LifeExpression.source), func.count()).group_by(
                    col(LifeExpression.source)
                )
            )
            by_scene = await session.execute(
                select(col(LifeExpression.scene), func.count()).group_by(
                    col(LifeExpression.scene)
                )
            )
        return {
            "total": int(total or 0),
            "indexed": int(indexed or 0),
            "by_source": {source: int(count) for source, count in by_source.all()},
            "by_scene": {scene: int(count) for scene, count in by_scene.all()},
        }

    async def insert_rows(self, rows: Sequence[dict[str, Any]]) -> int:
        """Bulk insert rows that are known to be new.

        Args:
            rows: Fully specified row payloads.

        Returns:
            Number of inserted rows.
        """
        if not rows:
            return 0
        async with self.db.get_db() as session:
            result = await session.execute(
                sqlite_insert(LifeExpression)
                .values(list(rows))
                .on_conflict_do_nothing(
                    index_elements=["session_key", "checksum"],
                )
            )
            await session.commit()
            return max(int(result.rowcount or 0), 0)  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount
