"""Agent tools backed by long-term memory.

The tools are registered on the core ``llm_tools`` manager only while memory is
enabled, and every handler re-checks the session allowlist so a gray-release
deployment never exposes memory tools to sessions that are not part of it.
"""

from __future__ import annotations

import json
from typing import Any

from bot import logger

_TOOL_NAMES = ("search_memory", "remember", "get_person_profile")


def _session_enabled(service: Any, event: Any) -> bool:
    """Return whether the event's session is inside the Life scope.

    Args:
        service: Life service owning the configuration.
        event: Bot message event.

    Returns:
        True when the session is enabled.
    """
    umo = getattr(event, "unified_msg_origin", "") or ""
    if not umo:
        return False
    getter = getattr(event, "get_group_id", None)
    group_id = getter() if callable(getter) else None
    return service.config.session_enabled(umo, group_id)


def register_memory_tools(manager: Any, service: Any) -> None:
    """Register the memory tools on the core tool manager.

    Args:
        manager: ``FunctionToolManager`` exposed by the provider manager.
        service: Life service that owns the memory adapter.
    """

    async def search_memory(
        event: Any,
        query: str,
        mode: str = "hybrid",
        limit: int = 5,
    ) -> str:
        """Search this chat's long-term memory."""
        if service.memory is None or not _session_enabled(service, event):
            return "error: long-term memory is not enabled for this session"
        hits = await service.memory.search(
            query=query,
            session_key=event.unified_msg_origin,
            mode=mode,
            limit=max(1, min(int(limit), 20)),
        )
        if not hits:
            return "No matching memory."
        return json.dumps(
            [
                {
                    "kind": hit.get("kind"),
                    "content": hit.get("content"),
                    "score": hit.get("score"),
                    "ts": hit.get("ts"),
                }
                for hit in hits
            ],
            ensure_ascii=False,
        )

    async def remember(event: Any, content: str, importance: float = 0.6) -> str:
        """Store one memory in this chat's long-term memory."""
        if service.memory is None or not _session_enabled(service, event):
            return "error: long-term memory is not enabled for this session"
        inserted = await service.memory.remember(
            content=content,
            session_key=event.unified_msg_origin,
            kind="note",
            importance=float(importance),
        )
        return "saved" if inserted else "duplicate: already remembered"

    async def get_person_profile(event: Any, person: str) -> str:
        """Return what the bot remembers about one person."""
        if service.memory is None or not _session_enabled(service, event):
            return "error: long-term memory is not enabled for this session"
        hits = await service.memory.search(
            query=person,
            session_key=event.unified_msg_origin,
            mode="hybrid",
            limit=8,
            kinds=["fact"],
        )
        facts = [hit for hit in hits if hit.get("person_id") == person]
        if not facts:
            facts = hits[:3]
        if not facts:
            return f"No profile is available for {person} yet."
        return json.dumps(
            [{"content": hit.get("content")} for hit in facts],
            ensure_ascii=False,
        )

    try:
        manager.add_func(
            name="search_memory",
            func_args=[
                {
                    "type": "string",
                    "name": "query",
                    "description": "What to look up in long-term memory.",
                },
                {
                    "type": "string",
                    "name": "mode",
                    "description": (
                        "Retrieval mode: search, time, hybrid, episode or aggregate."
                    ),
                },
                {
                    "type": "integer",
                    "name": "limit",
                    "description": "Maximum number of memories to return.",
                },
            ],
            desc=(
                "Search the long-term memory of this chat. Use it when the user "
                "refers to earlier conversations, past decisions or people you "
                "should already know about."
            ),
            handler=search_memory,
        )
        manager.add_func(
            name="remember",
            func_args=[
                {
                    "type": "string",
                    "name": "content",
                    "description": "The fact or preference worth remembering.",
                },
                {
                    "type": "number",
                    "name": "importance",
                    "description": "How important it is, from 0 to 1.",
                },
            ],
            desc=(
                "Store one durable fact or preference in long-term memory when "
                "the user asks you to remember something."
            ),
            handler=remember,
        )
        manager.add_func(
            name="get_person_profile",
            func_args=[
                {
                    "type": "string",
                    "name": "person",
                    "description": "Display name or id of the person.",
                },
            ],
            desc="Look up what is remembered about one person in this chat.",
            handler=get_person_profile,
        )
    except Exception:
        logger.exception("[life] failed to register memory tools")


def unregister_memory_tools(manager: Any) -> None:
    """Remove the memory tools from the core tool manager.

    Args:
        manager: ``FunctionToolManager`` exposed by the provider manager.
    """
    for name in _TOOL_NAMES:
        try:
            manager.remove_func(name)
        except Exception:
            logger.debug(f"[life] failed to remove memory tool {name}")
