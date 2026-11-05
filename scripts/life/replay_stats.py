"""Compute baseline statistics for a Life replay corpus.

Example:
    python scripts/life/replay_stats.py data/life/replay/replay.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


def _parse_timestamp(value: object) -> float | None:
    """Convert a stored timestamp into epoch seconds.

    Args:
        value: Integer epoch, float epoch, or ISO-like string.

    Returns:
        Epoch seconds, or None when the value cannot be parsed.
    """
    if isinstance(value, bool | int | float):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _percentile(values: list[float], ratio: float) -> float:
    """Return a simple nearest-rank percentile.

    Args:
        values: Non-empty sample list.
        ratio: Percentile ratio in [0, 1].

    Returns:
        Selected value.
    """
    ordered = sorted(values)
    index = min(int(len(ordered) * ratio), len(ordered) - 1)
    return ordered[index]


def main() -> int:
    """Run the statistics pass.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", help="Path to replay.jsonl")
    parser.add_argument("--json-out", default="", help="Optional JSON output path")
    args = parser.parse_args()

    corpus_path = Path(args.corpus)
    if not corpus_path.exists():
        print(f"corpus not found: {corpus_path}", file=sys.stderr)
        return 1

    session_count = 0
    message_counts: list[int] = []
    gaps: list[float] = []
    roles: Counter[str] = Counter()
    part_types: Counter[str] = Counter()
    sessions_with_media = 0

    with corpus_path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            session = json.loads(line)
            messages = session.get("messages", [])
            session_count += 1
            message_counts.append(len(messages))

            timestamps: list[float] = []
            has_media = False
            for message in messages:
                roles[str(message.get("role", ""))] += 1
                for part in message.get("parts", []):
                    part_type = str(part.get("type", ""))
                    part_types[part_type] += 1
                    if part_type in {"image", "record", "video", "file"}:
                        has_media = True
                ts = _parse_timestamp(message.get("created_at"))
                if ts is not None:
                    timestamps.append(ts)
            timestamps.sort()
            gaps.extend(
                later - earlier
                for earlier, later in zip(timestamps, timestamps[1:])
                if later >= earlier
            )
            if has_media:
                sessions_with_media += 1

    total_messages = sum(message_counts)
    summary = {
        "sessions": session_count,
        "messages": total_messages,
        "messages_per_session_avg": round(total_messages / session_count, 2)
        if session_count
        else 0,
        "messages_per_session_median": statistics.median(message_counts)
        if message_counts
        else 0,
        "gap_p50_seconds": round(_percentile(gaps, 0.5), 2) if gaps else None,
        "gap_p90_seconds": round(_percentile(gaps, 0.9), 2) if gaps else None,
        "roles": dict(roles),
        "part_types": dict(part_types.most_common()),
        "sessions_with_media": sessions_with_media,
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.json_out:
        output_path = Path(args.json_out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
