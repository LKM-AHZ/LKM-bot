"""Gray-rollout runtime: durable observation of released turns (S7a).

The runtime already knows what a turn decided and how long it waited; this
service is where that knowledge survives a restart. It is deliberately passive:
it never answers, never changes a decision and never sends anything, so a gray
window can be reviewed from the panel long after the in-memory ring rolled over.
"""

from __future__ import annotations

import time
from typing import Any

from bot import logger
from bot.life.config import LifeConfig
from bot.life.rollout.store import TurnDecisionStore

REPORT_DAYS = 14
"""Default window of the gray report: two weeks is the exit gate of S7."""

REPORT_DAYS_MAX = 90
"""Upper bound of the report window; also the default retention."""

STATUS_CACHE_SECONDS = 60.0
"""How long the summary embedded in ``/life/status`` stays cached."""


class RolloutService:
    """Persist released turn decisions and aggregate them per window."""

    def __init__(
        self,
        db_helper: Any,
        life_config: LifeConfig,
        clock: Any = None,
    ) -> None:
        """Initialize the service without touching the database.

        Args:
            db_helper: Core async database helper.
            life_config: Root Life configuration (scope and runtime settings).
            clock: Clock override for tests.
        """
        self.db = db_helper
        self.life_config = life_config
        self.config = life_config.heartflow.runtime
        self.store = TurnDecisionStore(db_helper)
        self._clock = clock or time.time
        self._started = False
        self._counters = {"observed": 0, "failed": 0, "pruned": 0}
        self._summary: dict[str, Any] | None = None
        self._summary_at = 0.0

    async def initialize(self) -> None:
        """Create the decision table and prune the expired tail."""
        await self.store.ensure_schema()
        self._started = True
        try:
            pruned = await self.store.prune(
                self.config.decision_retention_days,
                self._clock(),
            )
            self._counters["pruned"] += pruned
        except Exception:
            logger.exception("[life] rollout prune failed during startup")
        logger.info(
            "[life] rollout observer ready; shadow=%s percent=%s retention_days=%s",
            self.config.shadow,
            self.life_config.scope.rollout_percent,
            self.config.decision_retention_days,
        )

    async def observe(self, payload: dict[str, Any]) -> None:
        """Persist one released turn.

        Args:
            payload: Column payload from the turn runtime; never raises, a lost
                observation must not break the conversation.
        """
        if not self._started:
            return
        row = dict(payload)
        row["created_at"] = int(self._clock())
        try:
            await self.store.insert(row)
            self._counters["observed"] += 1
            self._summary = None
        except Exception:
            self._counters["failed"] += 1
            logger.exception("[life] rollout observation failed")

    async def prune(self) -> int:
        """Drop observations older than the retention window.

        Returns:
            Number of deleted rows, or 0 when pruning is disabled.
        """
        deleted = await self.store.prune(
            self.config.decision_retention_days,
            self._clock(),
        )
        self._counters["pruned"] += deleted
        return deleted

    async def report(self, days: int | None = None) -> dict[str, Any]:
        """Return the aggregate report of a window.

        Args:
            days: Window length; defaults to :data:`REPORT_DAYS` and is clamped
                to ``[1, REPORT_DAYS_MAX]``.

        Returns:
            Report produced by the store.
        """
        window = min(max(int(days or REPORT_DAYS), 1), REPORT_DAYS_MAX)
        return await self.store.report(window, self._clock())

    async def status(self) -> dict[str, Any]:
        """Return the status block embedded in ``/life/status``.

        The summary is cached for a minute because the dashboard polls status
        far more often than it needs a fresh gray report.

        Returns:
            Mapping with the rollout scope, cached window summary and counters.
        """
        now = self._clock()
        if self._summary is None or now - self._summary_at >= STATUS_CACHE_SECONDS:
            try:
                report = await self.report(REPORT_DAYS)
                self._summary = {
                    key: report[key]
                    for key in (
                        "window_days",
                        "turns",
                        "reply",
                        "wait",
                        "shadowed",
                        "live",
                        "reply_rate",
                        "latency_ms",
                    )
                }
                self._summary_at = now
            except Exception:
                logger.exception("[life] rollout summary failed")
                self._summary = {"window_days": REPORT_DAYS, "turns": 0}
                self._summary_at = now
        return {
            "enabled": self._started,
            "shadow": bool(self.config.shadow),
            "rollout": {
                "mode": self.life_config.scope.mode,
                "percent": float(self.life_config.scope.rollout_percent),
                "salt_set": bool(self.life_config.scope.rollout_salt),
            },
            "window": dict(self._summary),
            "retention_days": int(self.config.decision_retention_days),
            "counters": dict(self._counters),
        }
