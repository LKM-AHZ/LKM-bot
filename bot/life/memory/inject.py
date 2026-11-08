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
    trace = getattr(event, "trace", None)
    if trace is not None:
        try:
            tools: list[str] = []
            tool_set = getattr(req, "func_tool", None)
            if tool_set is not None:
                tools = list(tool_set.names())
            trace.record(
                "life_request",
                session=str(getattr(event, "unified_msg_origin", "") or ""),
                model=str(getattr(req, "model", "") or ""),
                tools=tools,
                system_prompt=str(getattr(req, "system_prompt", "") or ""),
                prompt=str(getattr(req, "prompt", "") or ""),
            )
        except Exception:
            logger.exception("[life] trace request snapshot failed")
    config = getattr(service, "config", None)
    creative = getattr(config, "creative", None)
    # Translation lives in the reply layer; without it the foreign-language
    # instruction would leak straight to the user.
    if (
        creative is not None
        and getattr(creative, "enabled", False)
        and getattr(service, "reply", None) is not None
    ):
        from bot.life.reply.creative import creative_instruction

        instruction = creative_instruction(creative.language)
        if instruction and instruction not in (req.system_prompt or ""):
            req.system_prompt = f"{req.system_prompt}\n{instruction}\n"
            changed = True
    return changed
