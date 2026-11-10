"""Agent tool for sending stickers.

The model decides *when* a sticker fits; the service enforces the cooldown and
hourly cap, so a chatty model cannot turn the group into a sticker flood.
"""

from __future__ import annotations

from typing import Any

from bot import logger
from bot.life.common import event_session_enabled, event_umo, unregister_tools

_TOOL_NAMES = ("send_meme",)


def register_emoji_tools(manager: Any, service: Any) -> None:
    """Register the sticker tool on the core tool manager.

    Args:
        manager: ``FunctionToolManager`` exposed by the provider manager.
        service: Emoji service that performs selection and sending.
    """

    async def send_meme(event: Any, description: str = "") -> str:
        """Send one sticker that fits the current mood."""
        if not service.enabled:
            return "error: sticker library is not enabled"
        umo = event_umo(event)
        if not umo:
            return "error: session is unknown"
        if not event_session_enabled(service, event):
            return "error: sticker library is not enabled for this session"
        result = await service.send(umo, description or "当前对话")
        if result.get("ok"):
            return "sent"
        return f"error: {result.get('error') or 'send failed'}"

    try:
        manager.add_func(
            name="send_meme",
            func_args=[
                {
                    "type": "string",
                    "name": "description",
                    "description": (
                        "What the conversation is about, used to pick a sticker."
                    ),
                },
            ],
            desc=(
                "Send one sticker/emoji when it fits the mood better than words. "
                "Respect cooldowns: do not call it repeatedly."
            ),
            handler=send_meme,
        )
    except Exception:
        logger.exception("[life] failed to register emoji tool")


def unregister_emoji_tools(manager: Any) -> None:
    """Remove the sticker tool from the core tool manager.

    Args:
        manager: ``FunctionToolManager`` exposed by the provider manager.
    """
    unregister_tools(manager, _TOOL_NAMES, "emoji")
