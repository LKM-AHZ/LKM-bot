"""Prompt hook that injects Life context into LLM requests.

The core pipeline calls :func:`inject_life_context` right before a provider
request is sent. The hook stays tiny: an ordered list of block owners lets each
service append its own section, so the core keeps a single call site and the
Life layer owns every decision about what enters the prompt.

Order is meaningful, not alphabetical: persona first (its service prepends), then
memory, expressions, slang, focus, attention and delivered sub-agent conclusions.
S5b turns this fixed list into an explicit assembly step with a shared budget.
"""

from __future__ import annotations

from typing import Any

from bot import logger
from bot.life.service import get_life_service

INJECTOR_ORDER = (
    "persona",
    "memory",
    "expression",
    "jargon",
    "focus",
    "attention",
    "subagent",
)


async def inject_life_context(event: Any, req: Any) -> bool:
    """Inject persona, memory, expression, jargon, focus, attention and sub-agent.

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
    for name in INJECTOR_ORDER:
        injector = getattr(service, name, None)
        if injector is None:
            continue
        try:
            changed = await injector.inject(event, req) or changed
        except Exception:
            # Life context is an enhancement: a failure must never break the reply.
            logger.exception("[life] failed to inject %s context", name)
    assembler = getattr(service, "prompt", None)
    if assembler is not None and getattr(req, "system_prompt", ""):
        umo = getattr(event, "unified_msg_origin", "") or ""
        req.system_prompt = assembler.assemble(req.system_prompt, umo)
    return changed
