"""Rollback drill for the MaiBot migration (S7c).

The drill copies the live database, imports a synthetic MaiBot source, inserts
one "live traffic" row, rolls the batch back and verifies that the copy returns
to its baseline. It measures both directions and appends the record to
``<data-dir>/drill/history.jsonl``.

Examples:
    python scripts/life/drill.py --db data/data_v4.db
    python scripts/life/drill.py --db data/data_v4.db --messages 1000 --keep
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot.core.db.sqlite import SQLiteDatabase  # noqa: E402
from bot.life.migration.drill import (  # noqa: E402
    DEFAULT_MESSAGES,
    build_synthetic_source,
    run_drill,
)

DEFAULT_UMO = "aiocqhttp:GroupMessage:drill"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/data_v4.db", help="Live database")
    parser.add_argument(
        "--data-dir",
        default="data/life",
        help="Life data directory; drill artifacts live under <data-dir>/drill",
    )
    parser.add_argument("--messages", type=int, default=DEFAULT_MESSAGES)
    parser.add_argument("--umo", default=DEFAULT_UMO)
    parser.add_argument("--keep", action="store_true", help="Keep the drill copy")
    parser.add_argument("--json", action="store_true", help="Print raw JSON")
    return parser.parse_args()


def _copy_database(source: Path, destination: Path) -> None:
    """Copy a SQLite database with the backup API.

    Args:
        source: Live database path; opened read-only.
        destination: Copy path.
    """
    origin = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    target = sqlite3.connect(destination)
    try:
        origin.backup(target)
    finally:
        target.close()
        origin.close()


async def _run(args: argparse.Namespace) -> int:
    """Execute the drill.

    Args:
        args: Parsed command line.

    Returns:
        Process exit code: 0 pass, 1 fail or error.
    """
    source_db = Path(args.db).expanduser().resolve()  # noqa: ASYNC240  # one-off path probe
    data_dir = Path(args.data_dir).expanduser().resolve()  # noqa: ASYNC240  # one-off path probe
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    drill_root = data_dir / "drill" / stamp
    drill_root.mkdir(parents=True, exist_ok=True)
    copy_path = drill_root / "drill.db"
    _copy_database(source_db, copy_path)
    source_root = build_synthetic_source(
        drill_root / "maibot",
        messages=args.messages,
    )

    database = SQLiteDatabase(str(copy_path))
    await database.initialize()
    try:
        result = await run_drill(
            database,
            source_root,
            [args.umo],
            drill_root / "life",
        )
    finally:
        await database.engine.dispose()

    record = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "source_db": str(source_db),
        "messages": args.messages,
        "umo": args.umo,
        **result,
    }
    (drill_root / "record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    history = data_dir / "drill" / "history.jsonl"
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    (data_dir / "drill" / "last.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if args.json:
        print(json.dumps(record, ensure_ascii=False, indent=2))
    else:
        print(f"copy:     {copy_path}")
        print(
            f"imported: {record['imported_rows']} row(s) in {record['import_seconds']}s"
        )
        print(
            f"rollback: {record['deleted_rows']} row(s) in "
            f"{record['rollback_seconds']}s "
            f"(limit {record['limit_seconds']}s)"
        )
        print(f"live row survived: {record['sentinel_survived']}")
        print(f"baseline restored: {record['after_cleanup'] == record['baseline']}")
        print(f"record: {'PASS' if record['passed'] else 'FAIL'}")
    if not args.keep:
        shutil.rmtree(drill_root, ignore_errors=True)
    return 0 if record["passed"] else 1


def main() -> int:
    """Run the drill CLI.

    Returns:
        Process exit code.
    """
    try:
        return asyncio.run(_run(_parse_args()))
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
