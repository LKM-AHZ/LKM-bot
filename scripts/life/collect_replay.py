"""Export LKMBot chat history into a replay corpus for the Life migration.

The corpus is a JSONL file with one session per line::

    {"session_key": "...", "platform_id": "...", "messages": [...]}

Each message keeps the stored role, sender, parts and timestamp so the replay
tools can reconstruct decision points (speak / stay silent) later.

Example:
    python scripts/life/collect_replay.py --db data/data_v4.db --out data/life/replay
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/data_v4.db", help="Path to data_v4.db")
    parser.add_argument(
        "--out",
        default="data/life/replay",
        help="Output directory for replay.jsonl",
    )
    parser.add_argument("--sessions", type=int, default=300)
    parser.add_argument("--min-messages", type=int, default=50)
    parser.add_argument("--max-messages", type=int, default=200)
    return parser.parse_args()


def main() -> int:
    """Run the exporter.

    Returns:
        Process exit code.
    """
    args = _parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 1

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT platform_id, user_id, sender_id, sender_name, content, created_at
            FROM platform_message_history
            ORDER BY user_id, id
            """,
        ).fetchall()
    finally:
        connection.close()

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        try:
            content = json.loads(row["content"]) if row["content"] else {}
        except (TypeError, json.JSONDecodeError):
            continue
        grouped[(row["platform_id"], row["user_id"])].append(
            {
                "role": str(content.get("type", "")),
                "sender_id": row["sender_id"],
                "sender_name": row["sender_name"],
                "parts": content.get("message", []),
                "created_at": row["created_at"],
            },
        )

    selected = [
        (key, messages[-args.max_messages :])
        for key, messages in grouped.items()
        if len(messages) >= args.min_messages
    ]
    selected.sort(key=lambda item: len(item[1]), reverse=True)
    selected = selected[: args.sessions]

    output_dir = Path(args.out)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "replay.jsonl"
    with output_path.open("w", encoding="utf-8") as file:
        for (platform_id, session_key), messages in selected:
            record = {
                "platform_id": platform_id,
                "session_key": session_key,
                "messages": messages,
            }
            file.write(json.dumps(record, ensure_ascii=False))
            file.write("\n")

    message_counts = [len(messages) for _, messages in selected]
    print(f"sessions: {len(selected)}")
    print(f"messages: {sum(message_counts)}")
    if message_counts:
        print(f"median messages/session: {statistics.median(message_counts):.0f}")
    print(f"written: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
