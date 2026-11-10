"""Agent tool that delegates one question to the external ACP agent (S6d)."""

from __future__ import annotations

from typing import Any

from bot import logger
from bot.life.common import event_session_enabled, unregister_tools

_TOOL_NAMES = ("ask_external_agent",)


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
        if not event_session_enabled(service, event):
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
    unregister_tools(manager, _TOOL_NAMES, "ACP")
