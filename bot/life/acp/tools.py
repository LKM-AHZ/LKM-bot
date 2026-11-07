"""Agent tool that delegates one question to the external ACP agent (S6d)."""

from __future__ import annotations

from typing import Any

from bot import logger

_TOOL_NAMES = ("ask_external_agent",)


def _session_enabled(service: Any, event: Any) -> bool:
    """Return whether the event's session may use the ACP agent.

    Args:
        service: Life service owning the configuration.
        event: Bot message event.

    Returns:
        True when the session is inside the Life scope.
    """
    umo = getattr(event, "unified_msg_origin", "") or ""
    if not umo:
        return False
    getter = getattr(event, "get_group_id", None)
    group_id = getter() if callable(getter) else None
    return service.config.session_enabled(umo, group_id)


def register_acp_tools(manager: Any, service: Any) -> None:
    """Register ``ask_external_agent`` on the core tool manager.

    Args:
        manager: ``FunctionToolManager`` exposed by the provider manager.
        service: Life service that owns the ACP adapter.
    """

    async def ask_external_agent(event: Any, question: str) -> str:
        """Ask the external ACP agent one self-contained question."""
        if service.acp is None:
            return "error: external agent is not enabled"
        if not _session_enabled(service, event):
            return "error: external agent is not enabled for this session"
        umo = getattr(event, "unified_msg_origin", "") or ""
        answer = await service.acp.ask(umo, str(question or ""))
        if not answer:
            return "error: the external agent did not answer"
        return answer

    try:
        manager.add_func(
            name="ask_external_agent",
            func_args=[
                {
                    "type": "string",
                    "name": "question",
                    "description": (
                        "A self-contained question; the external agent cannot see "
                        "this chat, so include the context it needs."
                    ),
                },
            ],
            desc=(
                "Delegate one question to the external ACP agent and get its answer. "
                "Use it for work that needs that agent's tools or environment."
            ),
            handler=ask_external_agent,
        )
    except Exception:
        logger.exception("[life] failed to register ask_external_agent")


def unregister_acp_tools(manager: Any) -> None:
    """Remove the ACP tool from the core tool manager.

    Args:
        manager: ``FunctionToolManager`` exposed by the provider manager.
    """
    for name in _TOOL_NAMES:
        try:
            manager.remove_func(name)
        except Exception:
            logger.debug(f"[life] failed to remove ACP tool {name}")
