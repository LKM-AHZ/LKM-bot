"""Super creative reply mode (MaiBot 1.4 experimental).

The trick upstream uses is not a different template: the model writes the reply
in another language (or classical Chinese), which breaks its Chinese "assistant"
phrasing, and the same model then translates the result back. This module owns
the two fixed instruction texts and the translation request; the prompt side and
the outbound rewrite live in :mod:`bot.life.memory.inject` and
:mod:`bot.life.reply.postprocess`.
"""

from __future__ import annotations

import asyncio
from typing import Any

OUTPUT_LANGUAGE: dict[str, str] = {
    "zh-classical": "本次回复请使用中文文言文，保留角色语气，只输出回复正文，不附白话译文。",
    "ja-JP": "今回の返信は日本語で、返信本文だけを出力してください。",
    "ru-RU": "Напишите этот ответ по-русски, выводя только текст ответа.",
    "fr-FR": "Rédigez cette réponse en français et produisez uniquement le texte de la réponse.",
}

TRANSLATION_INSTRUCTION = (
    "请将以下内容翻译成中文，保留原文的含义、创意、角色语气、情绪和分段，不增删内容。\n"
    "只输出中文译文，保持原文格式，不要添加内容。如果已经是中文，不用翻译。\n"
    "待翻译内容："
)

CLASSICAL_TRANSLATION_INSTRUCTION = (
    "请将以下文言文翻译成现代白话中文，保留原文的含义、创意、角色语气、情绪和分段，不增删内容。\n"
    "只输出现代白话中文译文，保持原文格式，不要添加内容。如果已经是现代白话中文，不用翻译。\n"
    "待翻译内容："
)


def creative_instruction(language: str) -> str:
    """Return the generation-language instruction for one language.

    Args:
        language: Configured generation language.

    Returns:
        Instruction appended to the request system prompt; empty when unknown.
    """
    return OUTPUT_LANGUAGE.get(language, "")


async def translate_reply(
    provider: Any,
    text: str,
    *,
    language: str,
    timeout_seconds: float = 20.0,
) -> str | None:
    """Translate one generated reply back to Chinese.

    Mirrors upstream: a single user message, no system prompt, and an empty or
    unchanged answer counts as a failure so the caller can decide.

    Args:
        provider: Chat provider that generated the reply.
        text: Reply body in the generation language.
        language: Generation language used for the reply.
        timeout_seconds: Hard timeout for the translation request.

    Returns:
        The Chinese translation, or None when the provider is unavailable, the
        call fails, or it returns an empty translation.
    """
    body = (text or "").strip()
    if provider is None or not body:
        return None
    instruction = (
        CLASSICAL_TRANSLATION_INSTRUCTION
        if language == "zh-classical"
        else TRANSLATION_INSTRUCTION
    )
    try:
        async with asyncio.timeout(max(timeout_seconds, 1.0)):
            response = await provider.text_chat(prompt=f"{instruction}\n{body}")
        translated = (getattr(response, "completion_text", "") or "").strip()
    except Exception:
        return None
    return translated or None
