"""Normalize LKM message events into Life archive records.

The normalizer is deliberately duck-typed: it reads the public
``BotMessageEvent`` getters, so adapters and tests can pass a lightweight stub
without constructing a full event.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from bot.life.config import IngestConfig

_MAX_LIST_ITEMS = 50
_MAX_DEPTH = 4
_MAX_DICT_KEYS = 32


@dataclass(slots=True)
class IngestedMessage:
    """Normalized message ready for persistence."""

    platform_id: str
    session_key: str
    message_id: str
    ts: int
    sender_id: str | None
    sender_name: str | None
    group_id: str | None
    components: list = field(default_factory=list)
    plain_text: str = ""
    reply_to: str | None = None
    flags: dict = field(default_factory=dict)
    ingested_at: int = 0

    def to_row(self) -> dict[str, Any]:
        """Return the row payload used by the store.

        Returns:
            Mapping consumed by ``LifeStore.insert_batch``.
        """
        return {
            "platform_id": self.platform_id,
            "session_key": self.session_key,
            "message_id": self.message_id,
            "ts": self.ts,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "group_id": self.group_id,
            "components": self.components,
            "plain_text": self.plain_text,
            "reply_to": self.reply_to,
            "flags": self.flags,
            "ingested_at": self.ingested_at,
        }


def _sanitize(value: Any, max_chars: int, depth: int = 0) -> Any:
    """Recursively shrink a component payload into JSON-safe data.

    Args:
        value: Raw value from ``component.toDict()``.
        max_chars: Maximum length of a single string field.
        depth: Current recursion depth; guards against cyclic structures.

    Returns:
        JSON-serializable value with long strings and base64 folded.
    """
    if depth > _MAX_DEPTH:
        return "<truncated>"
    if isinstance(value, str):
        if value.startswith("base64://"):
            return f"base64://<{len(value) - 9} chars>"
        if len(value) > max_chars:
            return f"{value[:max_chars]}...<{len(value)} chars>"
        return value
    if isinstance(value, dict):
        items = list(value.items())[:_MAX_DICT_KEYS]
        return {str(key): _sanitize(item, max_chars, depth + 1) for key, item in items}
    if isinstance(value, list | tuple):
        items = [
            _sanitize(item, max_chars, depth + 1) for item in value[:_MAX_LIST_ITEMS]
        ]
        if len(value) > _MAX_LIST_ITEMS:
            items.append(f"<+{len(value) - _MAX_LIST_ITEMS} more>")
        return items
    if isinstance(value, bool | int | float) or value is None:
        return value
    return str(value)[:max_chars]


def _component_payload(
    component: Any,
    max_chars: int,
    store_components: bool,
) -> dict:
    """Convert one message component into a compact payload.

    Args:
        component: Component instance exposing ``toDict()``.
        max_chars: Maximum length of a single string field.
        store_components: When False only the component type is kept.

    Returns:
        JSON-safe component payload.
    """
    try:
        raw = component.toDict()
    except Exception:
        return {"type": "unknown", "data": {}}
    if not isinstance(raw, dict):
        raw = {"type": "unknown", "data": {"value": str(raw)}}
    if not store_components:
        return {"type": str(raw.get("type", "unknown"))}
    payload = _sanitize(raw, max_chars)
    if not isinstance(payload, dict):
        return {"type": "unknown", "data": {"value": payload}}
    return payload


def normalize_event(event: Any, ingest: IngestConfig) -> IngestedMessage:
    """Build an archive record from a ``BotMessageEvent``-like object.

    Args:
        event: Message event exposing the public LKM event getters.
        ingest: Archive settings controlling truncation and component storage.

    Returns:
        Normalized message ready for persistence.
    """
    created_at = float(getattr(event, "created_at", time.time()))
    message_obj = getattr(event, "message_obj", None)
    message_id = str(getattr(message_obj, "message_id", "") or "")
    session_key = str(event.unified_msg_origin)
    if not message_id:
        message_id = f"auto:{session_key}:{int(created_at * 1000)}"

    raw_timestamp = getattr(message_obj, "timestamp", 0) or created_at
    try:
        ts = int(raw_timestamp)
    except (TypeError, ValueError):
        ts = int(created_at)

    components: list[dict] = []
    reply_to: str | None = None
    try:
        raw_components = event.get_messages()
    except Exception:
        raw_components = []
    for component in raw_components:
        payload = _component_payload(
            component,
            ingest.max_component_chars,
            ingest.store_components,
        )
        components.append(payload)
        if payload.get("type") == "reply" and reply_to is None:
            data = payload.get("data")
            if isinstance(data, dict) and data.get("id") not in (None, ""):
                reply_to = str(data["id"])

    text = event.get_message_str()
    plain_text = str(text)[: ingest.max_text_chars] if text else ""

    component_types = {str(item.get("type", "")) for item in components}
    message_type = getattr(event.get_message_type(), "value", "")
    flags = {
        "at": "at" in component_types,
        "at_all": "at" in component_types
        and any(
            item.get("data", {}).get("qq") == "all"
            for item in components
            if item.get("type") == "at"
        ),
        "reply": "reply" in component_types,
        "image": "image" in component_types,
        "record": "record" in component_types,
        "video": "video" in component_types,
        "file": "file" in component_types,
        "json": "json" in component_types,
        "is_group": message_type == "GroupMessage",
        "is_private": message_type == "FriendMessage",
        "is_command": plain_text.lstrip().startswith("/"),
        "is_at_or_wake_command": bool(getattr(event, "is_at_or_wake_command", False)),
        "is_wake": bool(getattr(event, "is_wake", False)),
    }

    group_id = event.get_group_id() or None
    return IngestedMessage(
        platform_id=str(event.get_platform_id()),
        session_key=session_key,
        message_id=message_id,
        ts=ts,
        sender_id=event.get_sender_id() or None,
        sender_name=event.get_sender_name() or None,
        group_id=group_id,
        components=components,
        plain_text=plain_text,
        reply_to=reply_to,
        flags=flags,
        ingested_at=int(time.time()),
    )
