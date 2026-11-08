"""Learn how the group phrases things.

Learning runs over the message archive, one session at a time, from a persisted
cursor. Two paths feed the same table:

* **heuristic** - segment messages on punctuation, keep short emphatic segments
  that repeat, and use the surrounding sentence as the situation. Works offline
  and keeps the first version honest.
* **LLM** - one batched call turns recent messages into ``(situation, style)``
  pairs, which carry more context but need a chat provider.

The loop itself is shared with the jargon learner; only the extraction differs.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from bot.life.common import ArchiveLearner
from bot.life.config import ExpressionConfig
from bot.life.expression.store import ExpressionStore

_SEGMENT_SPLIT = re.compile(r"[，,、。！？!?…~～\s]+")
_TRIM = " \t「」『』“”\"'()（）[]【】…~～"
_NOISE = re.compile(r"https?://|@\S+|^/|^\d+$|^[\W_]+$")
_MARKERS = (
    "哈哈",
    "嘿嘿",
    "笑死",
    "麻了",
    "无语",
    "服了",
    "绝了",
    "离谱",
    "好家伙",
    "确实",
    "绷不住",
    "破防",
    "泪目",
    "典",
    "草",
    "牛",
    "强",
    "乐",
    "呃",
    "emm",
    "啊这",
    "不会吧",
    "真的假的",
)
_MAX_SITUATION_CHARS = 120
_MAX_LLM_EXPRESSIONS = 5


class ExpressionLearner(ArchiveLearner):
    """Turn archived messages into learned expressions."""

    label = "expression"
    config_type = ExpressionConfig
    result_keys = ("inserted", "merged")
    include_candidates = True

    target: ExpressionStore

    def mine(self, messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract repeated emphatic phrases from a batch of messages.

        Args:
            messages: Archived messages ordered by timestamp.

        Returns:
            Candidate payloads with ``situation``, ``style``, ``scene`` and
            ``weight``.
        """
        counts: dict[str, dict[str, Any]] = {}
        for message in messages:
            text = str(message.get("plain_text") or "").strip()
            if not text:
                continue
            scene = "group" if message.get("group_id") else "private"
            for segment in _SEGMENT_SPLIT.split(text):
                style = segment.strip().strip(_TRIM).strip()
                if not self._acceptable(style):
                    continue
                entry = counts.setdefault(
                    style,
                    {"count": 0, "situation": "", "scene": scene},
                )
                entry["count"] += 1
                if not entry["situation"]:
                    surround = text.replace(segment, "").strip().strip(_TRIM)
                    entry["situation"] = surround[:_MAX_SITUATION_CHARS] or "日常闲聊"
        candidates: list[dict[str, Any]] = []
        session_key = str(messages[0].get("session_key") or "")
        for style, entry in counts.items():
            if entry["count"] < max(self.config.min_frequency, 1):
                continue
            if not self._emphatic(style) and entry["count"] < 3:
                continue
            candidates.append(
                {
                    "session_key": session_key,
                    "situation": entry["situation"],
                    "style": style,
                    "scene": entry["scene"],
                    "weight": float(entry["count"]),
                    "source": "heuristic",
                    "tags": ["emphatic"] if self._emphatic(style) else [],
                }
            )
        return candidates

    def _acceptable(self, style: str) -> bool:
        """Return whether a segment is a plausible expression.

        Args:
            style: Trimmed segment.

        Returns:
            True when the segment is short, clean and not noise.
        """
        if not style or len(style) < max(self.config.min_style_chars, 1):
            return False
        if len(style) > max(self.config.max_style_chars, 2):
            return False
        return not _NOISE.search(style)

    def _emphatic(self, style: str) -> bool:
        """Return whether a segment carries an emotive marker.

        Args:
            style: Candidate phrasing.

        Returns:
            True when a marker or strong punctuation is present.
        """
        lowered = style.lower()
        return any(marker in lowered for marker in _MARKERS)

    def llm_prompt(self, transcript: str) -> str:
        """Build the extraction prompt.

        Args:
            transcript: Rendered message transcript.

        Returns:
            Prompt text.
        """
        return (
            "从下面的群聊里提炼「表达方式」：某个具体情景下人们怎么说。\n"
            "只输出 JSON 数组，元素形如 "
            '{"situation": "什么情景", "style": "怎么说（要短，像口头禅或句式）"}。\n'
            f"要求：style 不超过 {self.config.max_style_chars} 个字；"
            "不要抽象总结；不要编造；没有就输出 []。最多 "
            f"{_MAX_LLM_EXPRESSIONS} 条。\n\n记录：\n{transcript}"
        )

    def llm_system_prompt(self) -> str:
        """Return the extraction system prompt.

        Returns:
            System prompt text.
        """
        return "你是语言习惯分析助手，只输出 JSON，不输出解释。"

    def parse_items(
        self,
        items: list[Any],
        messages: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Turn parsed LLM items into expression rows.

        Args:
            items: JSON array returned by the model.
            messages: Archived messages of the session.

        Returns:
            Candidate payloads with ``source="llm"``.
        """
        session_key = str(messages[0].get("session_key") or "")
        scene = "group" if messages[0].get("group_id") else "private"
        rows: list[dict[str, Any]] = []
        for item in items[:_MAX_LLM_EXPRESSIONS]:
            if not isinstance(item, dict):
                continue
            style = str(item.get("style") or "").strip()
            if not self._acceptable(style):
                continue
            situation = str(item.get("situation") or "").strip()[:_MAX_SITUATION_CHARS]
            rows.append(
                {
                    "session_key": session_key,
                    "situation": situation or "日常闲聊",
                    "style": style,
                    "scene": scene,
                    "weight": 2.0,
                    "source": "llm",
                    "tags": [],
                }
            )
        return rows
