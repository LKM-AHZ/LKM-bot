from typing import TYPE_CHECKING

from .basic import (
    check_bot_root,
    check_dashboard,
    get_bot_root,
)
from .plugin import (
    PluginStatus,
    build_plug_list,
    download_repository,
    install_local_plugin,
    manage_plugin,
)

if TYPE_CHECKING:
    from bot.core.utils.version_comparator import VersionComparator


def __getattr__(name: str):
    # Re-exported lazily: importing bot.core is heavy and must not happen
    # when the CLI is merely starting up.
    if name == "VersionComparator":
        from bot.core.utils.version_comparator import VersionComparator

        return VersionComparator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "PluginStatus",
    "VersionComparator",
    "build_plug_list",
    "check_bot_root",
    "check_dashboard",
    "download_repository",
    "get_bot_root",
    "install_local_plugin",
    "manage_plugin",
]
