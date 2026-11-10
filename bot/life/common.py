"""Shared helpers for the Life layer.

The Life features were ported one after another and each grew its own copy of
the same few primitives: schema creation, session-scope checks, the chat
provider call, archive cursors and the learn-from-archive loop. They live here
so behavior stays identical across features without copy-paste drift.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Iterator, Sequence
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import delete, select
from sqlmodel import col

from bot import logger
from bot.core.provider.entities import ProviderType
from bot.core.umo_alias import parse_umo

Summarizer = Callable[[str, str], Awaitable[str | None]]

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_JSON_FENCE = re.compile(
    r"\A```(?:json)?[ \t]*\r?\n(.*?)\r?\n```[ \t]*\Z",
    re.IGNORECASE | re.DOTALL,
)


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


async def get_row(db_helper: Any, model: Any, row_id: Any) -> dict[str, Any] | None:
    """Return one row by primary key as a plain dict.

    Args:
        db_helper: Core async database helper.
        model: SQLModel class owning the table.
        row_id: Primary key value.

    Returns:
        Row dictionary, or None when missing.
    """
    async with db_helper.get_db() as session:
        row = await session.get(model, row_id)
        return row.model_dump() if row else None


async def delete_rows(
    db_helper: Any,
    model: Any,
    ids: Iterable[int] | None,
    session_key: str | None,
    label: str,
) -> int:
    """Delete rows by id or by session.

    Args:
        db_helper: Core async database helper.
        model: SQLModel class owning the table.
        ids: Explicit row ids.
        session_key: Delete every row of one session.
        label: Prefix of the id field used in the error message.

    Returns:
        Number of deleted rows.

    Raises:
        ValueError: When neither filter is given.
    """
    if not ids and not session_key:
        raise ValueError(f"delete requires {label}_ids or session_key")
    async with db_helper.get_db() as session:
        condition = (
            col(model.id).in_(tuple(ids))
            if ids
            else col(model.session_key) == session_key
        )
        result = await session.execute(delete(model).where(condition))
        await session.commit()
        return max(int(result.rowcount or 0), 0)  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount


def event_umo(event: Any) -> str:
    """Return the unified message origin of an event.

    Args:
        event: Bot message event.

    Returns:
        Session key, or an empty string when the event has none.
    """
    return str(getattr(event, "unified_msg_origin", "") or "")


def event_group_id(event: Any) -> str | None:
    """Return the group id of an event, if any.

    Args:
        event: Bot message event.

    Returns:
        Group id, or None for private sessions and stubs without the getter.
    """
    getter = getattr(event, "get_group_id", None)
    return getter() if callable(getter) else None


def event_session_enabled(service: Any, event: Any) -> bool:
    """Return whether the event's session is inside the Life scope.

    Works with both the root ``LifeService`` (``config`` is the root
    configuration) and feature services inheriting :class:`SessionScoped`
    (``session_enabled`` reads their ``life_config``).

    Args:
        service: Life service or feature service owning the scope.
        event: Bot message event.

    Returns:
        True when the session is enabled.
    """
    umo = event_umo(event)
    if not umo:
        return False
    group_id = event_group_id(event)
    checker = getattr(service, "session_enabled", None)
    if callable(checker):
        return bool(checker(umo, group_id))
    config = getattr(service, "config", None)
    if config is None:
        return True
    return bool(config.session_enabled(umo, group_id))


def tool_guard(
    service: Any,
    event: Any,
    available: bool,
    label: str,
) -> str | None:
    """Return the error of an unavailable tool, or None when it may run.

    Args:
        service: Life service or feature service owning the scope.
        event: Bot message event.
        available: Whether the feature itself is enabled.
        label: Feature name used in the error message.

    Returns:
        Error text for the model, or None when the tool may run.
    """
    if not available:
        return f"error: {label} is not enabled"
    if not event_session_enabled(service, event):
        return f"error: {label} is not enabled for this session"
    return None


def umo_group_id(umo: str) -> str | None:
    """Return the group id encoded in a unified message origin.

    Args:
        umo: Unified message origin.

    Returns:
        Group id, or None for private sessions and malformed keys.
    """
    parsed = parse_umo(umo)
    if parsed["message_type"].lower() != "groupmessage":
        return None
    return parsed["session_id"] or None


def umo_platform(umo: str) -> str:
    """Return the platform id encoded in a unified message origin.

    Args:
        umo: Unified message origin.

    Returns:
        Platform id, ``unknown`` when the key is empty or malformed.
    """
    return parse_umo(umo)["platform"]


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


async def resolve_chat_provider(context: Any) -> Any:
    """Return the active chat provider without raising.

    Args:
        context: Star context exposing ``provider_manager``.

    Returns:
        Provider instance, or None when none is configured or resolvable.
    """
    manager = getattr(context, "provider_manager", None)
    if manager is None:
        return None
    try:
        return await manager.get_using_provider_async(ProviderType.CHAT_COMPLETION)
    except Exception:
        return None


def cosine(left: Any, right: Any) -> float:
    """Return the raw cosine similarity of two vectors.

    Args:
        left: First vector.
        right: Second vector of the same size.

    Returns:
        Similarity in ``[-1, 1]``; 0 for empty or mismatched vectors.
    """
    left_vector = np.asarray(left, dtype=np.float32)
    right_vector = np.asarray(right, dtype=np.float32)
    if not left_vector.size or left_vector.size != right_vector.size:
        return 0.0
    denominator = (
        float(np.linalg.norm(left_vector) * np.linalg.norm(right_vector)) or 1e-9
    )
    similarity = float(np.dot(left_vector, right_vector)) / denominator
    return similarity if math.isfinite(similarity) else 0.0


def prune_oldest_files(directory: Path, keep: int, label: str = "file") -> int:
    """Delete the oldest files when a directory exceeds its cap.

    Args:
        directory: Directory to prune.
        keep: Maximum number of files to keep; non-positive disables pruning.
        label: Noun used in failure logs.

    Returns:
        Number of files deleted.
    """
    if keep <= 0 or not directory.exists():
        return 0
    files = sorted(
        (path for path in directory.iterdir() if path.is_file()),
        key=lambda path: path.stat().st_mtime,
    )
    removed = 0
    for path in files[: max(len(files) - keep, 0)]:
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning(f"[life] failed to prune {label} {path}: {exc}")
    return removed


def iter_jsonl(path: str | Path, *, strict: bool = False) -> Iterator[dict[str, Any]]:
    """Yield the JSON objects of a JSONL file.

    Args:
        path: File to read.
        strict: Raise ``ValueError`` on malformed lines instead of skipping them.

    Yields:
        Parsed objects.

    Raises:
        ValueError: When ``strict`` is set and a line is not a JSON object.
    """
    target = Path(path)
    with target.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                if strict:
                    raise ValueError(
                        f"{target}:{number} is not valid JSON: {exc}"
                    ) from exc
                continue
            if not isinstance(record, dict):
                if strict:
                    raise ValueError(f"{target}:{number} is not a JSON object")
                continue
            yield record


def read_jsonl(path: str | Path, *, strict: bool = False) -> list[dict[str, Any]]:
    """Parse a JSONL file into a list of objects.

    Args:
        path: File to read.
        strict: Raise ``ValueError`` on malformed lines instead of skipping them.

    Returns:
        Parsed objects in file order.
    """
    return list(iter_jsonl(path, strict=strict))


def to_unix_seconds(value: Any, *, timezone: str | None = None) -> int:
    """Convert a datetime, epoch or ISO-like value to Unix seconds.

    Naive datetimes are interpreted in the given zone; without one the host's
    local zone is used, matching how MaiBot exports store them.

    Args:
        value: Datetime, ISO text, integer/float seconds or milliseconds.
        timezone: IANA zone applied to naive values.

    Returns:
        Unix timestamp in seconds; 0 when the value cannot be parsed.
    """
    if value in (None, ""):
        return 0
    zone = ZoneInfo(timezone) if timezone else None
    if isinstance(value, datetime):
        if value.tzinfo is None and zone is not None:
            value = value.replace(tzinfo=zone)
        return int(value.timestamp())
    if isinstance(value, int | float):
        number = float(value)
        if number > 10**11:  # milliseconds
            number /= 1000.0
        return int(number)
    text = str(value).strip()
    if text.isdigit():
        return to_unix_seconds(int(text), timezone=timezone)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None and zone is not None:
            parsed = parsed.replace(tzinfo=zone)
        return int(parsed.timestamp())
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text[:19], pattern)
        except ValueError:
            continue
        if zone is not None:
            return int(parsed.replace(tzinfo=zone).timestamp())
        return int(time.mktime(parsed.timetuple()))
    return 0


def parse_json_array(raw: str) -> list[Any] | None:
    """Parse a JSON array from a completion, unwrapping a code fence.

    Args:
        raw: Completion text.

    Returns:
        Parsed list, or None when the payload is not a JSON array.
    """
    if not raw:
        return None
    match = _JSON_BLOCK.search(raw)
    try:
        payload = json.loads(match.group(1) if match else raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, list) else None


def parse_json_object(raw: str, label: str = "payload") -> Any:
    """Parse one JSON object from a completion, unwrapping a code fence.

    Args:
        raw: Completion text.
        label: Payload name used in the error message.

    Returns:
        Parsed JSON value.

    Raises:
        ValueError: When the payload is not valid JSON.
    """
    text = (raw or "").strip()
    match = _JSON_FENCE.fullmatch(text)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {exc.msg}") from exc


def has_prompt_block(req: Any, tag: str) -> bool:
    """Return whether a prompt already carries the given Life block.

    Args:
        req: Provider request.
        tag: XML tag name of the block.

    Returns:
        True when the opening tag is already present.
    """
    return f"<{tag}" in (getattr(req, "system_prompt", "") or "")


def append_prompt_block(
    req: Any,
    tag: str,
    block: str,
    *,
    prepend: bool = False,
    separator: str = "",
) -> bool:
    """Append or prepend one Life block unless it is already present.

    Args:
        req: Provider request.
        tag: XML tag name used for the idempotency check.
        block: Rendered block.
        prepend: Put the block before the existing prompt.
        separator: Text inserted between the existing prompt and the block.

    Returns:
        True when the prompt was modified.
    """
    current = getattr(req, "system_prompt", "") or ""
    if not block or f"<{tag}" in current:
        return False
    if prepend:
        req.system_prompt = f"{block}\n\n{current}" if current else block
    else:
        req.system_prompt = f"{current}{separator}{block}" if current else block
    return True


def unregister_tools(manager: Any, names: Sequence[str], label: str) -> None:
    """Remove a set of tools from the core tool manager.

    Args:
        manager: ``FunctionToolManager`` exposed by the provider manager.
        names: Tool names to remove.
        label: Noun used in the debug log, e.g. ``memory``.
    """
    for name in names:
        try:
            manager.remove_func(name)
        except Exception:
            logger.debug(f"[life] failed to remove {label} tool {name}")


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
    provider = await resolve_chat_provider(context)
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


async def active_session_keys(
    source: Any,
    interval_minutes: float,
    limit: int,
) -> list[str]:
    """Return the recently active sessions of one learning round.

    Args:
        source: Archive store exposing ``active_sessions``.
        interval_minutes: Round interval; the lookback covers two rounds.
        limit: Maximum number of sessions.

    Returns:
        Session keys, newest first.
    """
    lookback = int(time.time()) - max(interval_minutes, 1) * 120
    keys = await source.active_sessions(since_ts=lookback, limit=max(limit, 1))
    return list(keys)[: max(limit, 1)]


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
            session_keys = await active_session_keys(
                self.source, self.config.learn_interval_minutes, 20
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
        items = parse_json_array(raw)
        if items is None:
            logger.debug(f"[life] {self.label} payload was not JSON; ignored")
            return []
        return self.parse_items(items, messages)
