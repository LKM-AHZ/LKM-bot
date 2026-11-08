"""Offline migration from a MaiBot deployment into the Life layer (S7b).

The service reads MaiBot read-only, converts six datasets and writes only Life
tables:

- ``messages``: ``mai_messages`` to the shared message archive (bot accounts and
  ``is_notify`` system notices skipped),
- ``people``: ``person_info`` plus its ``memory_points`` to ``life_person`` and
  ``life_person_fact`` (rows the source never confirmed are skipped unless
  ``include_unknown`` is set),
- ``expressions``: ``expressions`` to ``life_expression`` (``checked`` decides
  whether a row starts ``active`` or ``pending``),
- ``jargon``: ``jargons`` to ``life_jargon``,
- ``emoji``: ``images`` plus ``data/emoji`` files to ``life_emoji``,
- ``memory``: A_memorix ``paragraphs`` to global ``life_memory`` rows.

Every write is an ``INSERT ... ON CONFLICT DO NOTHING``, so re-running a batch
only fills gaps; conflicts are reported as ``skipped_reasons["duplicate"]`` so
the counters always add up. A real run records the row-id watermark of each
touched table; :meth:`MigrationService.rollback` deletes exactly the rows
between the watermarks, plus the sticker files they referenced. Use a dry run
first: it reads and converts everything but writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import SQLModel, col

from bot import logger
from bot.core.db import BaseDatabase
from bot.core.db.po import PlatformMessageHistory
from bot.life.common import ensure_tables
from bot.life.emoji.models import LifeEmoji
from bot.life.expression.models import LifeExpression
from bot.life.expression.store import expression_checksum
from bot.life.jargon.models import LifeJargon
from bot.life.jargon.store import term_checksum
from bot.life.memory.importer import MemoryImporter
from bot.life.memory.models import LifeMemory
from bot.life.memory.store import MemoryStore
from bot.life.migration import MIGRATION_DATASETS
from bot.life.migration.models import LifeMigrationRun
from bot.life.migration.source import (
    MaiBotSource,
    build_session_key,
    map_platform,
    to_ts,
)
from bot.life.person.models import LifePerson, LifePersonFact
from bot.life.person.names import clean_name, normalize_name
from bot.life.person.store import fact_hash
from bot.life.store import LifeStore

_INSERT_BATCH = 500
"""Rows per write transaction; messages are streamed in chunks of this size."""

_MEMORY_BATCH = 200
"""Paragraphs per A_memorix conversion batch."""

_DATASET_MODELS: dict[str, tuple[type[SQLModel], ...]] = {
    "messages": (),
    "people": (LifePerson, LifePersonFact),
    "expressions": (LifeExpression,),
    "jargon": (LifeJargon,),
    "emoji": (LifeEmoji,),
    "memory": (LifeMemory,),
}

_ALL_MODELS: tuple[type[SQLModel], ...] = (
    LifePerson,
    LifePersonFact,
    LifeExpression,
    LifeJargon,
    LifeEmoji,
    LifeMemory,
    LifeMigrationRun,
)

_MEMORY_KINDS = {
    # A_memorix writes the ``KnowledgeType`` values below; ``fact``/``episode``
    # are kept for older exports that already used the canonical names.
    "factual": "fact",
    "narrative": "episode",
    "structured": "note",
    "quote": "note",
    "mixed": "note",
    "fact": "fact",
    "episode": "episode",
    "summary": "summary",
    "note": "note",
}


def _value(record: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Read a column that older MaiBot versions may not have.

    Args:
        record: Source row.
        key: Column name.
        default: Value returned when the column is absent.

    Returns:
        Column value or the default.
    """
    try:
        return record[key]
    except (IndexError, KeyError):
        return default


def _json_value(raw: Any, fallback: Any) -> Any:
    """Parse a JSON column, tolerating already-decoded values.

    Args:
        raw: Column value.
        fallback: Value used when parsing fails.

    Returns:
        Decoded value or the fallback.
    """
    if not raw:
        return fallback
    if isinstance(raw, list | dict):
        return raw
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return fallback


def _counters() -> dict[str, Any]:
    """Return an empty counter block for one dataset.

    Returns:
        Mutable counters with seen/ready/written/skipped/truncated/errors.
    """
    return {
        "seen": 0,
        "ready": 0,
        "written": 0,
        "skipped": 0,
        "truncated": 0,
        "errors": 0,
        "skipped_reasons": {},
    }


def _sniff_image(data: bytes) -> tuple[str, str]:
    """Return the image type of a payload.

    Args:
        data: File bytes.

    Returns:
        ``(mime, suffix)`` or ``("", "")`` when the format is unsupported.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return "", ""


def _memory_kind(raw: Any) -> str:
    """Map an A_memorix knowledge type to a Life memory kind.

    A_memorix stores ``structured`` / ``narrative`` / ``factual`` / ``quote`` /
    ``mixed``; factual memories become ``fact`` rows so the person-fact and
    recall paths see them, narrative ones become ``episode`` and the rest fall
    back to ``note``.

    Args:
        raw: ``knowledge_type`` column value.

    Returns:
        One of summary/fact/episode/note; ``note`` for unknown values.
    """
    return _MEMORY_KINDS.get(str(raw or "").strip().lower(), "note")


class MigrationService:
    """Convert a MaiBot deployment into Life tables."""

    def __init__(
        self,
        db_helper: BaseDatabase,
        data_dir: str | Path,
        timezone: str | None = None,
    ) -> None:
        """Initialize the service.

        Args:
            db_helper: Core async database helper.
            data_dir: Life data directory; sticker files are copied below it.
            timezone: IANA zone of the source deployment; naive timestamps are
                read in this zone instead of the host's local one.

        Raises:
            ValueError: When ``timezone`` is not a known IANA zone.
        """
        self.db = db_helper
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.timezone = timezone
        if timezone is not None:
            try:
                ZoneInfo(timezone)  # fail fast on an unknown zone
            except (KeyError, ValueError) as exc:
                raise ValueError(f"unknown timezone: {timezone}") from exc
        self.memory_store = MemoryStore(db_helper)
        self.importer = MemoryImporter(self.memory_store)
        self.message_store = LifeStore(db_helper)

    async def ensure_schema(self) -> None:
        """Create every table the migration can touch."""
        await ensure_tables(self.db, *_ALL_MODELS)

    async def preflight(
        self,
        source_path: str | Path,
        *,
        datasets: Sequence[str] | None = None,
        memory_source: str | Path | None = None,
    ) -> dict[str, Any]:
        """Inspect a source without writing anything.

        Args:
            source_path: MaiBot instance root, data directory or database.
            datasets: Datasets to inspect; defaults to all.
            memory_source: Explicit A_memorix directory or database.

        Returns:
            Source metadata, per-dataset row counts and warnings.
        """
        selected = self._select(datasets)
        source = MaiBotSource.open(source_path)
        connection = source.connect()
        try:
            tables = source.table_names(connection)
            missing = source.missing_tables(
                connection,
                [dataset for dataset in selected if dataset != "memory"],
            )
            memory_db = (
                source.memory_db(memory_source) if "memory" in selected else None
            )
            counts: dict[str, int] = {}
            for dataset in selected:
                count = 0
                if dataset == "memory" and memory_db is not None:
                    count = self._count_paragraphs(memory_db)
                else:
                    for table in self._dataset_tables(dataset):
                        if table in tables:
                            count += int(
                                connection.execute(
                                    f'SELECT COUNT(*) FROM "{table}"'
                                ).fetchone()[0]
                                or 0
                            )
                counts[dataset] = count
            warnings: list[str] = []
            if missing:
                warnings.append("missing table(s): " + ", ".join(sorted(missing)))
            if "memory" in selected and memory_db is None:
                warnings.append("A_memorix memory database not found")
            if "emoji" in selected:
                emoji_dir = source.root / "data" / "emoji"
                if not emoji_dir.is_dir():
                    warnings.append(f"emoji directory not found: {emoji_dir}")
            session_index = source.session_index(connection)
            return {
                "source": {
                    "root": str(source.root),
                    "db": str(source.db_path),
                },
                "tables": sorted(tables),
                "datasets": [
                    {
                        "name": dataset,
                        "available": all(
                            table in tables
                            for table in self._dataset_tables(dataset)
                            if dataset != "memory"
                        )
                        and (
                            dataset != "memory"
                            or (
                                memory_db is not None
                                and self._paragraphs_available(memory_db)
                            )
                        ),
                        "rows": counts.get(dataset, 0),
                    }
                    for dataset in selected
                ],
                "missing_tables": sorted(missing),
                "memory_db": str(memory_db) if memory_db else None,
                "sessions": {
                    "mapped": len(session_index),
                    "unmapped_platforms": self._unmapped_platforms(connection),
                },
                "warnings": warnings,
            }
        finally:
            connection.close()

    async def run(
        self,
        source_path: str | Path,
        *,
        umos: Sequence[str] = (),
        datasets: Sequence[str] | None = None,
        dry_run: bool = True,
        since_days: int = 0,
        limit: int = 0,
        memory_source: str | Path | None = None,
        include_unknown: bool = False,
    ) -> dict[str, Any]:
        """Import one MaiBot deployment.

        Args:
            source_path: MaiBot instance root, data directory or database.
            umos: Target sessions for global data (stickers, global slang,
                expressions without a session, people without group cards).
            datasets: Datasets to import; defaults to all.
            dry_run: Read and convert without writing; default True.
            since_days: Only import messages newer than this many days; 0 keeps
                the whole history.
            limit: Maximum messages to import; 0 keeps all.
            memory_source: Explicit A_memorix directory or database.
            include_unknown: Also import people the source never confirmed.

        Returns:
            Report with per-dataset counters, warnings and the rollback
            watermark.

        Raises:
            ValueError: When a dataset is unknown, a required table is missing
                or a real sticker import has no target session.
        """
        selected = self._select(datasets)
        targets = [str(umo) for umo in umos if umo]
        warnings: list[str] = []
        if "emoji" in selected and not targets:
            if datasets is not None or not dry_run:
                raise ValueError("importing emoji needs at least one --umo target")
            # The panel preview uses the default dataset set; drop stickers
            # instead of failing the whole inspection.
            selected = tuple(name for name in selected if name != "emoji")
            warnings.append("emoji skipped: no target session (--umo)")
        source = MaiBotSource.open(source_path)
        started = int(time.time())
        counts = {dataset: _counters() for dataset in selected}
        marks: dict[str, dict[str, int]] = {}
        batch_id = uuid.uuid4().hex[:16]
        connection = source.connect()
        try:
            missing = source.missing_tables(
                connection,
                [dataset for dataset in selected if dataset != "memory"],
            )
            if missing:
                raise ValueError(
                    "MaiBot database is missing table(s): " + ", ".join(sorted(missing))
                )
            session_index = source.session_index(connection)
            bot_accounts = source.bot_accounts(connection)
            if not dry_run:
                marks = await self._watermarks(_models_for(selected))
            if "messages" in selected:
                await self._import_messages(
                    connection,
                    counts["messages"],
                    bot_accounts,
                    started - since_days * 86400 if since_days > 0 else 0,
                    max(limit, 0),
                    dry_run,
                    batch_id,
                )
            if "people" in selected:
                await self._import_people(
                    connection,
                    counts["people"],
                    targets,
                    dry_run,
                    include_unknown,
                )
            if "expressions" in selected:
                await self._import_expressions(
                    connection,
                    counts["expressions"],
                    session_index,
                    targets,
                    dry_run,
                )
            if "jargon" in selected:
                await self._import_jargon(
                    connection,
                    counts["jargon"],
                    session_index,
                    targets,
                    dry_run,
                )
            if "emoji" in selected:
                await self._import_emoji(
                    source,
                    connection,
                    counts["emoji"],
                    targets,
                    dry_run,
                )
            if "memory" in selected:
                await self._import_memory(
                    source,
                    counts["memory"],
                    memory_source,
                    dry_run,
                )
        except Exception as exc:
            if not dry_run:
                marks = await self._watermarks(_models_for(selected), marks, after=True)
                await self._save_run(
                    batch_id,
                    source,
                    selected,
                    counts,
                    marks,
                    dry_run,
                    umos=targets,
                    since_days=since_days,
                    limit=limit,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}"[:500],
                    started=started,
                    include_unknown=include_unknown,
                )
            connection.close()
            raise
        unmapped = self._unmapped_platforms(connection)
        connection.close()
        if not dry_run:
            marks = await self._watermarks(_models_for(selected), marks, after=True)
            await self._save_run(
                batch_id,
                source,
                selected,
                counts,
                marks,
                dry_run,
                umos=targets,
                since_days=since_days,
                limit=limit,
                status="completed",
                started=started,
                include_unknown=include_unknown,
            )
        return {
            "batch_id": None if dry_run else batch_id,
            "source": str(source.db_path),
            "dry_run": dry_run,
            "datasets": counts,
            "totals": self._totals(counts),
            "sessions": {
                "targets": targets,
                "source_sessions": len(session_index),
                "unmapped_platforms": unmapped,
            },
            "warnings": warnings,
            "marks": marks,
            "started_at": started,
            "finished_at": int(time.time()),
        }

    async def rollback(self, batch_id: str) -> dict[str, Any]:
        """Delete the rows written by one batch and its sticker files.

        Args:
            batch_id: Identifier returned by :meth:`run`.

        Returns:
            Deleted row counts per table plus the removed file count.

        Raises:
            ValueError: When the batch does not exist.
        """
        run = await self._get_run(batch_id)
        if run is None:
            raise ValueError(f"migration batch not found: {batch_id}")
        deleted: dict[str, int] = {}
        files = 0
        if run.status != "rolled_back":
            emoji_table = LifeEmoji.__table__  # ty: ignore[unresolved-attribute]  # SQLModel adds __table__ at runtime
            paths: set[str] = set()
            async with self.db.get_db() as session:
                span = (run.marks or {}).get("life_emoji") or {}
                if span.get("before") is not None and span.get("after") is not None:
                    rows = await session.execute(
                        select(emoji_table.c.file_path).where(
                            emoji_table.c.id > int(span["before"]),
                            emoji_table.c.id <= int(span["after"]),
                        )
                    )
                    paths = {str(path) for path in rows.scalars().all() if path}
                for table, span in (run.marks or {}).items():
                    model = _MODEL_BY_TABLE.get(table)
                    if (
                        model is None
                        or span.get("before") is None
                        or span.get("after") is None
                    ):
                        continue
                    table_obj = model.__table__  # ty: ignore[unresolved-attribute]  # SQLModel adds __table__ at runtime
                    result = await session.execute(
                        delete(table_obj).where(
                            table_obj.c.id > int(span.get("before", 0)),
                            table_obj.c.id <= int(span["after"]),
                        )
                    )
                    deleted[table] = max(
                        int(result.rowcount or 0),  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount
                        0,
                    )
                # Imported messages live in the shared archive; their watermark
                # would also match live traffic, so they are tagged by batch.
                message_result = await session.execute(
                    delete(PlatformMessageHistory).where(
                        col(PlatformMessageHistory.life_batch) == batch_id
                    )
                )
                message_deleted = max(
                    int(message_result.rowcount or 0),  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount
                    0,
                )
                if message_deleted:
                    deleted["platform_message_history"] = message_deleted
                await session.commit()
                # Files are only dropped once no surviving row references them.
                if paths:
                    remaining: set[str] = set()
                    listed = sorted(paths)
                    for start in range(0, len(listed), 200):
                        rows = await session.execute(
                            select(emoji_table.c.file_path).where(
                                emoji_table.c.file_path.in_(listed[start : start + 200])
                            )
                        )
                        remaining.update(str(path) for path in rows.scalars().all())
                    files = self._remove_files(sorted(paths - remaining))
            await self._mark_rolled_back(batch_id)
        return {
            "batch_id": batch_id,
            "status": "rolled_back",
            "deleted": deleted,
            "files": files,
        }

    @staticmethod
    def _remove_files(paths: Sequence[str]) -> int:
        """Delete sticker files and the session directory they leave empty.

        Args:
            paths: Files that no ``life_emoji`` row references anymore.

        Returns:
            Number of files removed.
        """
        removed = 0
        for raw in paths:
            path = Path(raw)
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning(f"[life] emoji rollback could not remove {path}: {exc}")
                continue
            try:
                path.parent.rmdir()
            except OSError:
                continue
        return removed

    async def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent migration batches, newest first.

        Args:
            limit: Maximum number of batches.

        Returns:
            Serialized manifest rows.
        """
        async with self.db.get_db() as session:
            rows = (
                (
                    await session.execute(
                        select(LifeMigrationRun)
                        .order_by(
                            desc(col(LifeMigrationRun.created_at)),
                            desc(col(LifeMigrationRun.id)),
                        )
                        .limit(max(limit, 1))
                    )
                )
                .scalars()
                .all()
            )
        return [self._run_payload(run) for run in rows]

    @staticmethod
    def _select(datasets: Sequence[str] | None) -> tuple[str, ...]:
        """Validate and normalize a dataset selection.

        Args:
            datasets: Requested names, or None for all.

        Returns:
            Ordered dataset names.

        Raises:
            ValueError: When a name is unknown.
        """
        selected = tuple(datasets or MIGRATION_DATASETS)
        unknown = [name for name in selected if name not in MIGRATION_DATASETS]
        if unknown:
            raise ValueError("unknown dataset(s): " + ", ".join(sorted(unknown)))
        return selected

    @staticmethod
    def _dataset_tables(dataset: str) -> tuple[str, ...]:
        """Return the source tables of one dataset.

        Args:
            dataset: Dataset name.

        Returns:
            Table names used by the dataset.
        """
        return {
            "messages": ("mai_messages",),
            "people": ("person_info",),
            "expressions": ("expressions",),
            "jargon": ("jargons",),
            "emoji": ("images",),
            "memory": ("paragraphs",),
        }.get(dataset, ())

    @staticmethod
    def _totals(counts: dict[str, dict[str, Any]]) -> dict[str, int]:
        """Sum the counters of every dataset.

        Args:
            counts: Per-dataset counters.

        Returns:
            Aggregate counters.
        """
        return {
            key: sum(int(block.get(key, 0)) for block in counts.values())
            for key in ("seen", "ready", "written", "skipped", "truncated", "errors")
        }

    @staticmethod
    def _skip(counters: dict[str, Any], reason: str) -> None:
        """Count one skipped row and its reason.

        Args:
            counters: Dataset counters.
            reason: Short machine-readable reason.
        """
        counters["skipped"] += 1
        reasons = counters["skipped_reasons"]
        reasons[reason] = int(reasons.get(reason, 0)) + 1

    @staticmethod
    def _record_writes(
        counters: dict[str, Any],
        written: int,
        duplicates: int,
    ) -> None:
        """Account an insert so written plus skipped always equals ready.

        Args:
            counters: Dataset counters.
            written: Rows actually inserted.
            duplicates: Rows dropped by the natural-key conflict.
        """
        counters["written"] += written
        if duplicates <= 0:
            return
        counters["skipped"] += duplicates
        reasons = counters["skipped_reasons"]
        reasons["duplicate"] = int(reasons.get("duplicate", 0)) + duplicates

    async def _insert(
        self, model: type[SQLModel], rows: list[dict[str, Any]]
    ) -> tuple[int, int]:
        """Insert rows, ignoring duplicates.

        Args:
            model: Target SQLModel table.
            rows: Row payloads; every row must use the same columns.

        Returns:
            ``(written, duplicates)`` so the caller can report conflicts.
        """
        if not rows:
            return 0, 0
        async with self.db.get_db() as session:
            result = await session.execute(
                sqlite_insert(model).values(rows).on_conflict_do_nothing()
            )
            await session.commit()
            written = max(int(result.rowcount or 0), 0)  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount
            return written, max(len(rows) - written, 0)

    async def _watermarks(
        self,
        models: Sequence[type[SQLModel]],
        marks: dict[str, dict[str, int]] | None = None,
        after: bool = False,
    ) -> dict[str, dict[str, int]]:
        """Record the maximum row id of every table before or after a run.

        Args:
            models: Tables touched by the run.
            marks: Existing marks when recording the ``after`` watermark.
            after: When True, fill ``after`` instead of ``before``.

        Returns:
            Updated marks.
        """
        result = dict(marks or {})
        async with self.db.get_db() as session:
            for model in models:
                table_name = str(getattr(model, "__tablename__"))
                table = model.__table__  # ty: ignore[unresolved-attribute]  # SQLModel adds __table__ at runtime
                current = int(
                    (
                        await session.execute(
                            select(func.coalesce(func.max(table.c.id), 0))
                        )
                    ).scalar_one()
                    or 0
                )
                entry = result.setdefault(table_name, {})
                entry["after" if after else "before"] = current
        return result

    async def _import_messages(
        self,
        connection: sqlite3.Connection,
        counters: dict[str, Any],
        bot_accounts: set[tuple[str, str]],
        since_ts: int,
        limit: int,
        dry_run: bool,
        batch_id: str,
    ) -> None:
        """Convert ``mai_messages`` rows in chunks.

        Args:
            connection: Open source connection.
            counters: Dataset counters.
            bot_accounts: ``(platform, account)`` pairs excluded as bot output.
            since_ts: Lower timestamp bound; 0 keeps everything.
            limit: Maximum rows to convert; 0 keeps everything.
            dry_run: Count only when True.
            batch_id: Rollback tag stored on every imported row.
        """
        rows: list[dict[str, Any]] = []
        for record in connection.execute("SELECT * FROM mai_messages ORDER BY id"):
            counters["seen"] += 1
            row, reason = self._message_row(record, bot_accounts, since_ts)
            if row is None:
                self._skip(counters, reason)
                continue
            counters["ready"] += 1
            if not dry_run:
                rows.append(row)
                if len(rows) >= _INSERT_BATCH:
                    written = await self.message_store.insert_batch(
                        rows, life_batch=batch_id
                    )
                    self._record_writes(counters, written, len(rows) - written)
                    rows = []
            if limit and counters["ready"] >= limit:
                break
        if rows and not dry_run:
            written = await self.message_store.insert_batch(rows, life_batch=batch_id)
            self._record_writes(counters, written, len(rows) - written)

    def _message_row(
        self,
        record: sqlite3.Row,
        bot_accounts: set[tuple[str, str]],
        since_ts: int,
    ) -> tuple[dict[str, Any] | None, str]:
        """Convert one MaiBot message.

        Args:
            record: ``mai_messages`` row.
            bot_accounts: Pairs excluded as bot output.
            since_ts: Lower timestamp bound; 0 keeps everything.

        Returns:
            ``(row, "")`` on success, ``(None, reason)`` when skipped.
        """
        message_id = str(_value(record, "message_id") or "").strip()
        if not message_id:
            return None, "missing_id"
        if bool(_value(record, "is_notify", False)):
            return None, "notify"
        platform = str(_value(record, "platform") or "").strip()
        user_id = str(_value(record, "user_id") or "").strip()
        if (platform, user_id) in bot_accounts:
            return None, "bot_message"
        group_id = str(_value(record, "group_id") or "").strip() or None
        session_key = build_session_key(platform, group_id, user_id or None)
        canonical = map_platform(platform)
        if session_key is None or canonical is None:
            return None, "unknown_platform"
        ts = to_ts(_value(record, "timestamp"), self.timezone) or to_ts(
            _value(record, "received_at"), self.timezone
        )
        if ts <= 0:
            return None, "missing_timestamp"
        if since_ts and ts < since_ts:
            return None, "before_window"
        text = str(_value(record, "processed_plain_text") or "")
        is_at = bool(
            _value(record, "is_at", False) or _value(record, "is_mentioned", False)
        )
        is_picture = bool(_value(record, "is_picture", False))
        is_emoji = bool(_value(record, "is_emoji", False))
        reply_to = str(_value(record, "reply_to") or "").strip() or None
        components: list[dict[str, Any]] = []
        flags: dict[str, Any] = {
            "is_group": bool(group_id),
            "is_private": not group_id,
            "is_command": bool(_value(record, "is_command", False)),
        }
        if text:
            components.append({"type": "text", "data": {"text": text[:4000]}})
        if is_at:
            flags["at"] = True
            components.append({"type": "at", "data": {}})
        if is_picture:
            flags["image"] = True
            components.append({"type": "image", "data": {}})
        if is_emoji:
            flags["emoji"] = True
        if reply_to:
            flags["reply"] = True
            components.append({"type": "reply", "data": {"id": reply_to}})
        sender_name = str(
            _value(record, "user_cardname") or _value(record, "user_nickname") or ""
        )
        return (
            {
                "platform_id": canonical,
                "session_key": session_key,
                "message_id": message_id[:255],
                "ts": ts,
                "sender_id": user_id or None,
                "sender_name": sender_name[:255] or None,
                "group_id": group_id,
                "components": components,
                "plain_text": text[:4000],
                "reply_to": reply_to,
                "flags": flags,
                "ingested_at": int(time.time()),
            },
            "",
        )

    async def _import_people(
        self,
        connection: sqlite3.Connection,
        counters: dict[str, Any],
        umos: Sequence[str],
        dry_run: bool,
        include_unknown: bool = False,
    ) -> None:
        """Convert ``person_info`` rows and their memory points.

        Args:
            connection: Open source connection.
            counters: Dataset counters.
            umos: Fallback target sessions for people without a group card.
            dry_run: Count only when True.
            include_unknown: Also import people the source never confirmed.
        """
        people: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []
        ready_people = 0
        ready_facts = 0
        for record in connection.execute("SELECT * FROM person_info ORDER BY id"):
            counters["seen"] += 1
            person_rows, fact_rows, reason, truncated = self._person_rows(
                record, umos, include_unknown
            )
            if not person_rows:
                self._skip(counters, reason)
                continue
            counters["ready"] += len(person_rows) + len(fact_rows)
            counters["truncated"] += truncated
            ready_people += len(person_rows)
            ready_facts += len(fact_rows)
            if not dry_run:
                people.extend(person_rows)
                facts.extend(fact_rows)
        if dry_run:
            counters["detail"] = {"people": ready_people, "facts": ready_facts}
            return
        written_people, duplicate_people = await self._insert(LifePerson, people)
        written_facts, duplicate_facts = await self._insert(LifePersonFact, facts)
        self._record_writes(
            counters,
            written_people + written_facts,
            duplicate_people + duplicate_facts,
        )
        counters["detail"] = {"people": written_people, "facts": written_facts}

    def _person_rows(
        self,
        record: sqlite3.Row,
        umos: Sequence[str],
        include_unknown: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, int]:
        """Convert one person and their memory points.

        Args:
            record: ``person_info`` row.
            umos: Fallback target sessions.
            include_unknown: Also import people the source never confirmed.

        Returns:
            ``(people, facts, "", truncated)``, or ``([], [], reason, 0)`` when
            skipped.
        """
        person_id = str(_value(record, "person_id") or "").strip()
        if not person_id:
            return [], [], "missing_id", 0
        # Older sources have no ``is_known`` column; treat them as confirmed.
        if not include_unknown and not bool(_value(record, "is_known", True)):
            return [], [], "not_known", 0
        platform = str(_value(record, "platform") or "")
        person_name = str(_value(record, "person_name") or "").strip()
        nickname = str(_value(record, "user_nickname") or "").strip()
        targets: list[tuple[str, str | None, str]] = []
        groups = _json_value(_value(record, "group_cardname"), [])
        if isinstance(groups, list):
            for item in groups:
                if not isinstance(item, dict):
                    continue
                group_id = str(item.get("group_id") or "").strip()
                if not group_id:
                    continue
                key = build_session_key(platform, group_id, None)
                if key:
                    card = str(item.get("group_cardname") or "").strip()
                    targets.append((key, group_id, card))
        if not targets:
            targets = [(umo, None, "") for umo in umos]
        if not targets:
            return [], [], "no_session", 0
        know_counts = max(int(_value(record, "know_counts") or 0), 0)
        last_seen = to_ts(_value(record, "last_known_time"), self.timezone)
        now = int(time.time())
        points = _json_value(_value(record, "memory_points"), [])
        fact_specs: list[tuple[str, str, float]] = []
        if isinstance(points, list):
            for point in points:
                spec = self._memory_point(point)
                if spec is not None:
                    fact_specs.append(spec)
        aliases = []
        for alias in (person_name, nickname):
            cleaned = clean_name(alias)
            if cleaned and cleaned not in aliases:
                aliases.append(cleaned)
        person_rows: list[dict[str, Any]] = []
        fact_rows: list[dict[str, Any]] = []
        truncated = 0
        for session_key, group_id, card in targets:
            display = clean_name(card or person_name or nickname)
            session_aliases = list(aliases)
            if display and display not in session_aliases:
                session_aliases.append(display)
            truncated += max(len(session_aliases) - 12, 0)
            person_rows.append(
                {
                    "session_key": session_key,
                    "person_id": person_id[:255],
                    "display_name": display[:255],
                    "normalized_name": normalize_name(display)[:255],
                    "aliases": session_aliases[:12],
                    "group_id": group_id,
                    "relation_score": 0.0,
                    "interactions": know_counts,
                    "last_seen_ts": last_seen,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            for category, content, confidence in fact_specs:
                fact_rows.append(
                    {
                        "session_key": session_key,
                        "person_id": person_id[:255],
                        "content": content,
                        "fact_hash": fact_hash(content),
                        "category": category,
                        "confidence": confidence,
                        "status": "active",
                        "conflict_group": "",
                        "source_memory_id": None,
                        "verified_at": 0,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
        return person_rows, fact_rows, "", truncated

    @staticmethod
    def _memory_point(raw: Any) -> tuple[str, str, float] | None:
        """Parse one MaiBot memory point (``category:text:weight``).

        Args:
            raw: Stored memory point.

        Returns:
            ``(category, content, confidence)`` or None when empty.
        """
        text = str(raw or "").strip()
        if not text:
            return None
        parts = text.split(":", 2)
        if len(parts) == 3:
            category = parts[0].strip() or "other"
            content = parts[1].strip()
            try:
                confidence = float(parts[2].strip())
            except ValueError:
                confidence = 0.6
        else:
            category = "other"
            content = text
            confidence = 0.6
        if not content:
            return None
        if not 0.0 < confidence <= 1.0:
            confidence = 0.6
        return category[:32], content[:2000], confidence

    async def _import_expressions(
        self,
        connection: sqlite3.Connection,
        counters: dict[str, Any],
        session_index: dict[str, dict[str, Any]],
        umos: Sequence[str],
        dry_run: bool,
    ) -> None:
        """Convert ``expressions`` rows.

        Args:
            connection: Open source connection.
            counters: Dataset counters.
            session_index: ``session_id`` to conversation metadata.
            umos: Fallback targets for global expressions.
            dry_run: Count only when True.
        """
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for record in connection.execute("SELECT * FROM expressions ORDER BY id"):
            counters["seen"] += 1
            converted, reason, truncated = self._expression_rows(
                record, session_index, umos, seen
            )
            if not converted:
                self._skip(counters, reason)
                continue
            counters["ready"] += len(converted)
            counters["truncated"] += truncated
            if not dry_run:
                rows.extend(converted)
        if rows and not dry_run:
            written, duplicates = await self._insert(LifeExpression, rows)
            self._record_writes(counters, written, duplicates)

    def _expression_rows(
        self,
        record: sqlite3.Row,
        session_index: dict[str, dict[str, Any]],
        umos: Sequence[str],
        seen: set[tuple[str, str]],
    ) -> tuple[list[dict[str, Any]], str, int]:
        """Convert one MaiBot expression.

        Args:
            record: ``expressions`` row.
            session_index: ``session_id`` to conversation metadata.
            umos: Fallback targets for global expressions.
            seen: ``(session_key, checksum)`` pairs already converted.

        Returns:
            ``(rows, "", truncated)``, or ``([], reason, 0)`` when skipped.
        """
        style = str(_value(record, "style") or "").strip()
        session_id = str(_value(record, "session_id") or "").strip()
        session_keys: list[str] = []
        if session_id:
            meta = session_index.get(session_id)
            if meta is not None:
                key = build_session_key(
                    str(meta.get("platform") or ""),
                    str(meta.get("group_id") or "") or None,
                    str(meta.get("user_id") or "") or None,
                )
                if key:
                    session_keys.append(key)
        if not session_keys:
            session_keys = [str(umo) for umo in umos if umo]
        if not session_keys:
            return [], "no_session", 0
        contents = _json_value(_value(record, "content_list"), [])
        if isinstance(contents, str):
            contents = [contents]
        texts = (
            [str(item).strip() for item in contents if str(item).strip()]
            if isinstance(contents, list)
            else []
        )
        if not texts and style:
            texts = [style]
        if not texts:
            return [], "empty", 0
        situation = str(_value(record, "situation") or "").strip()
        tags = [style] if style else []
        now = int(time.time())
        created = to_ts(_value(record, "create_time"), self.timezone) or now
        last_used = to_ts(_value(record, "last_active_time"), self.timezone)
        usage = max(int(_value(record, "count") or 0), 0)
        # MaiBot only replies with expressions a human approved; keep the rest as
        # pending so the archive still has them without using them.
        approved = bool(_value(record, "checked", True))
        truncated = max(len(texts) - 20, 0)
        rows: list[dict[str, Any]] = []
        for text in texts[:20]:
            checksum = expression_checksum(situation, text)
            for session_key in dict.fromkeys(session_keys):
                if (session_key, checksum) in seen:
                    continue
                seen.add((session_key, checksum))
                rows.append(
                    {
                        "session_key": session_key,
                        "scene": "any",
                        "situation": situation,
                        "style": text[:255],
                        "tags": tags,
                        "checksum": checksum,
                        "weight": 1.0,
                        "usage_count": usage,
                        "last_used_ts": last_used,
                        "source": "maibot",
                        "status": "active" if approved else "pending",
                        "vector_state": "pending",
                        "created_at": created,
                        "updated_at": now,
                    }
                )
        return rows, "" if rows else "duplicate", truncated

    async def _import_jargon(
        self,
        connection: sqlite3.Connection,
        counters: dict[str, Any],
        session_index: dict[str, dict[str, Any]],
        umos: Sequence[str],
        dry_run: bool,
    ) -> None:
        """Convert ``jargons`` rows.

        Args:
            connection: Open source connection.
            counters: Dataset counters.
            session_index: ``session_id`` to conversation metadata.
            umos: Targets for global slang.
            dry_run: Count only when True.
        """
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for record in connection.execute("SELECT * FROM jargons ORDER BY id"):
            counters["seen"] += 1
            converted, reason, truncated = self._jargon_rows(
                record, session_index, umos, seen
            )
            if not converted:
                self._skip(counters, reason)
                continue
            counters["ready"] += len(converted)
            counters["truncated"] += truncated
            if not dry_run:
                rows.extend(converted)
        if rows and not dry_run:
            written, duplicates = await self._insert(LifeJargon, rows)
            self._record_writes(counters, written, duplicates)

    def _jargon_rows(
        self,
        record: sqlite3.Row,
        session_index: dict[str, dict[str, Any]],
        umos: Sequence[str],
        seen: set[tuple[str, str]],
    ) -> tuple[list[dict[str, Any]], str, int]:
        """Convert one MaiBot slang entry.

        Args:
            record: ``jargons`` row.
            session_index: ``session_id`` to conversation metadata.
            umos: Targets for global slang.
            seen: ``(session_key, checksum)`` pairs already converted.

        Returns:
            ``(rows, "", truncated)``, or ``([], reason, 0)`` when skipped.
        """
        if not bool(_value(record, "is_jargon", False)):
            return [], "not_jargon", 0
        # ``life_jargon.term`` is capped at 64 characters; hash the value that is
        # actually stored so the checksum and the row stay consistent.
        term = str(_value(record, "content") or "").strip()[:64]
        if not term:
            return [], "missing_term", 0
        meaning = str(_value(record, "meaning") or "").strip()
        session_keys: list[str] = []
        if bool(_value(record, "is_global", False)):
            session_keys = [str(umo) for umo in umos if umo]
        else:
            counts = _json_value(_value(record, "session_id_dict"), {})
            if isinstance(counts, dict):
                for session_id in counts:
                    meta = session_index.get(str(session_id))
                    if meta is None:
                        continue
                    key = build_session_key(
                        str(meta.get("platform") or ""),
                        str(meta.get("group_id") or "") or None,
                        str(meta.get("user_id") or "") or None,
                    )
                    if key:
                        session_keys.append(key)
        if not session_keys:
            session_keys = [str(umo) for umo in umos if umo]
        if not session_keys:
            return [], "no_session", 0
        evidence: list[dict[str, Any]] = []
        raw_evidence = _json_value(_value(record, "evidence_messages"), [])
        if isinstance(raw_evidence, list):
            for group in raw_evidence:
                items = group if isinstance(group, list) else [group]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    evidence.append(
                        {
                            "platform": str(item.get("platform") or "")[:32],
                            "message_id": str(item.get("message_id") or "")[:255],
                        }
                    )
        created_by = str(_value(record, "created_by") or "")
        confidence = 0.8 if bool(_value(record, "is_complete", False)) else 0.6
        created = to_ts(_value(record, "created_timestamp"), self.timezone)
        updated = to_ts(_value(record, "updated_timestamp"), self.timezone)
        usage = max(int(_value(record, "count") or 0), 0)
        now = int(time.time())
        checksum = term_checksum(term)
        truncated = max(len(evidence) - 20, 0)
        rows: list[dict[str, Any]] = []
        for session_key in dict.fromkeys(session_keys):
            if (session_key, checksum) in seen:
                continue
            seen.add((session_key, checksum))
            rows.append(
                {
                    "session_key": session_key,
                    "term": term,
                    "meaning": meaning[:2000],
                    "confidence": confidence,
                    "status": "active",
                    "source": "manual" if "MANUAL" in created_by.upper() else "llm",
                    "evidence": evidence[:20],
                    "checksum": checksum,
                    "usage_count": usage,
                    "last_seen_ts": updated,
                    "created_at": created or now,
                    "updated_at": now,
                }
            )
        return rows, "" if rows else "duplicate", truncated

    async def _import_emoji(
        self,
        source: MaiBotSource,
        connection: sqlite3.Connection,
        counters: dict[str, Any],
        umos: Sequence[str],
        dry_run: bool,
    ) -> None:
        """Copy sticker files and insert their rows.

        Args:
            source: Source deployment (for relative file paths).
            connection: Open source connection.
            counters: Dataset counters.
            umos: Target sessions.
            dry_run: Count only when True; no file is copied.
        """
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        now = int(time.time())
        for record in connection.execute("SELECT * FROM images ORDER BY id"):
            counters["seen"] += 1
            if str(_value(record, "image_type") or "emoji").lower() != "emoji":
                self._skip(counters, "not_emoji")
                continue
            if bool(_value(record, "is_banned", False)):
                self._skip(counters, "banned")
                continue
            path = self._emoji_path(source.root, _value(record, "full_path"))
            if path is None:
                self._skip(counters, "missing_file")
                continue
            try:
                data = path.read_bytes()
            except OSError:
                self._skip(counters, "unreadable_file")
                continue
            mime, suffix = _sniff_image(data)
            if not mime:
                self._skip(counters, "unsupported_image")
                continue
            digest = hashlib.sha256(data).hexdigest()
            description = str(_value(record, "description") or "").strip()
            words = [part for part in re.split(r"[,，、\s]+", description) if part]
            tags = words[:8]
            counters["truncated"] += max(len(words) - 8, 0)
            usage = max(int(_value(record, "query_count") or 0), 0)
            last_used = to_ts(_value(record, "last_used_time"), self.timezone)
            for umo in umos:
                if (umo, digest) in seen:
                    continue
                seen.add((umo, digest))
                counters["ready"] += 1
                if dry_run:
                    continue
                destination = self._emoji_destination(umo, digest, suffix)
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.exists():
                        destination.write_bytes(data)
                except OSError as exc:
                    counters["errors"] += 1
                    logger.warning(f"[life] emoji migration write failed: {exc}")
                    continue
                rows.append(
                    {
                        "session_key": umo,
                        "sha256": digest,
                        "file_path": str(destination),
                        "mime": mime,
                        "size_bytes": len(data),
                        "width": 0,
                        "height": 0,
                        "tags": tags,
                        "caption": description[:2000],
                        "embedding": None,
                        "embedding_model": "",
                        "embedding_dim": 0,
                        "usage_count": usage,
                        "last_used_ts": last_used,
                        "status": "active",
                        "source": "maibot",
                        "created_at": now,
                        "updated_at": now,
                    }
                )
        if rows and not dry_run:
            written, duplicates = await self._insert(LifeEmoji, rows)
            self._record_writes(counters, written, duplicates)

    @staticmethod
    def _emoji_path(root: Path, raw: Any) -> Path | None:
        """Resolve a stored sticker path.

        Args:
            root: MaiBot instance root.
            raw: ``full_path`` column value.

        Returns:
            Existing path or None.
        """
        if not raw:
            return None
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = root / path
        return path if path.is_file() else None

    def _emoji_destination(self, session_key: str, digest: str, suffix: str) -> Path:
        """Return the Life sticker path of one file.

        Args:
            session_key: Target session.
            digest: SHA-256 of the file.
            suffix: File suffix.

        Returns:
            Path below ``<life data>/emoji/<session>/``.
        """
        folder = (
            self.data_dir
            / "emoji"
            / hashlib.sha1(session_key.encode()).hexdigest()[:16]
        )
        return folder / f"{digest}{suffix}"

    async def _import_memory(
        self,
        source: MaiBotSource,
        counters: dict[str, Any],
        memory_source: str | Path | None,
        dry_run: bool,
    ) -> None:
        """Convert A_memorix paragraphs into global memory rows.

        Args:
            source: Source deployment.
            counters: Dataset counters.
            memory_source: Explicit A_memorix directory or database.
            dry_run: Count only when True.
        """
        db_path = source.memory_db(memory_source)
        if db_path is None:
            counters["errors"] += 1
            counters["skipped_reasons"]["memory_db_missing"] = 1
            return
        connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            if "paragraphs" not in MaiBotSource.table_names(connection):
                counters["errors"] += 1
                counters["skipped_reasons"]["paragraphs_missing"] = 1
                return
            batch: list[dict[str, Any]] = []
            for record in connection.execute("SELECT * FROM paragraphs"):
                counters["seen"] += 1
                if int(_value(record, "is_deleted", 0) or 0):
                    self._skip(counters, "deleted")
                    continue
                content = str(_value(record, "content") or "").strip()
                if not content:
                    self._skip(counters, "empty")
                    continue
                counters["ready"] += 1
                batch.append(
                    {
                        "content": content,
                        "kind": _memory_kind(_value(record, "knowledge_type")),
                        "ts": to_ts(_value(record, "event_time"), self.timezone)
                        or to_ts(_value(record, "created_at"), self.timezone),
                    }
                )
                if len(batch) >= _MEMORY_BATCH:
                    await self._flush_memory(counters, batch, dry_run)
                    batch = []
            if batch:
                await self._flush_memory(counters, batch, dry_run)
        finally:
            connection.close()

    async def _flush_memory(
        self,
        counters: dict[str, Any],
        batch: list[dict[str, Any]],
        dry_run: bool,
    ) -> None:
        """Write one A_memorix batch through the memory importer.

        Args:
            counters: Dataset counters.
            batch: Converted rows.
            dry_run: Count only when True.
        """
        result = await self.importer.import_rows(
            list(batch),
            session_key="global",
            default_kind="note",
            dry_run=dry_run,
            scope="global",
            source="a_memorix",
        )
        written = int(result["imported"])
        skipped = int(result["skipped"])
        errors = int(result["errors"])
        counters["written"] += written
        counters["skipped"] += skipped
        counters["errors"] += errors
        if dry_run:
            return
        # Paragraphs dropped by the natural key on a re-run, so a second pass
        # reads as duplicates instead of silently writing nothing.
        duplicates = len(batch) - skipped - written - errors
        if duplicates > 0:
            counters["skipped"] += duplicates
            reasons = counters["skipped_reasons"]
            reasons["duplicate"] = int(reasons.get("duplicate", 0)) + duplicates

    @staticmethod
    def _count_paragraphs(path: Path) -> int:
        """Count the paragraphs an import would consider, or 0.

        Args:
            path: A_memorix ``metadata.db``.

        Returns:
            Row count without the soft-deleted paragraphs, so the inspection
            matches what :meth:`run` can actually import.
        """
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            if "paragraphs" not in MaiBotSource.table_names(connection):
                return 0
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(paragraphs)")
            }
            clause = " WHERE is_deleted = 0" if "is_deleted" in columns else ""
            return int(
                connection.execute(
                    f"SELECT COUNT(*) FROM paragraphs{clause}"
                ).fetchone()[0]
                or 0
            )
        finally:
            connection.close()

    @staticmethod
    def _paragraphs_available(path: Path) -> bool:
        """Return whether an A_memorix database has the paragraphs table.

        Args:
            path: A_memorix ``metadata.db``.

        Returns:
            True when the table exists.
        """
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            return "paragraphs" in MaiBotSource.table_names(connection)
        finally:
            connection.close()

    def _unmapped_platforms(
        self,
        connection: sqlite3.Connection | None,
    ) -> list[str]:
        """Return the source platforms this bot cannot address.

        Args:
            connection: Open source connection, or None for a best-effort empty
                answer after the connection was closed.

        Returns:
            Sorted raw platform ids.
        """
        if connection is None:
            return []
        tables = MaiBotSource.table_names(connection)
        found: set[str] = set()
        for table in ("mai_messages", "person_info"):
            if table not in tables:
                continue
            for row in connection.execute(f'SELECT DISTINCT platform FROM "{table}"'):
                raw = str(row[0] or "")
                if raw and map_platform(raw) is None:
                    found.add(raw)
        return sorted(found)

    async def _save_run(
        self,
        batch_id: str,
        source: MaiBotSource,
        datasets: Sequence[str],
        counts: dict[str, dict[str, Any]],
        marks: dict[str, dict[str, int]],
        dry_run: bool,
        *,
        umos: Sequence[str],
        since_days: int,
        limit: int,
        status: str,
        started: int,
        error: str = "",
        include_unknown: bool = False,
    ) -> None:
        """Persist one batch manifest.

        Args:
            batch_id: Batch identifier.
            source: Source deployment.
            datasets: Imported datasets.
            counts: Per-dataset counters.
            marks: Rollback watermarks.
            dry_run: Whether the batch only previewed.
            umos: Target sessions.
            since_days: Message window in days.
            limit: Message limit.
            status: Manifest status.
            started: Start timestamp.
            error: Failure detail for a failed batch.
            include_unknown: Whether unconfirmed people were imported.
        """
        async with self.db.get_db() as session:
            session.add(
                LifeMigrationRun(
                    batch_id=batch_id,
                    source=str(source.db_path),
                    status=status,
                    dry_run=dry_run,
                    datasets=list(datasets),
                    counts=counts,
                    marks=marks,
                    options={
                        "umos": list(umos),
                        "since_days": since_days,
                        "limit": limit,
                        "include_unknown": include_unknown,
                        "timezone": self.timezone,
                    },
                    error=error,
                    created_at=started,
                    finished_at=int(time.time()),
                )
            )
            await session.commit()

    async def _get_run(self, batch_id: str) -> LifeMigrationRun | None:
        """Return one batch manifest.

        Args:
            batch_id: Batch identifier.

        Returns:
            Manifest row, or None when missing.
        """
        async with self.db.get_db() as session:
            return (
                (
                    await session.execute(
                        select(LifeMigrationRun).where(
                            col(LifeMigrationRun.batch_id) == batch_id
                        )
                    )
                )
                .scalars()
                .first()
            )

    async def _mark_rolled_back(self, batch_id: str) -> None:
        """Mark one batch as rolled back.

        Args:
            batch_id: Batch identifier.
        """
        async with self.db.get_db() as session:
            run = (
                (
                    await session.execute(
                        select(LifeMigrationRun).where(
                            col(LifeMigrationRun.batch_id) == batch_id
                        )
                    )
                )
                .scalars()
                .first()
            )
            if run is not None:
                run.status = "rolled_back"
                run.rolled_back_at = int(time.time())
                session.add(run)
                await session.commit()

    @staticmethod
    def _run_payload(run: LifeMigrationRun) -> dict[str, Any]:
        """Serialize one manifest row.

        Args:
            run: Manifest row.

        Returns:
            JSON-safe mapping for the CLI and the panel.
        """
        return {
            "batch_id": run.batch_id,
            "source": run.source,
            "status": run.status,
            "dry_run": run.dry_run,
            "datasets": list(run.datasets or []),
            "counts": dict(run.counts or {}),
            "marks": dict(run.marks or {}),
            "options": dict(run.options or {}),
            "error": run.error,
            "created_at": run.created_at,
            "finished_at": run.finished_at,
            "rolled_back_at": run.rolled_back_at,
        }


def _models_for(datasets: Sequence[str]) -> list[type[SQLModel]]:
    """Return the Life tables touched by a dataset selection.

    Args:
        datasets: Dataset names.

    Returns:
        Models used for the rollback watermark, in dataset order.
    """
    models: list[type[SQLModel]] = []
    for dataset in datasets:
        models.extend(_DATASET_MODELS.get(dataset, ()))
    return models


_MODEL_BY_TABLE = {str(getattr(model, "__tablename__")): model for model in _ALL_MODELS}
