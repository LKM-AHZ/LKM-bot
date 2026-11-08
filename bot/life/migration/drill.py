"""Rollback drill for the MaiBot migration (S7c).

A rollback that has never been executed is a guess. The drill therefore runs the
real import and the real rollback against a *copy* of the production database:

1. generate a synthetic MaiBot source (messages, one person, expression, slang,
   sticker and A_memorix paragraph);
2. import it and time the run;
3. insert one "live traffic" row after the import, so the rollback has to prove
   it only removes the migrated rows;
4. roll the batch back and time it;
5. verify that every imported row is gone, the live row survived and the table
   counts match the baseline again.

The measured rollback time is the number behind the "rollback < 10 minutes"
exit gate. The caller owns the database copy: the drill never touches the
production file.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import delete, func, select
from sqlmodel import col

from bot.core.db import BaseDatabase
from bot.core.db.po import PlatformMessageHistory
from bot.life.emoji.models import LifeEmoji
from bot.life.expression.models import LifeExpression
from bot.life.jargon.models import LifeJargon
from bot.life.memory.models import LifeMemory
from bot.life.migration.service import MigrationService
from bot.life.person.models import LifePerson, LifePersonFact
from bot.life.rollout.models import LifeTurnDecision

ROLLBACK_LIMIT_SECONDS = 600.0
"""The exit gate: a rollback must finish inside ten minutes."""

DEFAULT_MESSAGES = 200
"""Synthetic messages per drill; enough to measure without taking minutes."""

_DRILL_MODELS = (
    PlatformMessageHistory,
    LifePerson,
    LifePersonFact,
    LifeExpression,
    LifeJargon,
    LifeEmoji,
    LifeMemory,
    LifeTurnDecision,
)

_JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


def build_synthetic_source(
    root: Path,
    messages: int = DEFAULT_MESSAGES,
    group_id: str = "drill-group",
) -> Path:
    """Generate a minimal MaiBot deployment for the drill.

    Args:
        root: Directory to create; it becomes the MaiBot instance root.
        messages: Number of chat rows to generate.
        group_id: Group id used by the generated messages and person card.

    Returns:
        The instance root, usable with :class:`MaiBotSource`.
    """
    data = root / "data"
    (data / "emoji").mkdir(parents=True, exist_ok=True)
    (data / "a-memorix").mkdir(parents=True, exist_ok=True)
    (data / "emoji" / "drill.jpg").write_bytes(_JPEG)

    connection = sqlite3.connect(data / "MaiBot.db")
    connection.executescript(
        """
        CREATE TABLE mai_messages (
            id INTEGER PRIMARY KEY,
            message_id TEXT,
            timestamp TEXT,
            platform TEXT,
            user_id TEXT,
            user_nickname TEXT,
            user_cardname TEXT,
            group_id TEXT,
            session_id TEXT,
            reply_to TEXT,
            is_at INTEGER DEFAULT 0,
            is_picture INTEGER DEFAULT 0,
            is_emoji INTEGER DEFAULT 0,
            is_command INTEGER DEFAULT 0,
            processed_plain_text TEXT
        );
        CREATE TABLE person_info (
            id INTEGER PRIMARY KEY,
            person_id TEXT,
            person_name TEXT,
            platform TEXT,
            user_id TEXT,
            user_nickname TEXT,
            group_cardname TEXT,
            memory_points TEXT,
            know_counts INTEGER DEFAULT 0,
            last_known_time TEXT
        );
        CREATE TABLE expressions (
            id INTEGER PRIMARY KEY,
            situation TEXT,
            style TEXT,
            content_list TEXT,
            count INTEGER DEFAULT 0,
            last_active_time TEXT,
            create_time TEXT,
            session_id TEXT
        );
        CREATE TABLE jargons (
            id INTEGER PRIMARY KEY,
            content TEXT,
            meaning TEXT,
            session_id_dict TEXT,
            count INTEGER DEFAULT 0,
            is_jargon INTEGER DEFAULT 0,
            is_complete INTEGER DEFAULT 0,
            is_global INTEGER DEFAULT 0,
            created_by TEXT,
            created_timestamp TEXT,
            updated_timestamp TEXT,
            evidence_messages TEXT
        );
        CREATE TABLE images (
            id INTEGER PRIMARY KEY,
            image_hash TEXT,
            description TEXT,
            full_path TEXT,
            image_type TEXT,
            query_count INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            no_file_flag INTEGER DEFAULT 0,
            last_used_time TEXT
        );
        CREATE TABLE chat_sessions (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            platform TEXT,
            user_id TEXT,
            group_id TEXT
        );
        CREATE TABLE bot_platform_accounts (
            id INTEGER PRIMARY KEY,
            platform TEXT,
            account_id TEXT,
            disabled INTEGER DEFAULT 0
        );
        """
    )
    session_id = f"qq_group_{group_id}"
    connection.executemany(
        "INSERT INTO mai_messages (message_id, timestamp, platform, user_id, "
        "user_nickname, user_cardname, group_id, session_id, processed_plain_text) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                f"drill-{index}",
                "2026-11-01 09:00:00",
                "qq",
                "u1",
                "阿一",
                "一群的一号",
                group_id,
                session_id,
                f"drill message {index}",
            )
            for index in range(1, max(int(messages), 0) + 1)
        ],
    )
    connection.execute(
        "INSERT INTO person_info (person_id, person_name, platform, user_id, "
        "user_nickname, group_cardname, memory_points, know_counts) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            "p-drill",
            "演练用户",
            "qq",
            "u1",
            "阿一",
            json.dumps([{"group_id": group_id, "group_cardname": "一群的一号"}]),
            json.dumps(["喜好:咖啡:0.9"]),
            3,
        ),
    )
    connection.execute(
        "INSERT INTO expressions (situation, style, content_list, count, "
        "session_id) VALUES (?,?,?,?,?)",
        ("被叫到", "卖萌", json.dumps(["好的呀"]), 2, session_id),
    )
    connection.execute(
        "INSERT INTO jargons (content, meaning, session_id_dict, count, "
        "is_jargon, is_global, created_by) VALUES (?,?,?,?,?,?,?)",
        ("吃瓜", "围观", json.dumps({session_id: 2}), 4, 1, 0, "AI"),
    )
    connection.execute(
        "INSERT INTO images (description, full_path, image_type, query_count) "
        "VALUES (?,?,?,?)",
        ("开心,点赞", "data/emoji/drill.jpg", "emoji", 1),
    )
    connection.execute(
        "INSERT INTO chat_sessions (session_id, platform, group_id) VALUES (?,?,?)",
        (session_id, "qq", group_id),
    )
    connection.commit()
    connection.close()

    memory = sqlite3.connect(data / "a-memorix" / "metadata.db")
    memory.executescript(
        """
        CREATE TABLE paragraphs (
            hash TEXT PRIMARY KEY,
            content TEXT,
            knowledge_type TEXT,
            created_at REAL,
            event_time REAL,
            is_deleted INTEGER DEFAULT 0
        );
        """
    )
    memory.execute(
        "INSERT INTO paragraphs (hash, content, knowledge_type, created_at, "
        "event_time, is_deleted) VALUES (?,?,?,?,?,?)",
        ("drill-memory", "演练用的长期记忆", "note", time.time(), time.time(), 0),
    )
    memory.commit()
    memory.close()
    return root


async def run_drill(
    db_helper: BaseDatabase,
    source_path: str | Path,
    umos: Sequence[str],
    data_dir: str | Path,
) -> dict[str, Any]:
    """Import and roll back one synthetic source on the given database.

    Args:
        db_helper: Database helper; the caller must point it at a copy.
        source_path: Synthetic MaiBot root from :func:`build_synthetic_source`.
        umos: Target sessions for global data.
        data_dir: Life data directory used for copied sticker files.

    Returns:
        Drill record with the pass flag, timings and row counts.

    Raises:
        RuntimeError: When the drill import wrote no rows.
    """
    migration = MigrationService(db_helper, data_dir)
    await migration.ensure_schema()
    baseline = await _counts(db_helper)
    imported_started = time.perf_counter()
    report = await migration.run(source_path, umos=list(umos), dry_run=False)
    import_seconds = time.perf_counter() - imported_started
    imported_rows = int(report["totals"]["written"])
    if imported_rows <= 0:
        raise RuntimeError("drill import wrote no rows; check the synthetic source")
    sentinel = await _insert_sentinel(db_helper, umos[0])
    rollback_started = time.perf_counter()
    rolled = await migration.rollback(str(report["batch_id"]))
    rollback_seconds = time.perf_counter() - rollback_started
    sentinel_survived = await _message_exists(db_helper, sentinel)
    await _delete_sentinel(db_helper, sentinel)
    cleaned = await _counts(db_helper)
    deleted_rows = sum(int(count) for count in rolled["deleted"].values())
    passed = (
        rolled["status"] == "rolled_back"
        and deleted_rows >= imported_rows
        and sentinel_survived
        and cleaned == baseline
        and rollback_seconds < ROLLBACK_LIMIT_SECONDS
    )
    return {
        "passed": passed,
        "batch_id": report["batch_id"],
        "imported_rows": imported_rows,
        "deleted_rows": deleted_rows,
        "import_seconds": round(import_seconds, 3),
        "rollback_seconds": round(rollback_seconds, 3),
        "limit_seconds": ROLLBACK_LIMIT_SECONDS,
        "sentinel_survived": sentinel_survived,
        "baseline": baseline,
        "after_cleanup": cleaned,
        "deleted": rolled["deleted"],
        "datasets": report["datasets"],
    }


async def _counts(db_helper: BaseDatabase) -> dict[str, int]:
    """Count the rows of the tables the drill touches.

    Args:
        db_helper: Database helper.

    Returns:
        Table name to row count.
    """
    counts: dict[str, int] = {}
    async with db_helper.get_db() as session:
        for model in _DRILL_MODELS:
            table = model.__table__  # ty: ignore[unresolved-attribute]  # SQLModel adds __table__ at runtime
            value = (
                await session.execute(select(func.count()).select_from(table))
            ).scalar_one()
            counts[str(getattr(model, "__tablename__"))] = int(value or 0)
    return counts


async def _insert_sentinel(db_helper: BaseDatabase, umo: str) -> str:
    """Insert one row that imitates live traffic after the import.

    Args:
        db_helper: Database helper.
        umo: Session the row belongs to.

    Returns:
        Message id of the sentinel row.
    """
    message_id = f"drill-live-{uuid.uuid4().hex[:12]}"
    async with db_helper.get_db() as session:
        session.add(
            PlatformMessageHistory(
                platform_id="aiocqhttp",
                user_id=umo,
                content={
                    "type": "user",
                    "message": [],
                    "life": {
                        "message_id": message_id,
                        "ts": int(time.time()),
                        "plain_text": "live traffic",
                    },
                },
                source_message_id=message_id,
            )
        )
        await session.commit()
    return message_id


async def _message_exists(db_helper: BaseDatabase, message_id: str) -> bool:
    """Return whether a message row still exists.

    Args:
        db_helper: Database helper.
        message_id: Message id to look for.

    Returns:
        True when the row is present.
    """
    async with db_helper.get_db() as session:
        row = (
            (
                await session.execute(
                    select(PlatformMessageHistory).where(
                        col(PlatformMessageHistory.source_message_id) == message_id
                    )
                )
            )
            .scalars()
            .first()
        )
    return row is not None


async def _delete_sentinel(db_helper: BaseDatabase, message_id: str) -> None:
    """Delete the sentinel row so the drill leaves no trace.

    Args:
        db_helper: Database helper.
        message_id: Message id to delete.
    """
    async with db_helper.get_db() as session:
        await session.execute(
            delete(PlatformMessageHistory).where(
                col(PlatformMessageHistory.source_message_id) == message_id
            )
        )
        await session.commit()
