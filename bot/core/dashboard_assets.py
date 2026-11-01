"""Static panel asset discovery.

The panel frontend is built and served by the community website (Astro serves
``/bot/``), so LKMBot no longer ships, downloads, or version-checks a Dashboard
dist. This module only keeps the optional static fallback: an explicit
``--webui-dir`` argument or a prebuilt panel dropped into ``data/dist`` is still
served by the bot process itself.
"""

from __future__ import annotations

from pathlib import Path

from bot.core import logger
from bot.core.utils.bot_path import get_bot_data_path

__all__ = ["get_dashboard_version", "resolve_dashboard_dist"]


def _read_dashboard_version(dist_dir: str | Path) -> str | None:
    """Read the version declared by a static panel directory.

    Args:
        dist_dir: Static panel directory path.

    Returns:
        Version from ``assets/version``, or None when it cannot be read.
    """
    version_file = Path(dist_dir) / "assets" / "version"
    try:
        if version_file.exists():
            return version_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Failed to read WebUI version from %s: %s", version_file, exc)
    return None


def resolve_dashboard_dist(webui_dir: str | Path | None = None) -> Path | None:
    """Select the static panel directory to serve, if any.

    Args:
        webui_dir: Optional explicitly configured static panel directory.

    Returns:
        The explicit directory when it exists, otherwise a structurally usable
        ``data/dist`` fallback, otherwise None.
    """
    explicit_dist = Path(webui_dir).absolute() if webui_dir else None
    if explicit_dist is not None and explicit_dist.exists():
        return explicit_dist

    user_dist = Path(get_bot_data_path()) / "dist"
    if (user_dist / "index.html").is_file():
        logger.info("Serving static panel files from %s", user_dist)
        return user_dist.absolute()
    if user_dist.exists():
        logger.warning(
            "Ignoring %s because index.html is missing; the panel is served by "
            "the website at /bot/.",
            user_dist,
        )
    return None


async def get_dashboard_version(
    dist_dir: str | Path | None = None,
) -> str | None:
    """Return the version of a static panel directory, when one is present.

    Args:
        dist_dir: Optional static panel directory. When omitted, ``data/dist``
            is inspected.

    Returns:
        The version declared by the directory, or None when there is none.
    """
    if dist_dir is not None:
        return _read_dashboard_version(dist_dir)
    user_dist = Path(get_bot_data_path()) / "dist"
    if user_dist.exists():
        return _read_dashboard_version(user_dist)
    return None
