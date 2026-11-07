"""The ``read_tool_result`` tool: fetch details the condenser dropped.

The tool only reads the per-session stash, so it is harmless to expose: an
unknown id or a session outside the Life scope gets a plain error string and
never leaks another chat's raw result.
"""

from __future__ import annotations

from typing import Any

from bot import logger
from bot.life.toolresult.store import StoredToolResult

_TOOL_NAMES = ("read_tool_result",)

MAX_MATCHES = 50
MAX_LINES = 400
MAX_LINE_CHARS = 400


def render_read_view(
    stored: StoredToolResult,
    *,
    keyword: str = "",
    offset: int = 0,
    limit: int = 0,
    max_chars: int = 4000,
) -> str:
    """Format one stored result for the model.

    Long single-line results (a JSON dump is the common case) are split into
    display lines of at most ``MAX_LINE_CHARS`` so ``keyword`` and ``offset``
    can actually reach into them.

    Args:
        stored: Raw result pulled from the stash.
        keyword: Only return lines containing this text (case-insensitive).
        offset: 1-based line to start from when slicing; ignored with keyword.
        limit: Maximum lines to return; 0 uses a size-based default.
        max_chars: Character budget of the returned body.

    Returns:
        A header, the selected lines with numbers, and a continuation hint when
        the view was cut.
    """
    lines: list[str] = []
    for raw_line in stored.content.splitlines() or [""]:
        if len(raw_line) <= MAX_LINE_CHARS:
            lines.append(raw_line)
        else:
            lines.extend(
                raw_line[i : i + MAX_LINE_CHARS]
                for i in range(0, len(raw_line), MAX_LINE_CHARS)
            )
    total = len(lines)
    max_chars = max(200, int(max_chars))

    if keyword:
        needle = keyword.strip().lower()
        matches = [
            index
            for index, line in enumerate(lines)
            if needle and needle in line.lower()
        ]
        if not matches:
            return (
                f"[{stored.result_id} {stored.tool_name}: {stored.chars} chars, "
                f"{total} lines] no line contains {keyword!r}"
            )
        wanted = max(1, min(MAX_MATCHES, int(limit) or MAX_MATCHES))
        selected: list[str] = []
        used = 0
        for index in matches:
            line = f"L{index + 1}: {lines[index]}"
            if selected and used + len(line) + 1 > max_chars:
                break
            selected.append(line)
            used += len(line) + 1
            if len(selected) >= wanted:
                break
        header = (
            f"[{stored.result_id} {stored.tool_name}: {stored.chars} chars, "
            f"{total} lines] {len(matches)} line(s) contain {keyword!r}, "
            f"showing {len(selected)}"
        )
        note = (
            f"\n... ({len(matches) - len(selected)} more matching line(s); "
            "narrow the keyword or pass limit to see them)"
            if len(selected) < len(matches)
            else ""
        )
        return f"{header}\n" + "\n".join(selected) + note

    start = max(0, int(offset) - 1) if offset else 0
    wanted = min(MAX_LINES, int(limit)) if limit else MAX_LINES
    selected: list[str] = []
    used = 0
    for index in range(start, min(total, start + wanted)):
        line = f"L{index + 1}: {lines[index]}"
        if used + len(line) + 1 > max_chars and selected:
            break
        selected.append(line)
        used += len(line) + 1
    end = start + len(selected)
    header = (
        f"[{stored.result_id} {stored.tool_name}: {stored.chars} chars, "
        f"{total} lines] showing lines {start + 1}-{end}"
    )
    note = (
        f'\n... (more lines; call read_tool_result(result_id="{stored.result_id}", '
        f"offset={end + 1}) to continue)"
        if end < total
        else ""
    )
    return f"{header}\n" + "\n".join(selected) + note


def _session_enabled(service: Any, event: Any) -> bool:
    """Return whether the event's session may read its stash.

    Args:
        service: Life service owning the configuration.
        event: Bot message event.

    Returns:
        True when the session is inside the Life scope.
    """
    umo = getattr(event, "unified_msg_origin", "") or ""
    if not umo:
        return False
    getter = getattr(event, "get_group_id", None)
    group_id = getter() if callable(getter) else None
    return service.config.session_enabled(umo, group_id)


def register_tool_result_tools(manager: Any, service: Any) -> None:
    """Register ``read_tool_result`` on the core tool manager.

    Args:
        manager: ``FunctionToolManager`` exposed by the provider manager.
        service: Life service that owns the stash and the configuration.
    """

    async def read_tool_result(
        event: Any,
        result_id: str,
        keyword: str = "",
        offset: int = 0,
        limit: int = 0,
    ) -> str:
        """Read the raw text behind an earlier condensed tool result."""
        config = service.config.tool_result
        if not config.enabled or service.tool_results is None:
            return "error: tool-result cache is not enabled"
        if not _session_enabled(service, event):
            return "error: tool-result cache is not enabled for this session"
        umo = getattr(event, "unified_msg_origin", "") or ""
        stored = service.tool_results.get(umo, str(result_id))
        if stored is None:
            kept = service.tool_results.ids(umo)
            return (
                f"error: no stored result {result_id!r}; "
                f"kept ids: {', '.join(kept) if kept else 'none'}"
            )
        try:
            return render_read_view(
                stored,
                keyword=str(keyword or ""),
                offset=int(offset or 0),
                limit=int(limit or 0),
                max_chars=config.read_max_chars,
            )
        except Exception:
            logger.exception("[life] read_tool_result failed for %s", result_id)
            return "error: failed to read the stored result"

    try:
        manager.add_func(
            name="read_tool_result",
            func_args=[
                {
                    "type": "string",
                    "name": "result_id",
                    "description": "Id shown in the condensed result, e.g. r3.",
                },
                {
                    "type": "string",
                    "name": "keyword",
                    "description": "Only return lines containing this text.",
                },
                {
                    "type": "integer",
                    "name": "offset",
                    "description": "1-based line to start from when slicing.",
                },
                {
                    "type": "integer",
                    "name": "limit",
                    "description": "Maximum lines to return.",
                },
            ],
            desc=(
                "Read the raw text of an earlier tool result that was condensed. "
                "Use keyword to jump to a detail, or offset to page through the original."
            ),
            handler=read_tool_result,
        )
    except Exception:
        logger.exception("[life] failed to register read_tool_result")


def unregister_tool_result_tools(manager: Any) -> None:
    """Remove ``read_tool_result`` from the core tool manager.

    Args:
        manager: ``FunctionToolManager`` exposed by the provider manager.
    """
    for name in _TOOL_NAMES:
        try:
            manager.remove_func(name)
        except Exception:
            logger.debug(f"[life] failed to remove tool {name}")
