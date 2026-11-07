"""Prompt hook that injects Life context into LLM requests.

The core pipeline calls :func:`inject_life_context` right before a provider
request is sent. The hook stays tiny: it resolves the Life service and lets the
memory adapter and the expression service each append their own block, so the
core keeps a single call site and the Life layer owns every decision about what
enters the prompt.
"""

from __future__ import annotations

from typing import Any

from bot import logger
from bot.life.service import get_life_service


async def inject_life_context(event: Any, req: Any) -> bool:
    """Inject memory and expression context into a provider request.

    Args:
        event: Bot message event being processed.
        req: ``ProviderRequest`` about to be sent to the model.

    Returns:
        True when the system prompt was modified.
    """
    service = get_life_service()
    if service is None:
        return False
    changed = False
    for injector in (service.memory, service.expression):
        if injector is None:
            continue
        try:
            changed = await injector.inject(event, req) or changed
        except Exception:
            # Life context is an enhancement: a failure must never break the reply.
            logger.exception("[life] failed to inject life context")
    return changed
