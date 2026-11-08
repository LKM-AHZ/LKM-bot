"""Learn group slang from the archive.

Two sources, same table:

* **definition patterns** - ``X 就是 Y`` / ``X 的意思是 Y`` / ``Y 简称 X`` inside a
  message is the cheapest reliable signal that a term has a meaning in this
  group. Works offline.
* **LLM** - one batched call over recent messages finds terms that no pattern
  would catch and may write a short explanation.

A term is only learned together with an explanation; a bare word is not slang
yet, and storing it would produce exactly the "乱用" failure the plan warns about.
The loop itself is shared with the expression learner; only the extraction
differs.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from bot.life.common import ArchiveLearner
from bot.life.config import JargonConfig
from bot.life.jargon.store import JargonStore

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


class JargonLearner(ArchiveLearner):
    """Turn archived messages into ``(term, meaning)`` rows."""

    label = "jargon"
    config_type = JargonConfig
    result_keys = ("inserted", "updated", "conflicts")

    target: JargonStore

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

    def llm_prompt(self, transcript: str) -> str:
        """Build the extraction prompt.

        Args:
            transcript: Rendered message transcript.

        Returns:
            Prompt text.
        """
        return (
            "从下面的群聊里找出「黑话/内部梗」：只有在这个群里被反复使用、"
            "有特定含义的词才算。\n"
            "只输出 JSON 数组，元素形如 "
            '{"term": "词", "meaning": "一句话解释"}。\n'
            f"要求：term 不超过 {self.config.max_term_chars} 个字；"
            "meaning 不超过 40 字；不确定就不要输出；没有就输出 []。最多 "
            f"{_MAX_LLM_TERMS} 条。\n\n记录：\n{transcript}"
        )

    def llm_system_prompt(self) -> str:
        """Return the extraction system prompt.

        Returns:
            System prompt text.
        """
        return "你是网络语言与群聊黑话分析助手，只输出 JSON，不输出解释。"

    def parse_items(
        self,
        items: list[Any],
        messages: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Turn parsed LLM items into jargon rows.

        Args:
            items: JSON array returned by the model.
            messages: Archived messages of the session.

        Returns:
            Candidate payloads with ``source="llm"``.
        """
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
