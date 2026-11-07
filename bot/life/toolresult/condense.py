"""Condensing oversized tool results before they enter the model history.

A search or listing tool can return thousands of characters that then ride
along in every later turn of the same run. When a result is successful, longer
than the configured threshold and not on the exclusion list, a small utility
completion rewrites it into the conclusions the current intent needs. The raw
text is stashed so ``read_tool_result`` can bring back any dropped detail; if
the utility call fails, times out or shortens nothing, the original text is
kept and the caller falls back to its normal handling.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from bot import logger
from bot.life.toolresult.store import ToolResultStore

CONDENSE_SYSTEM_PROMPT = (
    "你是工具结果整理助手。另一个决策模型调用了一个工具，原始结果很长，你要把它整理成决策模型能直接使用的结论。\n"
    "要求：\n"
    "1. 只保留和“调用意图”相关的事实、名称、数值与结论，删掉无关条目和重复描述。\n"
    "2. 后续可能要用来继续调用工具或发给用户的标识必须原样保留，包括 id、链接、路径、版本号、msg_id。\n"
    "3. 只根据原始结果整理，不要补充原文没有的内容；原文里没有答案就直接说明没有。\n"
    "4. 原始结果是列表时保留条目结构，压缩每一条的描述。\n"
    "5. 直接输出整理后的内容，不要寒暄，不要解释你做了什么，不要对用户说话。"
)

# ``(system_prompt, user_prompt) -> completion text or None``
UtilityLlm = Callable[[str, str], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class CondenseOutcome:
    """What happened to one oversized tool result."""

    content: str
    """Text the caller should write into the history."""

    condensed: bool = False
    """Whether ``content`` is a summary rather than the raw text."""

    result_id: str = ""
    """Id of the stashed raw result, empty when nothing was stashed."""

    original_chars: int = 0
    condensed_chars: int = 0
    error: str = ""
    """Empty on success; otherwise ``empty`` / ``not_shorter`` / ``timeout`` / ``exception``."""


def should_condense(
    tool_name: str,
    content: str,
    *,
    enabled: bool,
    threshold_chars: int,
    excluded_tool_names: tuple[str, ...] | list[str] = (),
) -> bool:
    """Return whether one result is worth a utility call.

    Args:
        tool_name: Name of the tool that produced the result.
        content: Raw result text.
        enabled: Whether condensing is switched on.
        threshold_chars: Minimum length that triggers condensing.
        excluded_tool_names: Control-signal tools that must never be rewritten.

    Returns:
        True when the result should be condensed.
    """
    if not enabled or not tool_name:
        return False
    if tool_name in excluded_tool_names:
        return False
    return len(content) >= threshold_chars


def build_task(
    tool_name: str,
    tool_args: dict[str, Any] | None,
    content: str,
    intent: str,
    *,
    max_input_chars: int,
    intent_max_chars: int,
) -> str:
    """Build the self-contained task handed to the utility call.

    Args:
        tool_name: Name of the tool that produced the result.
        tool_args: Arguments the model passed to the tool.
        content: Raw result text.
        intent: The model's own reasoning before the call.
        max_input_chars: Head of the result that is handed over.
        intent_max_chars: Tail of the intent that is handed over.

    Returns:
        Task text with the intent and the raw result.
    """
    arguments_text = json.dumps(tool_args or {}, ensure_ascii=False)
    planner_thought = intent.strip()[-intent_max_chars:] or "（决策模型没有写下想法）"
    raw_result = content[:max_input_chars]
    truncated_note = (
        f"\n（原始结果共 {len(content)} 字，这里只提供了前 {max_input_chars} 字）"
        if len(content) > max_input_chars
        else ""
    )
    return (
        f"工具名称：{tool_name}\n"
        f"调用参数：{arguments_text}\n\n"
        f"<调用意图>\n{planner_thought}\n</调用意图>\n\n"
        f"<原始结果>\n{raw_result}\n</原始结果>{truncated_note}"
    )


async def run_condense(
    *,
    tool_name: str,
    tool_args: dict[str, Any] | None,
    content: str,
    intent: str,
    llm: UtilityLlm,
    store: ToolResultStore,
    session_id: str,
    max_input_chars: int,
    intent_max_chars: int,
    timeout_seconds: float,
) -> CondenseOutcome:
    """Condense one result and stash the raw text when that succeeds.

    Args:
        tool_name: Name of the tool that produced the result.
        tool_args: Arguments the model passed to the tool.
        content: Raw result text.
        intent: The model's own reasoning before the call.
        llm: Utility completion used to write the summary.
        store: Stash that keeps the raw text.
        session_id: Unified message origin of the session.
        max_input_chars: Head of the result that is handed over.
        intent_max_chars: Tail of the intent that is handed over.
        timeout_seconds: Hard timeout of the utility completion.

    Returns:
        Outcome whose ``content`` is always safe to write into the history.
    """
    original_chars = len(content)
    task = build_task(
        tool_name,
        tool_args,
        content,
        intent,
        max_input_chars=max_input_chars,
        intent_max_chars=intent_max_chars,
    )
    try:
        summary = await asyncio.wait_for(
            llm(CONDENSE_SYSTEM_PROMPT, task),
            timeout=max(0.1, float(timeout_seconds)),
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[life] tool-result condense timed out after %.0fs for %s",
            timeout_seconds,
            tool_name,
        )
        return CondenseOutcome(
            content=content,
            original_chars=original_chars,
            error="timeout",
        )
    except Exception:
        logger.exception("[life] tool-result condense failed for %s", tool_name)
        return CondenseOutcome(
            content=content,
            original_chars=original_chars,
            error="exception",
        )

    summary = (summary or "").strip()
    if not summary:
        return CondenseOutcome(
            content=content,
            original_chars=original_chars,
            error="empty",
        )
    if len(summary) >= original_chars:
        logger.info(
            "[life] tool-result condense did not shorten %s: %s -> %s chars",
            tool_name,
            original_chars,
            len(summary),
        )
        return CondenseOutcome(
            content=content,
            original_chars=original_chars,
            condensed_chars=len(summary),
            error="not_shorter",
        )

    result_id = store.save(session_id, tool_name, content)
    condensed_content = (
        f"<压缩后信息；原始结果已保存为 {result_id}。需要上面没有的细节时，"
        f'调用 read_tool_result(result_id="{result_id}") 分段读取原文，或加 keyword 参数直接查找。>\n'
        f"{summary}"
    )
    logger.info(
        "[life] tool-result condensed %s: %s -> %s chars, stored as %s",
        tool_name,
        original_chars,
        len(summary),
        result_id,
    )
    return CondenseOutcome(
        content=condensed_content,
        condensed=True,
        result_id=result_id,
        original_chars=original_chars,
        condensed_chars=len(summary),
    )
