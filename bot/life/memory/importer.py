"""Import memory data from a previous MaiBot deployment.

MaiBot keeps memory in several shapes depending on version and plugin: JSONL
dumps, plain JSON arrays and SQLite databases. The importer accepts all three,
normalizes the common column names, and supports a dry run so a migration can be
inspected before anything is written.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from bot import logger
from bot.life.memory.store import MemoryStore, tokenize

_CONTENT_KEYS = ("content", "text", "memory", "memo", "summary", "fact", "content_text")
_TS_KEYS = ("ts", "timestamp", "time", "created_at", "create_time", "datetime")
_PERSON_KEYS = ("person", "person_id", "person_name", "subject", "name")
_KIND_KEYS = ("kind", "type", "category", "memory_type")


def _first_value(row: dict[str, Any], keys: Iterable[str]) -> Any:
    """Return the first present value among candidate keys.

    Args:
        row: Source row.
        keys: Candidate column names.

    Returns:
        The first non-empty value, or None.
    """
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _normalize_ts(value: Any) -> int:
    """Convert a timestamp-like value to a Unix timestamp.

    Args:
        value: Integer seconds, float seconds, milliseconds or ISO text.

    Returns:
        Unix timestamp in seconds; 0 when it cannot be parsed.
    """
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10**11:  # milliseconds
            number /= 1000.0
        return int(number)
    text = str(value).strip()
    if text.isdigit():
        return _normalize_ts(int(text))
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(time.mktime(time.strptime(text[:19], pattern)))
        except ValueError:
            continue
    return 0


class MemoryImporter:
    """Import memory rows from MaiBot exports into the local engine."""

    def __init__(self, store: MemoryStore) -> None:
        """Initialize the importer.

        Args:
            store: Target memory store.
        """
        self.store = store

    async def import_path(
        self,
        path: str | Path,
        session_key: str,
        default_kind: str = "summary",
        dry_run: bool = True,
        scope: str = "session",
        source: str = "maibot",
    ) -> dict[str, Any]:
        """Import one file or directory.

        Args:
            path: JSONL, JSON or SQLite file, or a directory to scan.
            session_key: Session the imported memories belong to.
            default_kind: Kind used when a row has no kind column.
            dry_run: When True, rows are counted but not written.
            scope: ``session`` for per-chat rows, ``global`` for cross-session
                rows such as the A_memorix knowledge base.
            source: Provenance stored in the row metadata.

        Returns:
            Counters with per-file detail.
        """
        target = Path(path)
        if not target.exists():
            raise FileNotFoundError(target)
        files = (
            [file for file in target.rglob("*") if file.is_file()]
            if target.is_dir()
            else [target]
        )
        totals = {"files": 0, "rows_seen": 0, "imported": 0, "skipped": 0, "errors": 0}
        detail: list[dict[str, Any]] = []
        for file in sorted(files):
            suffix = file.suffix.lower()
            try:
                if suffix in (".jsonl", ".ndjson"):
                    rows = list(self._read_jsonl(file))
                elif suffix == ".json":
                    rows = self._read_json(file)
                elif suffix in (".db", ".sqlite", ".sqlite3"):
                    rows = list(self._read_sqlite(file))
                else:
                    continue
            except Exception as exc:
                totals["errors"] += 1
                detail.append({"file": str(file), "error": str(exc)})
                logger.warning(f"[life] memory import failed for {file}: {exc}")
                continue
            result = await self.import_rows(
                rows,
                session_key=session_key,
                default_kind=default_kind,
                dry_run=dry_run,
                scope=scope,
                source=source,
            )
            totals["files"] += 1
            totals["rows_seen"] += result["rows_seen"]
            totals["imported"] += result["imported"]
            totals["skipped"] += result["skipped"]
            totals["errors"] += result["errors"]
            detail.append({"file": str(file), **result})
        return {**totals, "dry_run": dry_run, "detail": detail}

    async def import_rows(
        self,
        rows: Iterable[dict[str, Any]],
        session_key: str,
        default_kind: str = "summary",
        dry_run: bool = True,
        scope: str = "session",
        source: str = "maibot",
    ) -> dict[str, int]:
        """Normalize and write a batch of foreign memory rows.

        Args:
            rows: Source rows with loosely named columns.
            session_key: Session the rows belong to.
            default_kind: Kind used when a row has no kind column.
            dry_run: When True, rows are counted but not written.
            scope: ``session`` for per-chat rows, ``global`` for cross-session
                rows such as the A_memorix knowledge base.
            source: Provenance stored in the row metadata.

        Returns:
            Counters for this batch.
        """
        counters = {"rows_seen": 0, "imported": 0, "skipped": 0, "errors": 0}
        payload: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            counters["rows_seen"] += 1
            if not isinstance(row, dict):
                counters["skipped"] += 1
                continue
            content = _first_value(row, _CONTENT_KEYS)
            if not content or not str(content).strip():
                counters["skipped"] += 1
                continue
            text = str(content).strip()
            if text in seen:
                counters["skipped"] += 1
                continue
            seen.add(text)
            kind = str(_first_value(row, _KIND_KEYS) or default_kind).lower()
            if kind not in ("summary", "fact", "episode", "note"):
                kind = default_kind
            person = _first_value(row, _PERSON_KEYS)
            payload.append(
                {
                    "kind": kind,
                    "session_key": session_key,
                    "scope": scope if scope in ("session", "global") else "session",
                    "content": text[:4000],
                    "person_id": str(person)[:255] if person else None,
                    "importance": 0.5,
                    "ts": _normalize_ts(_first_value(row, _TS_KEYS))
                    or int(time.time()),
                    "meta": {"imported": True, "source": source},
                    "keywords": " ".join(tokenize(text)[:64]),
                }
            )
        if dry_run or not payload:
            counters["imported"] = 0 if dry_run else 0
            return counters
        try:
            counters["imported"] = await self.store.insert_many(payload)
        except Exception as exc:
            counters["errors"] += 1
            logger.warning(f"[life] memory import write failed: {exc}")
        return counters

    def _read_jsonl(self, path: Path) -> Iterable[dict[str, Any]]:
        """Yield rows from a JSONL file.

        Args:
            path: File to read.

        Yields:
            Parsed objects.
        """
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def _read_json(self, path: Path) -> list[dict[str, Any]]:
        """Read a JSON array or object file.

        Args:
            path: File to read.

        Returns:
            List of row dictionaries.
        """
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [row for row in raw if isinstance(row, dict)]
        if isinstance(raw, dict):
            for key in ("memories", "items", "data", "rows"):
                value = raw.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
            return [raw]
        return []

    def _read_sqlite(self, path: Path) -> Iterable[dict[str, Any]]:
        """Yield rows from every table that looks like a memory table.

        Args:
            path: SQLite database file.

        Yields:
            Row dictionaries.
        """
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            tables = [
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
            for table in tables:
                columns = [
                    row["name"]
                    for row in connection.execute(f'PRAGMA table_info("{table}")')
                ]
                if not any(key in columns for key in _CONTENT_KEYS):
                    continue
                for row in connection.execute(f'SELECT * FROM "{table}"'):
                    yield dict(row)
        finally:
            connection.close()
