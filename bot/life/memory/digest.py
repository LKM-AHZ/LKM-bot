"""Write chain: archived messages -> summaries, facts and episodes.

The builder is the only place that turns raw chat into long-term memory. It runs
on a timer, keeps a per-session cursor so restarts do not re-summarize, and only
closes a conversation window once it has been idle for ``gap_minutes`` so an
in-progress discussion is never frozen mid-topic.

LLM summarization is optional: without a summarizer (or with ``use_llm=false``)
the builder falls back to an extractive transcript summary, which still keeps
retrieval useful in an offline deployment.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Sequence

from bot import logger
from bot.life.common import parse_json_array
from bot.life.config import MemoryDigestConfig
from bot.life.memory.store import MemoryStore

Summarizer = Callable[[str, str], Awaitable[str | None]]
"""Callable taking ``(system_prompt, user_prompt)`` and returning text."""

FactSink = Callable[[str, list[dict]], Awaitable[None]]
"""Callable receiving ``(session_key, fact_rows)`` after a digest round."""

_MAX_WINDOW_CHARS = 2400
_MAX_SUMMARY_CHARS = 700
_MAX_FACTS_PER_WINDOW = 4


class DigestBuilder:
    """Turn archived chat into long-term memory rows."""

    def __init__(
        self,
        source: Any,
        target: MemoryStore,
        config: MemoryDigestConfig | None = None,
        summarizer: Summarizer | None = None,
        on_facts: FactSink | None = None,
    ) -> None:
        """Initialize the builder.

        Args:
            source: Archive store exposing ``fetch_range`` and ``active_sessions``.
            target: Memory store that receives summary, fact and episode rows.
            config: Digest settings.
            summarizer: Optional LLM callable used for abstractive summaries.
            on_facts: Optional sink that receives the extracted fact rows, used
                by the person layer to attach facts to people.
        """
        self.source = source
        self.target = target
        self.config = config or MemoryDigestConfig()
        self.summarizer = summarizer
        self.on_facts = on_facts
        self._counters = {
            "rounds": 0,
            "sessions": 0,
            "windowed_sessions": 0,
            "summaries": 0,
            "facts": 0,
            "episodes": 0,
            "llm_failures": 0,
            "skipped": 0,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return digest counters.

        Returns:
            Copy of the internal counters.
        """
        return dict(self._counters)

    async def run(self, session_keys: Sequence[str] | None = None) -> dict[str, Any]:
        """Digest recent activity for one round.

        Args:
            session_keys: Sessions to process; None discovers active sessions.

        Returns:
            Per-round counters including how many sessions were touched.
        """
        if not self.config.enabled:
            return {"enabled": False}
        self._counters["rounds"] += 1
        if session_keys is None:
            lookback = int(time.time()) - max(self.config.interval_minutes, 1) * 120
            session_keys = await self.source.active_sessions(
                since_ts=lookback,
                limit=self.config.max_sessions_per_round,
            )
        keys = list(session_keys)[: max(self.config.max_sessions_per_round, 1)]
        for session_key in keys:
            try:
                await self.digest_session(session_key)
            except Exception:
                logger.exception(f"[life] memory digest failed for {session_key}")
        self._counters["sessions"] += len(keys)
        return {"enabled": True, "sessions": len(keys), **self.snapshot()}

    async def digest_session(self, session_key: str) -> dict[str, Any]:
        """Digest one session from its cursor to the last closed window.

        Args:
            session_key: Session scope.

        Returns:
            Counts of the rows produced for this session.
        """
        cursor = await self.target.cursor(session_key)
        messages = await self.source.fetch_range(
            session_key,
            since_ts=cursor,
            limit=self.config.max_messages,
        )
        if len(messages) < max(self.config.min_messages, 1):
            self._counters["skipped"] += 1
            return {"session": session_key, "skipped": True, "messages": len(messages)}

        windows = self._split_windows(messages)
        closed_at = int(time.time()) - max(self.config.gap_minutes, 1) * 60
        # Keep the most recent window open until it has been idle long enough.
        if windows and windows[-1][-1]["ts"] > closed_at:
            windows = windows[:-1]
        if not windows:
            self._counters["skipped"] += 1
            return {"session": session_key, "skipped": True, "messages": len(messages)}

        summary_rows: list[dict[str, Any]] = []
        fact_rows: list[dict[str, Any]] = []
        for window in windows:
            summary = await self._summarize(window)
            if not summary:
                continue
            summary_rows.append(
                {
                    "kind": "summary",
                    "session_key": session_key,
                    "content": summary,
                    "ts": window[-1]["ts"],
                    "start_ts": window[0]["ts"],
                    "end_ts": window[-1]["ts"],
                    "source_ids": [row.get("id") for row in window if row.get("id")],
                    "importance": min(0.4 + len(window) / 100.0, 0.9),
                    "meta": {
                        "messages": len(window),
                        "participants": self._participants(window),
                    },
                }
            )
            fact_rows.extend(await self._extract_facts(session_key, window))

        inserted_summaries = await self.target.insert_many(summary_rows)
        inserted_facts = await self.target.insert_many(fact_rows)
        self._counters["summaries"] += inserted_summaries
        self._counters["facts"] += inserted_facts

        episode_rows = await self._episodes(session_key, summary_rows)
        inserted_episodes = await self.target.insert_many(episode_rows)
        self._counters["episodes"] += inserted_episodes

        last_ts = windows[-1][-1]["ts"]
        await self.target.set_cursor(session_key, last_ts)
        self._counters["windowed_sessions"] += 1
        return {
            "session": session_key,
            "windows": len(windows),
            "summaries": inserted_summaries,
            "facts": inserted_facts,
            "episodes": inserted_episodes,
            "cursor": last_ts,
        }

    def _split_windows(
        self, messages: list[dict[str, Any]]
    ) -> list[list[dict[str, Any]]]:
        """Split messages into conversation windows.

        Args:
            messages: Archived messages ordered by timestamp.

        Returns:
            Windows separated by idle gaps or size limits.
        """
        gap = max(self.config.gap_minutes, 1) * 60
        size_limit = max(self.config.max_messages // 4, 10)
        windows: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        previous_ts = 0
        for message in messages:
            if not (message.get("plain_text") or "").strip():
                continue
            ts = int(message.get("ts") or 0)
            if current and (ts - previous_ts > gap or len(current) >= size_limit):
                windows.append(current)
                current = []
            current.append(message)
            previous_ts = ts
        if current:
            windows.append(current)
        return windows

    def _participants(self, window: Sequence[dict[str, Any]]) -> list[str]:
        """Return distinct participant names of a window.

        Args:
            window: Messages in the window.

        Returns:
            Participant names in first-seen order.
        """
        names: list[str] = []
        for message in window:
            name = message.get("sender_name") or message.get("sender_id")
            if name and name not in names:
                names.append(str(name))
        return names[:12]

    def _transcript(self, window: Sequence[dict[str, Any]]) -> str:
        """Render a window as a compact transcript.

        Args:
            window: Messages in the window.

        Returns:
            Transcript text clipped to the prompt budget.
        """
        lines: list[str] = []
        for message in window:
            name = message.get("sender_name") or message.get("sender_id") or "?"
            text = (message.get("plain_text") or "").strip()
            if not text:
                continue
            lines.append(f"{name}: {text[:300]}")
        return "\n".join(lines)[:_MAX_WINDOW_CHARS]

    async def _summarize(self, window: Sequence[dict[str, Any]]) -> str:
        """Produce one summary for a window.

        Args:
            window: Messages in the window.

        Returns:
            Summary text; empty when the window has no usable text.
        """
        transcript = self._transcript(window)
        if not transcript:
            return ""
        if self.summarizer is not None and self.config.use_llm:
            prompt = (
                "请把下面这段群聊整理成一条长期记忆摘要，供以后检索使用。\n"
                "要求：1) 用中文第三人称陈述；2) 保留人物、事件、结论、约定和数字；"
                "3) 不要编造原文没有的信息；4) 200 字以内，只输出摘要正文。\n\n"
                f"群聊记录：\n{transcript}"
            )
            try:
                summary = await self.summarizer(
                    "你是严谨的记忆整理助手，只根据给定记录写摘要。",
                    prompt,
                )
            except Exception as exc:
                self._counters["llm_failures"] += 1
                logger.warning(f"[life] memory summarizer failed: {exc}")
                summary = None
            if summary and summary.strip():
                return summary.strip()[:_MAX_SUMMARY_CHARS]
        header = f"{self._participants(window)[:3] or ['群聊']}"
        return (f"[群聊片段] {'、'.join(header)}：\n{transcript}")[:_MAX_SUMMARY_CHARS]

    async def _extract_facts(
        self,
        session_key: str,
        window: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Extract durable facts from a window with the LLM.

        Args:
            session_key: Session scope.
            window: Messages in the window.

        Returns:
            Fact rows ready for insertion; empty without an LLM.
        """
        if self.summarizer is None or not self.config.use_llm:
            return []
        transcript = self._transcript(window)
        if not transcript:
            return []
        prompt = (
            "从下面的群聊记录中抽取值得长期记住的事实（人物偏好、身份、约定、重要事件）。\n"
            "只输出 JSON 数组，元素形如 "
            '{"content": "一句中文事实", "person": "相关人物名或空字符串"}。\n'
            "没有值得记住的事实时输出 []。最多 "
            f"{_MAX_FACTS_PER_WINDOW} 条。\n\n记录：\n{transcript}"
        )
        try:
            raw = await self.summarizer(
                "你是信息抽取助手，只输出 JSON，不输出解释。",
                prompt,
            )
        except Exception as exc:
            self._counters["llm_failures"] += 1
            logger.warning(f"[life] memory fact extraction failed: {exc}")
            return []
        if not raw:
            return []
        items = parse_json_array(raw)
        if items is None:
            logger.debug("[life] memory fact payload was not JSON; ignored")
            return []
        rows: list[dict[str, Any]] = []
        for item in items[:_MAX_FACTS_PER_WINDOW]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            person = str(item.get("person") or "").strip() or None
            rows.append(
                {
                    "kind": "fact",
                    "session_key": session_key,
                    "content": content[:500],
                    "person_id": person,
                    "ts": window[-1]["ts"],
                    "importance": 0.75,
                    "source_ids": [row.get("id") for row in window if row.get("id")],
                    "meta": {"extracted": True},
                }
            )
        return rows

    async def _episodes(
        self,
        session_key: str,
        summaries: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Fold a burst of summaries into one episode row.

        Args:
            session_key: Session scope.
            summaries: Summary rows produced in this round.

        Returns:
            Zero or one episode row.
        """
        if len(summaries) < 2:
            return []
        participants: list[str] = []
        for row in summaries:
            for name in row.get("meta", {}).get("participants", []):
                if name not in participants:
                    participants.append(name)
        body = "\n".join(f"- {row['content']}" for row in summaries)
        content = f"[会话片段] {'、'.join(participants[:5])}：\n{body}"
        start_ts = summaries[0].get("start_ts") or summaries[0]["ts"]
        end_ts = summaries[-1].get("end_ts") or summaries[-1]["ts"]
        return [
            {
                "kind": "episode",
                "session_key": session_key,
                "content": content[: _MAX_SUMMARY_CHARS * 2],
                "ts": end_ts,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "importance": 0.8,
                "meta": {
                    "windows": len(summaries),
                    "participants": participants[:12],
                },
            }
        ]
