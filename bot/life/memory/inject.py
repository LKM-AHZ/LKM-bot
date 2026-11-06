"""Prompt hook that injects long-term memory into LLM requests.

The core pipeline calls :func:`inject_memory_context` right before a provider
request is sent. The hook is deliberately tiny: it resolves the Life service and
delegates to the memory adapter, so the core keeps a single call site and the
Life layer owns every decision about what enters the prompt.
"""

from __future__ import annotations

from typing import Any

from bot import logger
from bot.life.service import get_life_service


async def inject_memory_context(event: Any, req: Any) -> bool:
    """Inject retrieved memories into a provider request.

    Args:
        event: Bot message event being processed.
        req: ``ProviderRequest`` about to be sent to the model.

    Returns:
        True when the system prompt was modified.
    """
    service = get_life_service()
    if service is None or service.memory is None:
        return False
    try:
        return await service.memory.inject(event, req)
    except Exception:
        # Memory is an enhancement: a failure must never break the reply.
        logger.exception("[life] failed to inject memory context")
        return False
