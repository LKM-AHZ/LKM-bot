"""Agent tools for background sub-agent tasks (S6b).

Three tools: dispatch a task, check it, stop it. The handlers stay thin; the
Life service owns capacity checks, tool grants and delivery of the conclusion.
"""

from __future__ import annotations

from typing import Any

from bot import logger
from bot.life.common import event_session_enabled, unregister_tools

_TOOL_NAMES = ("subagent", "subagent_status", "subagent_stop")


def register_subagent_tools(manager: Any, service: Any) -> None:
    """Register the three sub-agent tools on the core tool manager.

    Args:
        manager: ``FunctionToolManager`` exposed by the provider manager.
        service: Life service that runs the tasks.
    """

    async def subagent(event: Any, description: str, tools: str = "") -> str:
        """Start a background task that runs on its own and reports back."""
        if service.subagents is None:
            return "error: sub-agent is not enabled"
        if not event_session_enabled(service, event):
            return "error: sub-agent is not enabled for this session"
        return await service.start_subagent(
            event=event,
            description=str(description or ""),
            requested_tools=str(tools or ""),
        )

    async def subagent_status(event: Any, task_id: str = "") -> str:
        """Check one task, or list this chat's tasks."""
        if service.subagents is None:
            return "error: sub-agent is not enabled"
        if not event_session_enabled(service, event):
            return "error: sub-agent is not enabled for this session"
        umo = getattr(event, "unified_msg_origin", "") or ""
        if task_id.strip():
            task = service.subagents.get(task_id)
            if task is None or task.session_id != umo:
                return f"error: no task {task_id!r} in this chat"
            return _render_task(task.summary())
        tasks = service.subagents.list_for(umo, limit=10)
        if not tasks:
            return "No sub-agent tasks in this chat."
        return "\n".join(_render_task(task.summary()) for task in tasks)

    async def subagent_stop(event: Any, task_id: str) -> str:
        """Stop a running background task."""
        if service.subagents is None:
            return "error: sub-agent is not enabled"
        if not event_session_enabled(service, event):
            return "error: sub-agent is not enabled for this session"
        umo = getattr(event, "unified_msg_origin", "") or ""
        task = service.subagents.get(str(task_id))
        if task is None or task.session_id != umo:
            return f"error: no task {task_id!r} in this chat"
        return (
            "stopped"
            if service.subagents.stop(task.task_id)
            else (f"error: task {task.task_id} is not running (status={task.status})")
        )

    definitions = (
        (
            "subagent",
            [
                {
                    "type": "string",
                    "name": "description",
                    "description": (
                        "A self-contained task: the sub-agent cannot see this chat, "
                        "so include every name, id and link it needs."
                    ),
                },
                {
                    "type": "string",
                    "name": "tools",
                    "description": (
                        "Comma-separated tool names the sub-agent may use; "
                        "empty uses every tool this deployment allows."
                    ),
                },
            ],
            "Run one independent task in the background and get its conclusion in a later turn. "
            "Use it for research or lookups that would otherwise flood this chat with raw results.",
            subagent,
        ),
        (
            "subagent_status",
            [
                {
                    "type": "string",
                    "name": "task_id",
                    "description": "Task id such as subagent-3; empty lists recent tasks.",
                },
            ],
            "Check a background task, or list the recent ones in this chat.",
            subagent_status,
        ),
        (
            "subagent_stop",
            [
                {
                    "type": "string",
                    "name": "task_id",
                    "description": "Task id to stop.",
                },
            ],
            "Stop a running background task.",
            subagent_stop,
        ),
    )
    try:
        for name, args, desc, handler in definitions:
            manager.add_func(name=name, func_args=args, desc=desc, handler=handler)
    except Exception:
        logger.exception("[life] failed to register sub-agent tools")


def _render_task(summary: dict) -> str:
    """Render one task summary for the model.

    Args:
        summary: Mapping produced by ``SubAgentTask.summary``.

    Returns:
        One human-readable line.
    """
    line = (
        f"[{summary['task_id']}] {summary['status']} "
        f"({summary['elapsed_seconds']}s) {summary['description']}"
    )
    if summary.get("granted_tools"):
        line += f" tools={','.join(summary['granted_tools'])}"
    if summary.get("error"):
        line += f" error={summary['error']}"
    if summary.get("conclusion"):
        line += f" conclusion={summary['conclusion'][:300]}"
    return line


def unregister_subagent_tools(manager: Any) -> None:
    """Remove the sub-agent tools from the core tool manager.

    Args:
        manager: ``FunctionToolManager`` exposed by the provider manager.
    """
    unregister_tools(manager, _TOOL_NAMES, "sub-agent")
