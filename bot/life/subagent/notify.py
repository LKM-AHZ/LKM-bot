"""Prompt hook that delivers finished sub-agent conclusions.

The hook owns no scheduling: it only drains the session inbox when a prompt is
being assembled. Notices are consumed on injection, so a conclusion is shown
once; a turn that never reaches the model keeps it for the next turn.
"""

from __future__ import annotations

from typing import Any

from bot.life.common import (
    append_prompt_block,
    event_umo,
    has_prompt_block,
)
from bot.life.config import SubAgentConfig
from bot.life.subagent.inbox import SubAgentInbox

SUBAGENT_TAG = "life_subagent"


class SubAgentNotifier:
    """Inject pending sub-agent conclusions as a ``<life_subagent>`` block."""

    def __init__(self, config: SubAgentConfig, inbox: SubAgentInbox) -> None:
        """Initialize the notifier.

        Args:
            config: Sub-agent configuration.
            inbox: Inbox holding undelivered conclusions.
        """
        self.config = config
        self.inbox = inbox
        self.injected = 0

    async def inject(self, event: Any, req: Any) -> bool:
        """Append pending conclusions to the system prompt.

        Args:
            event: Bot message event being processed.
            req: ``ProviderRequest`` about to be sent to the model.

        Returns:
            True when the system prompt was modified.
        """
        umo = event_umo(event)
        if not umo or has_prompt_block(req, SUBAGENT_TAG):
            return False
        notices = self.inbox.take(umo, limit=self.config.inject_max_items)
        if not notices:
            return False

        lines: list[str] = []
        for notice in notices:
            conclusion = notice.conclusion.strip()
            if not conclusion:
                continue
            lines.append(f"- [{notice.task_id}] {notice.description}：{conclusion}")
        block = (
            f"<{SUBAGENT_TAG}>\n"
            "后台子任务已经完成，结论可以直接使用，不必再派发同一件事：\n"
            + "\n".join(lines)
            + f"\n</{SUBAGENT_TAG}>"
        )
        if len(block) > self.config.inject_max_chars:
            block = block[: self.config.inject_max_chars].rstrip() + "…"
        if not append_prompt_block(req, SUBAGENT_TAG, block, separator="\n\n"):
            return False
        self.injected += 1
        return True

    def status(self) -> dict:
        """Return counters for the status endpoint.

        Returns:
            Config bounds, injection count and inbox size.
        """
        return {
            "enabled": True,
            "injected": self.injected,
            "inject_max_items": self.config.inject_max_items,
            "inbox": self.inbox.stats(),
        }
