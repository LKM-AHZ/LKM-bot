"""Offline MaiBot -> Life migration (S7b).

The bot should be stopped while a real import runs: the importer records row-id
watermarks for rollback and concurrent writes could fall inside the window.

Examples:
    python scripts/life/migrate.py inspect --source /srv/maibot
    python scripts/life/migrate.py preview --source /srv/maibot \
        --umo aiocqhttp:GroupMessage:1
    python scripts/life/migrate.py import --source /srv/maibot \
        --umo aiocqhttp:GroupMessage:1 --since-days 90
    python scripts/life/migrate.py runs
    python scripts/life/migrate.py rollback --batch 0123456789abcdef
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot.core.db.sqlite import SQLiteDatabase  # noqa: E402
from bot.life.migration.service import MigrationService  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/data_v4.db", help="Path to data_v4.db")
    parser.add_argument(
        "--data-dir",
        default="data/life",
        help="Life data directory (sticker files are copied below it)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument("--json", action="store_true", help="Print raw JSON")

    inspect = sub.add_parser(
        "inspect", parents=[json_parent], help="Show the source layout"
    )
    inspect.add_argument("--source", required=True)
    inspect.add_argument("--memory-source", default=None)
    inspect.add_argument("--datasets", default=None)

    for name, help_text in (
        ("preview", "Dry run: convert and report without writing"),
        ("import", "Write the converted rows"),
    ):
        command = sub.add_parser(name, parents=[json_parent], help=help_text)
        command.add_argument("--source", required=True)
        command.add_argument(
            "--umo",
            action="append",
            default=[],
            help="Target session for global data; repeatable",
        )
        command.add_argument("--datasets", default=None)
        command.add_argument("--since-days", type=int, default=0)
        command.add_argument("--limit", type=int, default=0)
        command.add_argument("--memory-source", default=None)

    rollback = sub.add_parser(
        "rollback", parents=[json_parent], help="Delete the rows of one batch"
    )
    rollback.add_argument("--batch", required=True)

    runs = sub.add_parser("runs", parents=[json_parent], help="List migration batches")
    runs.add_argument("--limit", type=int, default=20)

    return parser.parse_args()


def _datasets(raw: str | None) -> list[str] | None:
    """Split a comma-separated dataset list.

    Args:
        raw: ``messages,people`` or None.

    Returns:
        Dataset names, or None for the default set.
    """
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def _print_inspect(report: dict) -> None:
    """Print the source inspection.

    Args:
        report: ``MigrationService.preflight`` result.
    """
    source = report["source"]
    print(f"source: {source['db']}")
    print(f"root:   {source['root']}")
    print(f"memory: {report['memory_db'] or '-'}")
    print("dataset    available  rows")
    for dataset in report["datasets"]:
        state = "yes" if dataset["available"] else "no"
        print(f"{dataset['name']:<10} {state:<10} {dataset['rows']}")
    sessions = report["sessions"]
    print(f"sessions: {sessions['mapped']} mapped")
    if sessions["unmapped_platforms"]:
        print("unmapped platforms: " + ", ".join(sessions["unmapped_platforms"]))
    for warning in report["warnings"]:
        print(f"warning: {warning}")


def _print_run(report: dict) -> None:
    """Print one import or dry-run report.

    Args:
        report: ``MigrationService.run`` result.
    """
    print(f"source: {report['source']}")
    print(f"dry run: {'yes' if report['dry_run'] else 'no'}")
    batch = report.get("batch_id") or "-"
    print(f"batch: {batch}")
    print("dataset      seen   ready  written  skipped  errors")
    for name, block in report["datasets"].items():
        print(
            f"{name:<12} {block['seen']:>6} {block['ready']:>7} "
            f"{block['written']:>8} {block['skipped']:>8} {block['errors']:>7}"
        )
    totals = report["totals"]
    print(
        f"{'total':<12} {totals['seen']:>6} {totals['ready']:>7} "
        f"{totals['written']:>8} {totals['skipped']:>8} {totals['errors']:>7}"
    )
    for name, block in report["datasets"].items():
        reasons = block.get("skipped_reasons") or {}
        if reasons:
            detail = ", ".join(f"{key}={value}" for key, value in reasons.items())
            print(f"skipped {name}: {detail}")
        extra = block.get("detail") or {}
        if extra:
            detail = ", ".join(f"{key}={value}" for key, value in extra.items())
            print(f"detail {name}: {detail}")
    unmapped = report["sessions"]["unmapped_platforms"]
    if unmapped:
        print("unmapped platforms: " + ", ".join(unmapped))


async def _run(args: argparse.Namespace) -> int:
    """Execute one command.

    Args:
        args: Parsed command line.

    Returns:
        Process exit code.
    """
    database = SQLiteDatabase(str(args.db))
    await database.initialize()
    try:
        service = MigrationService(database, args.data_dir)
        await service.ensure_schema()
        if args.command == "inspect":
            report = await service.preflight(
                args.source,
                datasets=_datasets(args.datasets),
                memory_source=args.memory_source,
            )
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                _print_inspect(report)
            return 0
        if args.command in ("preview", "import"):
            report = await service.run(
                args.source,
                umos=args.umo,
                datasets=_datasets(args.datasets),
                dry_run=args.command == "preview",
                since_days=args.since_days,
                limit=args.limit,
                memory_source=args.memory_source,
            )
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                _print_run(report)
            return 0
        if args.command == "rollback":
            report = await service.rollback(args.batch)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(f"batch: {report['batch_id']}")
                for table, deleted in report["deleted"].items():
                    print(f"deleted {table}: {deleted}")
            return 0
        if args.command == "runs":
            rows = await service.runs(args.limit)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    print(
                        f"{row['batch_id']:<18} {row['status']:<12} "
                        f"{'dry' if row['dry_run'] else 'real':<5} "
                        f"{','.join(row['datasets'])} {row['source']}"
                    )
            return 0
        print(f"unknown command: {args.command}", file=sys.stderr)
        return 2
    finally:
        await database.engine.dispose()


def main() -> int:
    """Run the CLI.

    Returns:
        Process exit code.
    """
    try:
        return asyncio.run(_run(_parse_args()))
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
