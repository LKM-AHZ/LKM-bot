"""Learn how the group phrases things.

Learning runs over the message archive, one session at a time, from a persisted
cursor. Two paths feed the same table:

* **heuristic** - segment messages on punctuation, keep short emphatic segments
  that repeat, and use the surrounding sentence as the situation. Works offline
  and keeps the first version honest.
* **LLM** - one batched call turns recent messages into ``(situation, style)``
  pairs, which carry more context but need a chat provider.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Awaitable, Callable, Sequence

from bot import logger
from bot.life.config import ExpressionConfig
from bot.life.expression.store import ExpressionStore

Summarizer = Callable[[str, str], Awaitable[str | None]]

_SEGMENT_SPLIT = re.compile(r"[，,、。！？!?…~～\s]+")
_TRIM = " \t「」『』“”\"'()（）[]【】…~～"
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
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


class ExpressionLearner:
    """Turn archived messages into learned expressions."""

    def __init__(
        self,
        source: Any,
        target: ExpressionStore,
        config: ExpressionConfig | None = None,
        summarizer: Summarizer | None = None,
    ) -> None:
        """Initialize the learner.

        Args:
            source: Archive store exposing ``fetch_range`` and ``active_sessions``.
            target: Expression store.
            config: Learning settings.
            summarizer: Optional LLM callable for the batched extraction path.
        """
        self.source = source
        self.target = target
        self.config = config or ExpressionConfig()
        self.summarizer = summarizer
        self._counters = {
            "rounds": 0,
            "sessions": 0,
            "candidates": 0,
            "inserted": 0,
            "merged": 0,
            "llm_calls": 0,
            "llm_failures": 0,
            "skipped": 0,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return learning counters.

        Returns:
            Copy of the internal counters.
        """
        return dict(self._counters)

    async def run(self, session_keys: Sequence[str] | None = None) -> dict[str, Any]:
        """Learn from recently active sessions.

        Args:
            session_keys: Sessions to process; None discovers active ones.

        Returns:
            Per-round counters.
        """
        if not self.config.enabled:
            return {"enabled": False}
        self._counters["rounds"] += 1
        if session_keys is None:
            lookback = (
                int(time.time()) - max(self.config.learn_interval_minutes, 1) * 120
            )
            session_keys = await self.source.active_sessions(
                since_ts=lookback, limit=20
            )
        for session_key in list(session_keys)[:20]:
            try:
                await self.learn_session(session_key)
            except Exception:
                logger.exception(f"[life] expression learning failed for {session_key}")
        self._counters["sessions"] += len(list(session_keys or []))
        return {"enabled": True, **self.snapshot()}

    async def learn_session(self, session_key: str) -> dict[str, Any]:
        """Learn from one session since its cursor.

        Args:
            session_key: Session scope.

        Returns:
            Counters of this session.
        """
        cursor = await self.target.cursor(session_key)
        messages = await self.source.fetch_range(
            session_key,
            since_ts=cursor,
            limit=max(self.config.max_learn_messages, 1),
        )
        if not messages:
            self._counters["skipped"] += 1
            return {"session": session_key, "skipped": True}
        candidates = self.mine(messages)
        if self.summarizer is not None and self.config.use_llm:
            candidates.extend(await self._llm_extract(messages))
        self._counters["candidates"] += len(candidates)
        result = await self.target.upsert_many(candidates)
        self._counters["inserted"] += result["inserted"]
        self._counters["merged"] += result["merged"]
        await self.target.set_cursor(session_key, int(messages[-1].get("ts") or 0))
        return {
            "session": session_key,
            "messages": len(messages),
            "candidates": len(candidates),
            **result,
        }

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

    async def _llm_extract(
        self,
        messages: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Ask the chat model for situation/style pairs.

        Args:
            messages: Archived messages of one session.

        Returns:
            Candidate payloads with ``source="llm"``; empty on failure.
        """
        transcript = "\n".join(
            f"{message.get('sender_name') or '?'}: "
            f"{str(message.get('plain_text') or '')[:200]}"
            for message in messages[-30:]
            if message.get("plain_text")
        )
        if not transcript.strip():
            return []
        prompt = (
            "从下面的群聊里提炼「表达方式」：某个具体情景下人们怎么说。\n"
            "只输出 JSON 数组，元素形如 "
            '{"situation": "什么情景", "style": "怎么说（要短，像口头禅或句式）"}。\n'
            f"要求：style 不超过 {self.config.max_style_chars} 个字；"
            "不要抽象总结；不要编造；没有就输出 []。最多 "
            f"{_MAX_LLM_EXPRESSIONS} 条。\n\n记录：\n{transcript}"
        )
        summarizer = self.summarizer
        if summarizer is None:
            return []
        self._counters["llm_calls"] += 1
        try:
            raw = await summarizer(
                "你是语言习惯分析助手，只输出 JSON，不输出解释。",
                prompt,
            )
        except Exception as exc:
            self._counters["llm_failures"] += 1
            logger.warning(f"[life] expression extraction failed: {exc}")
            return []
        if not raw:
            return []
        match = _JSON_BLOCK.search(raw)
        try:
            items = json.loads(match.group(1) if match else raw)
        except json.JSONDecodeError:
            logger.debug("[life] expression payload was not JSON; ignored")
            return []
        if not isinstance(items, list):
            return []
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
