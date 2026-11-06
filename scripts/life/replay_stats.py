"""Compute baseline statistics for a Life replay corpus.

Example:
    python scripts/life/replay_stats.py data/life/replay/replay.jsonl \
      --json-out data/life/replay/corpus.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot.life.replay.corpus import corpus_stats, load_sessions  # noqa: E402


def main() -> int:
    """Run the statistics pass.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", help="Path to replay.jsonl")
    parser.add_argument("--json-out", default="", help="Optional JSON output path")
    args = parser.parse_args()

    try:
        sessions = load_sessions(args.corpus)
    except FileNotFoundError:
        print(f"corpus not found: {args.corpus}", file=sys.stderr)
        return 1

    summary = corpus_stats(sessions)
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
