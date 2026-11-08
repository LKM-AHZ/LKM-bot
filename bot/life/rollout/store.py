"""Storage and aggregation for the gray-rollout decision log (S7a).

The report is the input of the gray dashboard: how many turns were released,
how many would have been answered, how the debounce latency is distributed and
which sessions dominate the window. SQL does the counting; the latency
percentiles are computed in Python because SQLite has no percentile function.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import case, delete, desc, func, select
from sqlmodel import col

from bot.core.db import BaseDatabase
from bot.life.common import ensure_tables
from bot.life.rollout.models import LifeTurnDecision


def _percentile(values: list[int], share: float) -> int:
    """Return the nearest-rank percentile of a list.

    Args:
        values: Latency samples; the caller sorts them.
        share: Share in ``[0, 1]``, e.g. ``0.9`` for P90.

    Returns:
        Sample at the requested rank, or 0 for an empty list.
    """
    if not values:
        return 0
    index = min(max(int(round(share * (len(values) - 1))), 0), len(values) - 1)
    return int(values[index])


class TurnDecisionStore:
    """Async store of released turn decisions."""

    def __init__(self, db_helper: BaseDatabase) -> None:
        """Initialize the store.

        Args:
            db_helper: Core async database helper.
        """
        self.db = db_helper

    async def ensure_schema(self) -> None:
        """Create the decision table when missing."""
        await ensure_tables(self.db, LifeTurnDecision)

    async def insert(self, row: dict[str, Any]) -> None:
        """Insert one decision row.

        Args:
            row: Column payload built by the turn runtime.
        """
        async with self.db.get_db() as session:
            session.add(LifeTurnDecision(**row))
            await session.commit()

    async def prune(self, retention_days: int, now: float | None = None) -> int:
        """Delete decisions older than the retention window.

        Args:
            retention_days: Days to keep; ``0`` disables pruning.
            now: Clock override for tests.

        Returns:
            Number of deleted rows.
        """
        if retention_days <= 0:
            return 0
        cutoff = (now if now is not None else time.time()) - retention_days * 86400
        async with self.db.get_db() as session:
            result = await session.execute(
                delete(LifeTurnDecision).where(col(LifeTurnDecision.ts) < cutoff)
            )
            await session.commit()
            return int(result.rowcount or 0)  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount

    async def report(
        self,
        days: int,
        now: float | None = None,
        session_limit: int = 10,
        recent_limit: int = 20,
    ) -> dict[str, Any]:
        """Aggregate the decisions of one window.

        Args:
            days: Window length in days.
            now: Clock override for tests.
            session_limit: Sessions returned in the leaderboard.
            recent_limit: Recent rows returned alongside the aggregates.

        Returns:
            Mapping with counts, reply rate, latency percentiles, the busiest
            sessions and the latest rows.
        """
        end = now if now is not None else time.time()
        since = end - max(days, 1) * 86400
        window = col(LifeTurnDecision.ts) >= since
        async with self.db.get_db() as session:
            total = int(
                (
                    await session.execute(
                        select(func.count()).select_from(LifeTurnDecision).where(window)
                    )
                ).scalar_one()
                or 0
            )
            by_decision = {
                str(decision): int(count)
                for decision, count in (
                    await session.execute(
                        select(
                            col(LifeTurnDecision.decision),
                            func.count(),
                        )
                        .where(window)
                        .group_by(col(LifeTurnDecision.decision))
                    )
                ).all()
            }
            shadowed = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(LifeTurnDecision)
                        .where(
                            window,
                            col(LifeTurnDecision.decision) == "reply",
                            col(LifeTurnDecision.shadow).is_(True),
                        )
                    )
                ).scalar_one()
                or 0
            )
            latencies = sorted(
                (
                    await session.execute(
                        select(col(LifeTurnDecision.latency_ms)).where(window)
                    )
                )
                .scalars()
                .all()
            )
            session_rows = (
                await session.execute(
                    select(
                        col(LifeTurnDecision.session_key),
                        func.count().label("turns"),
                        func.sum(
                            case(
                                (col(LifeTurnDecision.decision) == "reply", 1),
                                else_=0,
                            )
                        ).label("reply"),
                        func.max(col(LifeTurnDecision.ts)).label("last_ts"),
                    )
                    .where(window)
                    .group_by(col(LifeTurnDecision.session_key))
                    .order_by(desc(func.count()))
                    .limit(session_limit)
                )
            ).all()
            recent = (
                (
                    await session.execute(
                        select(LifeTurnDecision)
                        .where(window)
                        .order_by(desc(col(LifeTurnDecision.ts)))
                        .limit(recent_limit)
                    )
                )
                .scalars()
                .all()
            )
        reply = by_decision.get("reply", 0)
        return {
            "window_days": days,
            "since": round(since, 2),
            "turns": total,
            "reply": reply,
            "wait": total - reply,
            "shadowed": shadowed,
            "live": reply - shadowed,
            "reply_rate": round(reply / total, 3) if total else 0.0,
            "latency_ms": {
                "p50": _percentile(latencies, 0.5),
                "p90": _percentile(latencies, 0.9),
                "max": latencies[-1] if latencies else 0,
            },
            "sessions": [
                {
                    "session_key": session_key,
                    "turns": int(turns or 0),
                    "reply": int(reply_count or 0),
                    "last_ts": round(float(last_ts or 0), 2),
                }
                for session_key, turns, reply_count, last_ts in session_rows
            ],
            "recent": [
                {
                    "ts": round(row.ts, 2),
                    "session_key": row.session_key,
                    "messages": row.messages,
                    "decision": row.decision,
                    "action": row.action,
                    "shadow": row.shadow,
                    "reason": row.reason,
                    "likelihood": round(row.likelihood, 3),
                    "threshold": round(row.threshold, 3),
                    "latency_ms": row.latency_ms,
                    "text_chars": row.text_chars,
                }
                for row in recent
            ],
        }
