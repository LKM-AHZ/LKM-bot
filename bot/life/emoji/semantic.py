"""VLM tag axes for the sticker library (port of MaiBot 1.4 semantic match).

Upstream 1.4 stopped matching stickers with one mixed vector. The vision model
labels an image into two groups - *emotion* tags ("开心", "无语") and *content*
tags ("猫", "点赞") - each group is embedded on its own axis, and a query is
scored against the axis it asks for. That keeps "a cat sticker" from winning
when the intent was "embarrassed".

Rows live in :class:`~bot.life.emoji.models.LifeEmojiAxis`; this module only
knows how to ask the model for tags and how to score vectors. Persistence and
sending stay in :mod:`bot.life.emoji.service`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Sequence

import numpy as np
from pydantic import BaseModel, Field, field_validator

from bot.life.common import cosine, parse_json_object
from bot.life.memory.store import unpack_vector

EMOTION = "emotion"
CONTENT = "content"
AXES = (EMOTION, CONTENT)

_TAG_PROMPT = """看这张表情包，输出它的检索标签，只输出 JSON：
{"emotion_tags": ["最多5个情绪词"], "content_tags": ["1到5个画面内容词"]}

要求：
- emotion_tags 描述这张图传递的情绪或语气，例如 开心、无语、疑惑、生气、委屈；
- content_tags 描述画面里能指认的内容，例如 猫、狗、人物、文字、点赞、问号；
- 每个词 2~8 个字，不要造句，不要重复，不要输出 JSON 以外的任何内容。
"""


class EmojiTags(BaseModel):
    """Grouped labels returned by the vision model."""

    emotion_tags: list[str] = Field(default_factory=list, max_length=5)
    content_tags: list[str] = Field(default_factory=list, min_length=1, max_length=5)

    @field_validator("emotion_tags", "content_tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> object:
        """Strip, drop empties and de-duplicate while keeping order.

        Args:
            value: Raw field value from the model.

        Returns:
            Normalized list, or the raw value so pydantic reports the real error.
        """
        if isinstance(value, list) and all(isinstance(tag, str) for tag in value):
            return list(dict.fromkeys(tag.strip() for tag in value if tag.strip()))
        return value

    def axis_texts(self) -> dict[str, str]:
        """Return the embedding text of every non-empty axis.

        Returns:
            Mapping of axis name to the joined tag text.
        """
        texts = {CONTENT: "、".join(self.content_tags)}
        if self.emotion_tags:
            texts[EMOTION] = "、".join(self.emotion_tags)
        return texts


def parse_tags(response: str) -> EmojiTags:
    """Parse one model response into grouped tags.

    Args:
        response: Raw completion text, optionally wrapped in a JSON fence.

    Returns:
        The validated tags.

    Raises:
        ValueError: When the payload is not JSON or fails validation.
    """
    payload = parse_json_object(response, "emoji tags")
    return EmojiTags.model_validate(payload)


async def extract_tags(
    provider: Any,
    image_path: str,
    *,
    timeout_seconds: float = 15.0,
) -> EmojiTags | None:
    """Ask the vision model for grouped tags of one sticker.

    Args:
        provider: Chat provider exposing ``text_chat`` with ``image_urls``.
        image_path: Local file path of the sticker.
        timeout_seconds: Hard timeout for the call.

    Returns:
        Grouped tags, or None when no provider is available or the call fails.
    """
    if provider is None or not image_path:
        return None
    try:
        async with asyncio.timeout(max(timeout_seconds, 1.0)):
            response = await provider.text_chat(
                prompt=_TAG_PROMPT,
                image_urls=[str(image_path)],
                system_prompt="你是表情包标注器。只输出 JSON 对象，不要解释。",
            )
        text = (getattr(response, "completion_text", "") or "").strip()
        if not text:
            return None
        return parse_tags(text)
    except Exception:
        return None


def score_axes(
    query: Sequence[float],
    rows: list[dict[str, Any]],
    *,
    axis: str | None = None,
) -> list[tuple[float, dict[str, Any]]]:
    """Score sticker axis vectors against one query vector.

    Args:
        query: Query embedding.
        rows: Rows carrying ``axis`` and ``axis_vector``.
        axis: Optional axis filter (``emotion`` / ``content``).

    Returns:
        ``(score, row)`` pairs sorted best first; empty when nothing scores.
    """
    query_vector = np.asarray(query, dtype=np.float32)
    if not query_vector.size or not np.linalg.norm(query_vector):
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        if axis is not None and row.get("axis") != axis:
            continue
        vector = unpack_vector(row.get("axis_vector") or b"")
        if not vector.size or vector.size != query_vector.size:
            continue
        similarity = max(0.0, min(1.0, cosine(vector, query_vector)))
        scored.append((similarity, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored
