"""Shared helpers for the Life layer.

The Life features were ported one after another and each grew its own copy of
the same few primitives: schema creation, session-scope checks, the chat
provider call, archive cursors and the learn-from-archive loop. They live here
so behavior stays identical across features without copy-paste drift.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable, Sequence

from sqlalchemy import select
from sqlmodel import col

from bot import logger
from bot.core.provider.entities import ProviderType

Summarizer = Callable[[str, str], Awaitable[str | None]]

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


async def ensure_tables(db_helper: Any, *models: Any) -> None:
    """Create the given tables when they do not exist.

    Args:
        db_helper: Core async database helper.
        models: SQLModel classes owned by one store.
    """
    async with db_helper.get_db() as session:
        connection = await session.connection()
        for model in models:
            await connection.run_sync(
                lambda sync_connection, model=model: model.__table__.create(  # SQLModel adds __table__ at runtime
                    sync_connection,
                    checkfirst=True,
                )
            )
        await session.commit()


def event_session_enabled(service: Any, event: Any) -> bool:
    """Return whether the event's session is inside the Life scope.

    Args:
        service: Life service owning the root configuration.
        event: Bot message event.

    Returns:
        True when the session is enabled.
    """
    umo = getattr(event, "unified_msg_origin", "") or ""
    if not umo:
        return False
    getter = getattr(event, "get_group_id", None)
    group_id = getter() if callable(getter) else None
    return service.config.session_enabled(umo, group_id)


class SessionScoped:
    """Mixin forwarding the per-service allowlist check to the root config."""

    life_config: Any = None

    def session_enabled(self, umo: str, group_id: str | None = None) -> bool:
        """Return whether a session is inside the Life allowlist.

        Args:
            umo: Session key.
            group_id: Group id, or None.

        Returns:
            True when the session may use this feature.
        """
        if self.life_config is None:
            return True
        return bool(self.life_config.session_enabled(umo, group_id))


class CursorStoreMixin:
    """Cursor read/write shared by every archive consumer.

    Subclasses set ``cursor_model`` and own an async ``db`` helper.
    """

    cursor_model: Any = None
    db: Any = None

    async def cursor(self, session_key: str) -> int:
        """Return the last processed timestamp of a session.

        Args:
            session_key: Session scope.

        Returns:
            Last processed timestamp, or 0 when never processed.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                select(self.cursor_model).where(
                    col(self.cursor_model.session_key) == session_key
                )
            )
            row = result.scalars().first()
            return int(row.last_ts) if row else 0

    async def set_cursor(self, session_key: str, last_ts: int) -> None:
        """Persist the archive cursor of a session.

        Args:
            session_key: Session scope.
            last_ts: Last processed timestamp.
        """
        now = int(time.time())
        async with self.db.get_db() as session:
            result = await session.execute(
                select(self.cursor_model).where(
                    col(self.cursor_model.session_key) == session_key
                )
            )
            row = result.scalars().first()
            if row is None:
                session.add(
                    self.cursor_model(
                        session_key=session_key,
                        last_ts=last_ts,
                        updated_at=now,
                    )
                )
            else:
                row.last_ts = last_ts
                row.updated_at = now
            await session.commit()


async def llm_complete(
    context: Any,
    system_prompt: str,
    user_prompt: str,
    *,
    timeout: float | None = None,
) -> str | None:
    """Run one chat completion through the active provider.

    Args:
        context: Star context exposing ``provider_manager``.
        system_prompt: Instructions for the model.
        user_prompt: Payload of the call.
        timeout: Optional wall-clock limit in seconds.

    Returns:
        Completion text, or None when no provider is available.
    """
    manager = getattr(context, "provider_manager", None)
    if manager is None:
        return None
    provider = await manager.get_using_provider_async(ProviderType.CHAT_COMPLETION)
    if provider is None:
        return None
    if timeout is None:
        response = await provider.text_chat(
            prompt=user_prompt,
            system_prompt=system_prompt,
        )
    else:
        async with asyncio.timeout(timeout):
            response = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
    return (response.completion_text or "").strip() or None


class ArchiveLearner:
    """Shared learn-from-archive loop for expression and jargon.

    Subclasses provide ``mine`` and the LLM extraction hooks. The loop, the
    cursor handling, the counters and the JSON response parsing are identical.
    """

    label = "life"
    config_type: type = object
    result_keys: tuple[str, ...] = ()
    include_candidates = False

    def __init__(
        self,
        source: Any,
        target: Any,
        config: Any = None,
        summarizer: Summarizer | None = None,
    ) -> None:
        """Initialize the learner.

        Args:
            source: Archive store exposing ``fetch_range`` and ``active_sessions``.
            target: Feature store implementing ``cursor``/``set_cursor`` and
                ``upsert_many``.
            config: Learning settings.
            summarizer: Optional LLM callable for the batched extraction path.
        """
        self.source = source
        self.target = target
        self.config = config or self.config_type()
        self.summarizer = summarizer
        self._counters: dict[str, int] = {
            "rounds": 0,
            "sessions": 0,
            "candidates": 0,
            "llm_calls": 0,
            "llm_failures": 0,
            "skipped": 0,
        }
        for key in self.result_keys:
            self._counters[key] = 0

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
                logger.exception(
                    f"[life] {self.label} learning failed for {session_key}"
                )
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
        for key in self.result_keys:
            self._counters[key] += result[key]
        await self.target.set_cursor(session_key, int(messages[-1].get("ts") or 0))
        payload: dict[str, Any] = {"session": session_key, "messages": len(messages)}
        if self.include_candidates:
            payload["candidates"] = len(rows)
        payload.update(result)
        return payload

    def mine(self, messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Extract candidates without the model.

        Args:
            messages: Archived messages ordered by timestamp.

        Returns:
            Candidate payloads.
        """
        raise NotImplementedError

    def llm_prompt(self, transcript: str) -> str:
        """Build the user prompt for one extraction call.

        Args:
            transcript: Rendered message transcript.

        Returns:
            Prompt text.
        """
        raise NotImplementedError

    def llm_system_prompt(self) -> str:
        """Return the system prompt for one extraction call.

        Returns:
            System prompt text.
        """
        raise NotImplementedError

    def parse_items(
        self,
        items: list[Any],
        messages: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Turn parsed LLM items into candidate payloads.

        Args:
            items: JSON array returned by the model.
            messages: Archived messages of the session.

        Returns:
            Candidate payloads.
        """
        raise NotImplementedError

    async def _llm_extract(
        self,
        messages: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Run the batched LLM extraction path.

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
        self._counters["llm_calls"] += 1
        try:
            raw = await summarizer(self.llm_system_prompt(), self.llm_prompt(transcript))
        except Exception as exc:
            self._counters["llm_failures"] += 1
            logger.warning(f"[life] {self.label} extraction failed: {exc}")
            return []
        if not raw:
            return []
        match = _JSON_BLOCK.search(raw)
        try:
            items = json.loads(match.group(1) if match else raw)
        except json.JSONDecodeError:
            logger.debug(f"[life] {self.label} payload was not JSON; ignored")
            return []
        if not isinstance(items, list):
            return []
        return self.parse_items(items, messages)
