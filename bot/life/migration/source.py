"""Read-only view of a MaiBot deployment (S7b).

MaiBot 1.4 keeps the chat history, people, expressions, slang and stickers in a
single SQLite database (``data/MaiBot.db``) plus the ``data/emoji`` file tree.
The migration opens that database with ``mode=ro`` so a running or archived
deployment can be inspected without any risk of mutation.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PLATFORM_ALIASES = {
    "qq": "aiocqhttp",
    "aiocqhttp": "aiocqhttp",
    "onebot": "aiocqhttp",
    "napcat": "aiocqhttp",
    "llonebot": "aiocqhttp",
    "telegram": "telegram",
    "discord": "discord",
    "lark": "lark",
    "feishu": "lark",
    "wecom": "wecom",
    "wechat": "wecom",
    "webchat": "webchat",
}
"""MaiBot platform ids mapped to the ``platform_id`` of this bot."""

_REQUIRED_TABLES = {
    "messages": ("mai_messages",),
    "people": ("person_info",),
    "expressions": ("expressions",),
    "jargon": ("jargons",),
    "emoji": ("images",),
    "memory": ("paragraphs",),
}


def to_ts(value: Any, timezone: str | None = None) -> int:
    """Convert a MaiBot timestamp-like value to Unix seconds.

    MaiBot stores naive datetimes, so the zone of the *source* deployment has to
    be supplied explicitly; without it the host's local zone is assumed.

    Args:
        value: Datetime, ISO text, integer/float seconds or milliseconds.
        timezone: IANA zone applied to naive values.

    Returns:
        Unix timestamp in seconds; 0 when the value cannot be parsed.
    """
    if value in (None, ""):
        return 0
    zone = ZoneInfo(timezone) if timezone else None
    if isinstance(value, datetime):
        if value.tzinfo is None and zone is not None:
            value = value.replace(tzinfo=zone)
        return int(value.timestamp())
    if isinstance(value, int | float):
        number = float(value)
        if number > 10**11:  # milliseconds
            number /= 1000.0
        return int(number)
    text = str(value).strip()
    if text.isdigit():
        return to_ts(int(text), timezone)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None and zone is not None:
            parsed = parsed.replace(tzinfo=zone)
        return int(parsed.timestamp())
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text[:19], pattern)
        except ValueError:
            continue
        if zone is not None:
            return int(parsed.replace(tzinfo=zone).timestamp())
        return int(time.mktime(parsed.timetuple()))
    return 0


def map_platform(value: str | None) -> str | None:
    """Map a MaiBot platform id to this bot's platform id.

    Args:
        value: Raw platform value from the MaiBot database.

    Returns:
        Canonical platform id, or None when the platform is unknown.
    """
    if not value:
        return None
    raw = str(value).strip().lower()
    return PLATFORM_ALIASES.get(raw)


def build_session_key(
    platform: str | None,
    group_id: str | None = None,
    user_id: str | None = None,
) -> str | None:
    """Build the ``unified_msg_origin`` of a MaiBot conversation.

    Args:
        platform: Raw MaiBot platform id.
        group_id: Group id for a group chat.
        user_id: User id for a private chat.

    Returns:
        Session key such as ``aiocqhttp:GroupMessage:123``, or None when the
        platform is unknown or neither id is present.
    """
    canonical = map_platform(platform)
    if canonical is None:
        return None
    if group_id:
        return f"{canonical}:GroupMessage:{group_id}"
    if user_id:
        return f"{canonical}:FriendMessage:{user_id}"
    return None


@dataclass(slots=True)
class MaiBotSource:
    """One MaiBot deployment on disk."""

    root: Path
    db_path: Path

    @classmethod
    def open(cls, path: str | Path) -> MaiBotSource:
        """Resolve an instance root, data directory or database file.

        Args:
            path: MaiBot instance root, ``data`` directory or ``MaiBot.db``.

        Returns:
            Source pointing at the database.

        Raises:
            FileNotFoundError: When no ``MaiBot.db`` can be found.
        """
        target = Path(path).expanduser().resolve()
        if not target.exists():
            raise FileNotFoundError(f"MaiBot source not found: {target}")
        if target.is_dir():
            root = target
            db_path = cls._find_db(root)
        else:
            db_path = target
            root = (
                target.parent.parent if target.parent.name == "data" else target.parent
            )
        if not db_path.is_file():
            raise FileNotFoundError(f"MaiBot database not found under {target}")
        return cls(root=root, db_path=db_path)

    @staticmethod
    def _find_db(root: Path) -> Path:
        """Return the database path below an instance root.

        Args:
            root: Instance or ``data`` directory.

        Returns:
            Path of ``MaiBot.db`` (missing file is reported by the caller).
        """
        if root.name == "data" and (root / "MaiBot.db").is_file():
            return root / "MaiBot.db"
        for candidate in (root / "data" / "MaiBot.db", root / "MaiBot.db"):
            if candidate.is_file():
                return candidate
        return root / "data" / "MaiBot.db"

    def connect(self) -> sqlite3.Connection:
        """Open the database read-only.

        Returns:
            Connection with ``sqlite3.Row`` rows.
        """
        connection = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def table_names(connection: sqlite3.Connection) -> set[str]:
        """List the tables of an open database.

        Args:
            connection: Open source connection.

        Returns:
            Set of table names.
        """
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    def missing_tables(
        self,
        connection: sqlite3.Connection,
        datasets: tuple[str, ...] | list[str],
    ) -> list[str]:
        """Return the required source tables that are absent.

        Args:
            connection: Open source connection.
            datasets: Datasets about to be imported.

        Returns:
            Missing table names, empty when the source is complete.
        """
        tables = self.table_names(connection)
        missing: list[str] = []
        for dataset in datasets:
            for table in _REQUIRED_TABLES.get(dataset, ()):
                if table not in tables:
                    missing.append(table)
        return missing

    def session_index(
        self, connection: sqlite3.Connection
    ) -> dict[str, dict[str, Any]]:
        """Return ``session_id`` to conversation metadata.

        Args:
            connection: Open source connection.

        Returns:
            Mapping used to resolve expressions and slang to a session. Empty
            when the deployment predates the ``chat_sessions`` table.
        """
        if "chat_sessions" not in self.table_names(connection):
            return {}
        index: dict[str, dict[str, Any]] = {}
        for row in connection.execute("SELECT * FROM chat_sessions"):
            session_id = str(row["session_id"] or "")
            if not session_id:
                continue
            index[session_id] = dict(row)
        return index

    @staticmethod
    def bot_accounts(connection: sqlite3.Connection) -> set[tuple[str, str]]:
        """Return ``(platform, account_id)`` pairs that belong to the bot.

        Args:
            connection: Open source connection.

        Returns:
            Pairs excluded from the archive; empty when the table is absent.
        """
        tables = MaiBotSource.table_names(connection)
        if "bot_platform_accounts" not in tables:
            return set()
        return {
            (str(row["platform"] or ""), str(row["account_id"] or ""))
            for row in connection.execute("SELECT * FROM bot_platform_accounts")
        }

    def memory_db(self, override: str | Path | None = None) -> Path | None:
        """Locate the A_memorix ``metadata.db``.

        Args:
            override: Explicit database file or data directory.

        Returns:
            Database path when it exists, otherwise None.
        """
        candidates: list[Path] = []
        if override:
            target = Path(override).expanduser().resolve()
            candidates.append(target / "metadata.db" if target.is_dir() else target)
        candidates.extend(
            (
                self.root / "data" / "a-memorix" / "metadata.db",
                self.root / "a-memorix" / "metadata.db",
            )
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None
