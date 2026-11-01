import json

from bot import logger
from bot.core.conversation_mgr import ConversationManager
from bot.core.platform.bot_message_event import BotMessageEvent
from bot.core.provider.entities import ProviderRequest


async def persist_agent_history(
    conversation_manager: ConversationManager,
    *,
    event: BotMessageEvent,
    req: ProviderRequest,
    summary_note: str,
) -> None:
    """Persist agent interaction into conversation history."""
    if not req or not req.conversation:
        return

    history = []
    try:
        history = json.loads(req.conversation.history or "[]")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse conversation history: %s", exc)
    history.append({"role": "user", "content": "Output your last task result below."})
    history.append({"role": "assistant", "content": summary_note})
    await conversation_manager.update_conversation(
        event.unified_msg_origin,
        req.conversation.cid,
        history=history,
    )
