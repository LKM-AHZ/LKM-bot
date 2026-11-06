"""Replay a Life corpus through a decision policy and emit a decision stream.

The runner is the S0 "one-click replay" entry point. A decision stream is the
comparable artifact between MaiBot and LKM-bot: the same corpus, the same
per-message keys, different policies. Import an external stream (for example a
MaiBot run) by writing it in the same JSONL shape and feeding it to
``life_metrics.py``.

Examples:
    python scripts/life/replay_runner.py data/life/replay/replay.jsonl
    python scripts/life/replay_runner.py data/life/replay/replay.jsonl \
      --decider silent --out data/life/replay/decisions_silent.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot.life.replay.corpus import (  # noqa: E402
    load_sessions,
    message_text,
    message_timestamp,  # noqa: E402
)

_QUESTION_MARKS = ("?", "？")


def _decide_keyword(text: str, wake_words: list[str]) -> tuple[str, str]:
    """Apply the naive keyword policy to one message.

    Args:
        text: Message text.
        wake_words: Words that trigger a reply.

    Returns:
        ``(decision, reason)`` where decision is ``reply`` or ``wait``.
    """
    lowered = text.lower()
    for word in wake_words:
        if word and word.lower() in lowered:
            return "reply", "wake_word"
    if text.rstrip().endswith(_QUESTION_MARKS):
        return "reply", "question"
    return "wait", "no_trigger"


def main() -> int:
    """Replay the corpus and write the decision stream.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", help="Path to replay.jsonl")
    parser.add_argument(
        "--out",
        default="data/life/replay/decisions.jsonl",
        help="Decision stream output path",
    )
    parser.add_argument(
        "--decider",
        default="keyword",
        choices=["keyword", "silent", "always"],
        help="Policy used to decide whether to reply",
    )
    parser.add_argument(
        "--wake-words",
        default="bot,机器人,小助手",
        help="Comma-separated trigger words for the keyword policy",
    )
    parser.add_argument(
        "--latency-ms",
        type=float,
        default=0.0,
        help="Synthetic per-message latency recorded for P95 tracking",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max sessions (0 = all)")
    args = parser.parse_args()

    try:
        sessions = load_sessions(args.corpus)
    except FileNotFoundError:
        print(f"corpus not found: {args.corpus}", file=sys.stderr)
        return 1
    if args.limit > 0:
        sessions = sessions[: args.limit]

    wake_words = [word.strip() for word in args.wake_words.split(",") if word.strip()]
    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    replies = 0
    messages_total = 0
    with output_path.open("w", encoding="utf-8") as file:
        for session_index, session in enumerate(sessions):
            session_key = str(session.get("session_key") or f"session-{session_index}")
            for message_index, message in enumerate(session.get("messages") or []):
                if not isinstance(message, dict):
                    continue
                text = message_text(message)
                if args.decider == "silent":
                    decision, reason = "wait", "policy_silent"
                elif args.decider == "always":
                    decision, reason = "reply", "policy_always"
                else:
                    decision, reason = _decide_keyword(text, wake_words)
                record = {
                    "session_key": session_key,
                    "message_id": f"m{message_index}",
                    "ts": message_timestamp(message),
                    "decide": decision,
                    "reason": reason,
                    "latency_ms": args.latency_ms,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "memory_hits": 0,
                    "cited": False,
                }
                file.write(json.dumps(record, ensure_ascii=False))
                file.write("\n")
                messages_total += 1
                replies += 1 if decision == "reply" else 0

    print(f"sessions: {len(sessions)}")
    print(f"messages: {messages_total}")
    print(f"replies: {replies}")
    print(f"written: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
