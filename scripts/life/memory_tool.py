"""Offline memory maintenance: stats, search, import, export and self-check.

The script talks to the SQLite file directly, so it can run while the bot is
stopped (recommended for imports) or against a copy of a production database.

Examples:
    python scripts/life/memory_tool.py stats --db data/data_v4.db
    python scripts/life/memory_tool.py search --q 迁移 --umo aiocqhttp:GroupMessage:1
    python scripts/life/memory_tool.py import --path mai_memory/ \
        --umo aiocqhttp:GroupMessage:1 --apply
    python scripts/life/memory_tool.py selfcheck
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
from bot.life.memory.importer import MemoryImporter  # noqa: E402
from bot.life.memory.store import MemoryStore  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/data_v4.db", help="Path to data_v4.db")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="Print memory counters")

    search = sub.add_parser("search", help="Search one session's memory")
    search.add_argument("--umo", required=True)
    search.add_argument("--q", required=True)
    search.add_argument(
        "--mode",
        default="hybrid",
        choices=["search", "time", "hybrid", "episode", "aggregate"],
    )
    search.add_argument("--limit", type=int, default=5)

    importer = sub.add_parser("import", help="Import a MaiBot memory export")
    importer.add_argument("--path", required=True)
    importer.add_argument("--umo", required=True)
    importer.add_argument("--kind", default="summary")
    importer.add_argument(
        "--apply",
        action="store_true",
        help="Write the rows; without it the import is a dry run",
    )

    exporter = sub.add_parser("export", help="Export memories to JSON")
    exporter.add_argument("--umo", default=None)
    exporter.add_argument("--out", default="data/life/memory/export.json")

    sub.add_parser("selfcheck", help="Validate the memory tables")

    bench = sub.add_parser("bench", help="Measure local hybrid search latency")
    bench.add_argument("--rows", type=int, default=1000)
    bench.add_argument("--queries", type=int, default=50)
    return parser.parse_args()


async def _open_store(db_path: Path, retention_days: int = 0) -> tuple:
    database = SQLiteDatabase(str(db_path))
    await database.initialize()
    store = MemoryStore(database, retention_days=retention_days)
    await store.ensure_schema()
    return database, store


async def _bench(args: argparse.Namespace) -> int:
    """Measure search latency on a synthetic store.

    Args:
        args: Parsed CLI arguments with row and query counts.

    Returns:
        Process exit code.
    """
    import statistics
    import tempfile
    import time

    from bot.life.memory.store import pack_vector

    topics = [
        "迁移",
        "记忆",
        "心流",
        "插件",
        "面板",
        "部署",
        "检索",
        "人格",
        "表情",
        "计划",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        database, store = await _open_store(Path(tmp) / "bench.db")
        try:
            rows = [
                {
                    "kind": "summary" if index % 3 else "fact",
                    "session_key": "bench:session",
                    "content": (
                        f"第 {index} 条记忆，讨论{topics[index % len(topics)]}与"
                        f"{topics[(index + 1) % len(topics)]}的实现细节"
                    ),
                    "ts": int(time.time()) - index * 60,
                    "importance": 0.5 + (index % 5) / 10,
                    "embedding": pack_vector(
                        [float((index + offset) % 7) for offset in range(16)]
                    ),
                    "embedding_model": "bench",
                    "embedding_dim": 16,
                }
                for index in range(args.rows)
            ]
            await store.insert_many(rows)
            latencies: list[float] = []
            for index in range(args.queries):
                query = f"{topics[index % len(topics)]} 的实现细节"
                vector = [float((index + offset) % 7) for offset in range(16)]
                started = time.perf_counter()
                await store.search(
                    "bench:session",
                    query=query,
                    vector=vector,
                    mode="hybrid",
                    limit=5,
                )
                latencies.append((time.perf_counter() - started) * 1000)
            latencies.sort()
            p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
            print(
                json.dumps(
                    {
                        "rows": args.rows,
                        "queries": args.queries,
                        "p50_ms": round(statistics.median(latencies), 2),
                        "p95_ms": round(p95, 2),
                        "max_ms": round(latencies[-1], 2),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if p95 < 300 else 2
        finally:
            await database.engine.dispose()


async def _run(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():  # noqa: ASYNC240  # filesystem probe before opening the DB
        print(f"database not found: {db_path}", file=sys.stderr)
        return 1
    if args.command == "bench":
        return await _bench(args)

    database, store = await _open_store(db_path)
    try:
        if args.command == "stats":
            print(json.dumps(await store.stats(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "search":
            hits = await store.search(
                args.umo, query=args.q, mode=args.mode, limit=args.limit
            )
            payload = [
                {
                    "kind": hit.get("kind"),
                    "content": hit.get("content"),
                    "score": hit.get("score"),
                    "ts": hit.get("ts"),
                }
                for hit in hits
            ]
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "import":
            result = await MemoryImporter(store).import_path(
                args.path,
                session_key=args.umo,
                default_kind=args.kind,
                dry_run=not args.apply,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not args.apply:
                print("dry run: pass --apply to write the rows", file=sys.stderr)
            return 0 if result["errors"] == 0 else 1

        if args.command == "export":
            rows = await store.export(args.umo)
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(  # noqa: ASYNC240  # one-off export write
                json.dumps(rows, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"exported {len(rows)} memories to {out}")
            return 0

        stats = await store.stats()
        problems: list[str] = []
        if stats["total"] and not stats["embedded"]:
            problems.append("memories exist but none is vectorized")
        if stats["total"] and stats["latest_ts"] is None:
            problems.append("memories exist but the newest timestamp is missing")
        report = {"stats": stats, "problems": problems}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if not problems else 2
    finally:
        await database.engine.dispose()


def main() -> int:
    """Run the CLI.

    Returns:
        Process exit code.
    """
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
