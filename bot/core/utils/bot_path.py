"""Centralized LKMBot path helpers.

Project path:
- Fixed to the source tree location.

Root path:
- Defaults to the current working directory.
- Can be overridden with the ``BOT_ROOT`` environment variable.

Data subdirectories:
- Most runtime data lives under ``<root>/data``.
- A few tool-runtime files intentionally live under the system temporary
  directory as ``.bot``.
"""

import os
import tempfile
from pathlib import Path

from bot.core.utils.runtime_env import is_packaged_desktop_runtime


def get_bot_path() -> str:
    """Return the LKMBot project source path."""
    return os.path.realpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../"),
    )


def get_bot_root() -> str:
    """Return the LKMBot root directory."""
    if path := os.environ.get("BOT_ROOT"):
        return os.path.realpath(path)
    if is_packaged_desktop_runtime():
        return os.path.realpath(os.path.join(os.path.expanduser("~"), ".bot"))
    return os.path.realpath(os.getcwd())


def get_bot_data_path() -> str:
    """Return the LKMBot data directory path."""
    return os.path.realpath(os.path.join(get_bot_root(), "data"))


def get_bot_config_path() -> str:
    """Return the LKMBot config directory path."""
    return os.path.realpath(os.path.join(get_bot_data_path(), "config"))


def get_bot_plugin_path() -> str:
    """Return the LKMBot plugin directory path."""
    return os.path.realpath(os.path.join(get_bot_data_path(), "plugins"))


def get_bot_builtin_plugin_path() -> str:
    """Return the LKMBot built-in plugin directory path."""
    return os.path.realpath(os.path.join(get_bot_path(), "bot", "builtin_stars"))


def get_bot_plugin_data_path() -> str:
    """Return the LKMBot plugin data directory path."""
    return os.path.realpath(os.path.join(get_bot_data_path(), "plugin_data"))


def get_bot_t2i_templates_path() -> str:
    """Return the LKMBot T2I templates directory path."""
    return os.path.realpath(os.path.join(get_bot_data_path(), "t2i_templates"))


def get_bot_webchat_path() -> str:
    """Return the LKMBot WebChat data directory path."""
    return os.path.realpath(os.path.join(get_bot_data_path(), "webchat"))


def get_bot_temp_path() -> str:
    """Return the LKMBot temporary data directory path."""
    return os.path.realpath(os.path.join(get_bot_data_path(), "temp"))


def get_bot_skills_path() -> str:
    """Return the LKMBot skills directory path."""
    return os.path.realpath(os.path.join(get_bot_data_path(), "skills"))


def get_bot_workspaces_path() -> str:
    """Return the LKMBot workspaces directory path."""
    return os.path.realpath(os.path.join(get_bot_data_path(), "workspaces"))


def get_bot_system_tmp_path() -> str:
    """Return the shared system temporary directory used by local tools.

    The directory is the ``.bot`` entry of the system temporary
    directory. When the system temporary directory is unusable, Python falls
    back to the current working directory, which is where ``bot init``
    leaves the ``.bot`` marker file. In that case a sibling directory is
    used so the marker file is never shadowed by a directory.

    Returns:
        Absolute path of the shared system temporary directory.
    """
    system_tmp = Path(tempfile.gettempdir())
    candidate = system_tmp / ".bot"
    if candidate.is_file():
        candidate = system_tmp / ".bot-tmp"
    return os.path.realpath(str(candidate))


def get_bot_site_packages_path() -> str:
    """Return the LKMBot third-party site-packages directory path."""
    return os.path.realpath(os.path.join(get_bot_data_path(), "site-packages"))


def get_bot_knowledge_base_path() -> str:
    """Return the LKMBot knowledge base root path."""
    return os.path.realpath(os.path.join(get_bot_data_path(), "knowledge_base"))


def get_bot_backups_path() -> str:
    """Return the LKMBot backups directory path."""
    return os.path.realpath(os.path.join(get_bot_data_path(), "backups"))
