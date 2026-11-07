"""Learn group slang from the archive.

Two sources, same table:

* **definition patterns** - ``X 就是 Y`` / ``X 的意思是 Y`` / ``Y 简称 X`` inside a
  message is the cheapest reliable signal that a term has a meaning in this
  group. Works offline.
* **LLM** - one batched call over recent messages finds terms that no pattern
  would catch and may write a short explanation.

A term is only learned together with an explanation; a bare word is not slang
yet, and storing it would produce exactly the "乱用" failure the plan warns about.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Awaitable, Callable, Sequence

from bot import logger
from bot.life.config import JargonConfig
from bot.life.jargon.store import JargonStore

Summarizer = Callable[[str, str], Awaitable[str | None]]

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_MAX_LLM_TERMS = 5

# (pattern, quoted). Quoted terms may be a single character, which is common for
# slang like 「典」; unquoted one-character terms are far too noisy.
_PATTERNS = (
    (
        re.compile(
            r"[「“\"](?P<term>[^」”\"]{1,8})[」”\"]\s*"
            r"(?:就是|指的是|意思是|是指)\s*(?P<meaning>[^。！？!?\n]{2,80})",
        ),
        True,
    ),
    (
        re.compile(
            r"(?P<term>[^\s，。！？、,.!?「」“”\"]{2,8}?)\s*"
            r"(?:就是指|就是|是指|指的是|的意思是|意思是|意味着)\s*"
            r"(?P<meaning>[^。！？!?\n]{2,80})",
        ),
        False,
    ),
    (
        re.compile(
            r"(?P<meaning>[^，。\n]{2,60})[，,]?\s*(?:简称|缩写为)\s*"
            r"(?P<term>[A-Za-z0-9\u4e00-\u9fff]{2,8})",
        ),
        False,
    ),
)

# Pronouns and everyday phrases that would drown the table if learned.
_TERM_STOPWORDS = frozenset(
    {
        "这个",
        "那个",
        "这些",
        "那些",
        "这里",
        "那里",
        "这样",
        "那样",
        "我们",
        "你们",
        "他们",
        "她们",
        "它们",
        "大家",
        "自己",
        "人家",
        "东西",
        "事情",
        "问题",
        "时候",
        "地方",
        "感觉",
        "意思",
        "什么",
        "怎么",
        "为什么",
        "可以",
        "应该",
        "需要",
        "已经",
        "现在",
        "今天",
        "明天",
        "昨天",
    }
)

_PUNCT_TAIL = "，,。.!！?？、；;：: \t"


class JargonLearner:
    """Turn archived messages into ``(term, meaning)`` rows."""

    def __init__(
        self,
        source: Any,
        target: JargonStore,
        config: JargonConfig | None = None,
        summarizer: Summarizer | None = None,
    ) -> None:
        """Initialize the learner.

        Args:
            source: Archive store exposing ``fetch_range`` and ``active_sessions``.
            target: Jargon store.
            config: Learning settings.
            summarizer: Optional LLM callable for the extraction path.
        """
        self.source = source
        self.target = target
        self.config = config or JargonConfig()
        self.summarizer = summarizer
        self._counters = {
            "rounds": 0,
            "sessions": 0,
            "candidates": 0,
            "inserted": 0,
            "updated": 0,
            "conflicts": 0,
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
        keys = list(session_keys)[:20]
        for session_key in keys:
            try:
                await self.learn_session(session_key)
            except Exception:
                logger.exception(f"[life] jargon learning failed for {session_key}")
        self._counters["sessions"] += len(keys)
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
        rows = self.mine(messages)
        if self.summarizer is not None and self.config.use_llm:
            rows.extend(await self._llm_extract(messages))
        self._counters["candidates"] += len(rows)
        result = await self.target.upsert_many(rows)
        for key in ("inserted", "updated", "conflicts"):
            self._counters[key] += result[key]
        await self.target.set_cursor(session_key, int(messages[-1].get("ts") or 0))
        return {"session": session_key, "messages": len(messages), **result}

    def mine(self, messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract explicit definitions from messages.

        Args:
            messages: Archived messages ordered by timestamp.

        Returns:
            Candidate payloads with term, meaning, evidence and confidence.
        """
        session_key = str(messages[0].get("session_key") or "")
        found: dict[str, dict[str, Any]] = {}
        for message in messages:
            text = str(message.get("plain_text") or "").strip()
            if not text:
                continue
            for pattern, quoted in _PATTERNS:
                for match in pattern.finditer(text):
                    term = self._clean_term(match.group("term"), quoted=quoted)
                    meaning = self._clean_meaning(match.group("meaning"))
                    if not term or not meaning:
                        continue
                    # A one-character term is often inside its own explanation
                    # (典 -> 典型的例子), so only guard longer terms.
                    if len(term) >= 2 and (term in meaning or meaning.startswith(term)):
                        continue
                    entry = found.get(term)
                    if entry is None:
                        found[term] = {
                            "session_key": session_key,
                            "term": term,
                            "meaning": meaning,
                            "confidence": 0.75,
                            "source": "heuristic",
                            "evidence": [text[:200]],
                            "last_seen_ts": int(message.get("ts") or 0),
                        }
                    elif meaning not in entry["evidence"]:
                        entry["evidence"].append(text[:200])
        return list(found.values())

    def _clean_term(self, term: str, quoted: bool = False) -> str:
        """Validate and trim a mined term.

        Args:
            term: Raw capture.
            quoted: Whether the term was quoted, allowing single characters.

        Returns:
            Clean term, or an empty string when it must be dropped.
        """
        cleaned = term.strip().strip(_PUNCT_TAIL).strip()
        min_chars = 1 if quoted else max(self.config.min_term_chars, 2)
        if len(cleaned) < min_chars:
            return ""
        if len(cleaned) > max(self.config.max_term_chars, 2):
            return ""
        if cleaned in _TERM_STOPWORDS:
            return ""
        if cleaned.isdigit():
            return ""
        return cleaned

    def _clean_meaning(self, meaning: str) -> str:
        """Validate and trim an explanation.

        Args:
            meaning: Raw capture.

        Returns:
            Clean meaning, or an empty string when it must be dropped.
        """
        cleaned = meaning.strip().strip(_PUNCT_TAIL).strip()
        if len(cleaned) < 2:
            return ""
        return cleaned[: self.config.max_meaning_chars]

    async def _llm_extract(
        self,
        messages: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Ask the chat model for slang terms and their meanings.

        Args:
            messages: Archived messages of one session.

        Returns:
            Candidate payloads with ``source="llm"``; empty on failure.
        """
        summarizer = self.summarizer
        if summarizer is None:
            return []
        transcript = "\n".join(
            f"{message.get('sender_name') or '?'}: "
            f"{str(message.get('plain_text') or '')[:200]}"
            for message in messages[-30:]
            if message.get("plain_text")
        )
        if not transcript.strip():
            return []
        prompt = (
            "从下面的群聊里找出「黑话/内部梗」：只有在这个群里被反复使用、"
            "有特定含义的词才算。\n"
            "只输出 JSON 数组，元素形如 "
            '{"term": "词", "meaning": "一句话解释"}。\n'
            f"要求：term 不超过 {self.config.max_term_chars} 个字；"
            "meaning 不超过 40 字；不确定就不要输出；没有就输出 []。最多 "
            f"{_MAX_LLM_TERMS} 条。\n\n记录：\n{transcript}"
        )
        self._counters["llm_calls"] += 1
        try:
            raw = await summarizer(
                "你是网络语言与群聊黑话分析助手，只输出 JSON，不输出解释。",
                prompt,
            )
        except Exception as exc:
            self._counters["llm_failures"] += 1
            logger.warning(f"[life] jargon extraction failed: {exc}")
            return []
        if not raw:
            return []
        match = _JSON_BLOCK.search(raw)
        try:
            items = json.loads(match.group(1) if match else raw)
        except json.JSONDecodeError:
            logger.debug("[life] jargon payload was not JSON; ignored")
            return []
        if not isinstance(items, list):
            return []
        session_key = str(messages[0].get("session_key") or "")
        rows: list[dict[str, Any]] = []
        for item in items[:_MAX_LLM_TERMS]:
            if not isinstance(item, dict):
                continue
            term = self._clean_term(str(item.get("term") or ""))
            meaning = self._clean_meaning(str(item.get("meaning") or ""))
            if not term or not meaning:
                continue
            rows.append(
                {
                    "session_key": session_key,
                    "term": term,
                    "meaning": meaning,
                    "confidence": 0.7,
                    "source": "llm",
                    "evidence": [],
                    "last_seen_ts": int(messages[-1].get("ts") or 0),
                }
            )
        return rows
