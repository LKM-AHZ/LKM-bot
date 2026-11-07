"""Replay a Life corpus through a decision policy and emit a decision stream.

The runner is the S0 "one-click replay" entry point. A decision stream is the
comparable artifact between MaiBot and LKM-bot: the same corpus, the same
per-message keys, different policies. Import an external stream (for example a
MaiBot run) by writing it in the same JSONL shape and feeding it to
``life_metrics.py``.

The ``heart_flow`` decider runs the real S4 decision path (debounce + gate), so
its reply/no-reply F1 is the number the acceptance target talks about. It only
skips the language model: the gate, not the planner, decides whether a turn is
answered.

Examples:
    python scripts/life/replay_runner.py data/life/replay/replay.jsonl
    python scripts/life/replay_runner.py data/life/replay/replay.jsonl \
      --decider heart_flow --out data/life/replay/decisions_heart_flow.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot.life.config import HeartFlowConfig  # noqa: E402
from bot.life.heartflow.policy import (  # noqa: E402
    BOT_ROLES,
    HeartFlowPolicy,
    PolicyMessage,
)
from bot.life.replay.corpus import (  # noqa: E402
    load_sessions,
    message_text,
    message_timestamp,
)

_QUESTION_MARKS = ("?", "？")
_AT_PART = "at"
_REPLY_PART = "reply"
_IMAGE_PARTS = frozenset({"image", "sticker"})


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


def _part_target(part: Any) -> str:
    """Return the target id of an at part.

    Args:
        part: Component payload.

    Returns:
        Target id, or an empty string when the part has none.
    """
    if not isinstance(part, dict):
        return ""
    data = part.get("data")
    if not isinstance(data, dict):
        return ""
    for key in ("qq", "user_id", "id", "target"):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _corpus_flags(
    parts: list[Any],
    text: str,
    bot_ids: set[str],
) -> dict:
    """Derive the flags the gate needs from a corpus message.

    ``reply_to_bot`` cannot be derived offline: the corpus keeps the reply
    target id but not the author of that target. It stays False here and is set
    by the runtime once message ids are known.

    Args:
        parts: Message parts.
        text: Plain text of the message.
        bot_ids: Platform ids of the bot; empty means any at counts as the bot.

    Returns:
        Flag mapping compatible with ``PolicyMessage.flags``.
    """
    at_targets = [
        _part_target(part)
        for part in parts
        if isinstance(part, dict) and part.get("type") == _AT_PART
    ]
    at_bot = (
        any(target in bot_ids for target in at_targets) if bot_ids else bool(at_targets)
    )
    mentions_other = any(target and target not in bot_ids for target in at_targets)
    return {
        "at": at_bot,
        "mentions_other": mentions_other,
        "reply": any(
            isinstance(part, dict) and part.get("type") == _REPLY_PART for part in parts
        ),
        "reply_to_bot": False,
        "image": any(
            isinstance(part, dict) and part.get("type") in _IMAGE_PARTS
            for part in parts
        ),
        "is_command": text.lstrip().startswith("/"),
    }


def _heart_flow_records(
    session: dict[str, Any],
    policy: HeartFlowPolicy,
    bot_ids: set[str],
    latency_ms: float,
) -> list[dict]:
    """Replay one corpus session through the heart-flow policy.

    Args:
        session: Corpus session dictionary.
        policy: Decision policy.
        bot_ids: Platform ids of the bot.
        latency_ms: Synthetic decision latency recorded per message.

    Returns:
        Decision records in corpus order.
    """
    session_key = str(session.get("session_key") or "")
    messages: list[PolicyMessage] = []
    last_ts = 0.0
    for index, raw in enumerate(session.get("messages") or []):
        if not isinstance(raw, dict):
            continue
        text = message_text(raw)
        ts = message_timestamp(raw)
        if ts is None:
            # A message without a timestamp inherits the previous one so the
            # debounce window can still merge it into the same turn.
            ts = last_ts
        last_ts = max(last_ts, ts)
        parts = raw.get("parts") or []
        messages.append(
            PolicyMessage(
                message_id=f"m{index}",
                ts=ts,
                sender_id=str(raw.get("sender_id") or "") or None,
                sender_name=str(raw.get("sender_name") or "") or None,
                text=text,
                is_bot=str(raw.get("role") or "") in BOT_ROLES,
                flags=_corpus_flags(parts, text, bot_ids),
            )
        )
    is_private = "GroupMessage" not in session_key
    decisions = policy.decide_session(messages, is_private=is_private)
    records: list[dict] = []
    for decision in decisions:
        records.append(
            {
                "session_key": session_key,
                "message_id": decision.message_id,
                "ts": decision.release_at,
                "decide": decision.decide,
                "reason": decision.reason,
                "tier": decision.tier,
                "likelihood": decision.likelihood,
                "threshold": decision.threshold,
                "latency_ms": latency_ms,
                "tokens_in": 0,
                "tokens_out": 0,
                "memory_hits": 0,
                "cited": False,
            }
        )
    return records


def _build_policy(args: argparse.Namespace, wake_words: list[str]) -> HeartFlowPolicy:
    """Build the heart-flow policy from command-line overrides.

    Args:
        args: Parsed arguments.
        wake_words: Names and wake words that address the bot.

    Returns:
        Configured policy.
    """
    config = HeartFlowConfig(enabled=True)
    if args.gate_threshold is not None:
        config.gate.base_threshold = args.gate_threshold
    if args.no_debounce:
        config.debounce.enabled = False
    return HeartFlowPolicy(config, bot_names=wake_words)


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
        choices=["keyword", "silent", "always", "heart_flow"],
        help="Policy used to decide whether to reply",
    )
    parser.add_argument(
        "--wake-words",
        default="bot,机器人,小助手",
        help="Comma-separated names and wake words that address the bot",
    )
    parser.add_argument(
        "--bot-ids",
        default="",
        help=(
            "Comma-separated platform ids of the bot; empty treats every at as "
            "addressing the bot"
        ),
    )
    parser.add_argument(
        "--gate-threshold",
        type=float,
        default=None,
        help="Override gate.base_threshold for tuning runs",
    )
    parser.add_argument(
        "--no-debounce",
        action="store_true",
        help="Disable debounce (decide every message immediately)",
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
    bot_ids = {item.strip() for item in args.bot_ids.split(",") if item.strip()}
    policy = _build_policy(args, wake_words)
    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    replies = 0
    messages_total = 0
    with output_path.open("w", encoding="utf-8") as file:
        for session_index, session in enumerate(sessions):
            if args.decider == "heart_flow":
                records = _heart_flow_records(
                    session,
                    policy,
                    bot_ids,
                    args.latency_ms,
                )
                for record in records:
                    file.write(json.dumps(record, ensure_ascii=False))
                    file.write("\n")
                messages_total += len(records)
                replies += sum(1 for item in records if item["decide"] == "reply")
                continue
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
