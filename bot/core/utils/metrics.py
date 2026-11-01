"""Local-only usage counters.

The upstream project batched anonymous counters and POSTed them to a third-party endpoint
(hostname, Python version, OS, installation id). This fork keeps only the local half: per-platform
message counters written into the bot's own SQLite database and rendered on the dashboard
statistics page. Nothing here performs network I/O — the bot must not phone home by default.
"""

from __future__ import annotations

from typing import Any

from bot.core import db_helper, logger


class Metric:
    """Records usage counters locally; never reports them anywhere."""

    @staticmethod
    async def _save_platform_stats(kwargs: dict[str, Any]) -> None:
        """Persist one per-platform counter row.

        Args:
            kwargs: Metric fields supplied by the call site. ``adapter_name`` selects the platform
                row; everything else is informational and intentionally ignored.
        """
        try:
            if "adapter_name" in kwargs:
                await db_helper.insert_platform_stats(
                    platform_id=kwargs["adapter_name"],
                    platform_type=kwargs.get("adapter_type", "unknown"),
                )
        except Exception as e:
            logger.error(f"保存指标到数据库失败: {e}")

    @staticmethod
    async def upload(**kwargs: Any) -> None:
        """Record a usage counter in the local database.

        Call sites pass ``adapter_name`` / ``adapter_type``; the occasional ``et`` / ``repo`` fields
        (plugin installs) have no local table and are therefore dropped.

        Args:
            **kwargs: Metric fields from the call site.
        """
        await Metric._save_platform_stats(kwargs)
