"""Load a Life replay corpus and derive its baseline statistics.

The corpus format is one JSON object per line::

    {"platform_id": "...", "session_key": "...", "messages": [...]}

Each message keeps ``role``, ``parts`` and a timestamp in ``created_at``. The
helpers here are shared by the collector, the statistics CLI, the replay runner
and the metrics CLI so all four agree on parsing.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from bot.life.replay.metrics import percentile

_MEDIA_TYPES = frozenset({"image", "record", "video", "file"})


def parse_timestamp(value: object) -> float | None:
    """Convert a stored timestamp into epoch seconds.

    Args:
        value: Integer epoch, float epoch, or ISO-like string.

    Returns:
        Epoch seconds, or None when the value cannot be parsed.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def message_timestamp(message: dict[str, Any]) -> float | None:
    """Return the epoch timestamp of one corpus message.

    Args:
        message: Message dictionary.

    Returns:
        Epoch seconds, or None when the message has no parsable timestamp.
    """
    return parse_timestamp(message.get("created_at") or message.get("ts"))


def part_text(part: Any) -> str:
    """Extract readable text from one message part.

    Args:
        part: Component payload, usually ``{"type": "plain", "text": "..."}``.

    Returns:
        Text of the part, or an empty string for media and unknown parts.
    """
    if not isinstance(part, dict):
        return ""
    for key in ("text", "content"):
        value = part.get(key)
        if isinstance(value, str) and value:
            return value
    data = part.get("data")
    if isinstance(data, dict):
        for key in ("text", "content"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def message_text(message: dict[str, Any]) -> str:
    """Join the text parts of one message.

    Args:
        message: Message dictionary.

    Returns:
        Concatenated text without surrounding whitespace.
    """
    parts = message.get("parts") or []
    text = " ".join(part_text(part) for part in parts).strip()
    if text:
        return text
    for key in ("text", "content"):
        value = message.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def iter_sessions(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield corpus sessions from a JSONL file.

    Args:
        path: ``replay.jsonl`` path.

    Yields:
        Session dictionaries; malformed lines are skipped.

    Raises:
        FileNotFoundError: When the corpus file does not exist.
    """
    corpus_path = Path(path)
    if not corpus_path.exists():
        raise FileNotFoundError(corpus_path)
    with corpus_path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                session = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(session, dict):
                yield session


def load_sessions(path: str | Path) -> list[dict[str, Any]]:
    """Load every corpus session into memory.

    Args:
        path: ``replay.jsonl`` path.

    Returns:
        Session dictionaries in file order.
    """
    return list(iter_sessions(path))


def corpus_stats(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize a replay corpus.

    Args:
        sessions: Loaded corpus sessions.

    Returns:
        Mapping with session/message counts, gap percentiles, role counts, part
        type counts and how many sessions contain media.
    """
    message_counts: list[int] = []
    gaps: list[float] = []
    roles: Counter[str] = Counter()
    part_types: Counter[str] = Counter()
    sessions_with_media = 0

    for session in sessions:
        messages = session.get("messages") or []
        message_counts.append(len(messages))
        timestamps: list[float] = []
        has_media = False
        for message in messages:
            if not isinstance(message, dict):
                continue
            roles[str(message.get("role", ""))] += 1
            for part in message.get("parts") or []:
                part_type = str(part.get("type", "")) if isinstance(part, dict) else ""
                part_types[part_type] += 1
                if part_type in _MEDIA_TYPES:
                    has_media = True
            ts = message_timestamp(message)
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
    return {
        "sessions": len(sessions),
        "messages": total_messages,
        "messages_per_session_avg": (
            round(total_messages / len(sessions), 2) if sessions else 0
        ),
        "messages_per_session_median": (
            statistics.median(message_counts) if message_counts else 0
        ),
        "gap_p50_seconds": percentile(gaps, 0.5) if gaps else None,
        "gap_p90_seconds": percentile(gaps, 0.9) if gaps else None,
        "roles": dict(roles),
        "part_types": dict(part_types.most_common()),
        "sessions_with_media": sessions_with_media,
    }
