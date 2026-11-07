"""Registration and lifecycle of background sub-agent tasks.

The tool returns as soon as a task is registered; the run itself continues in
an ``asyncio`` task and reports back through the task record. Finished tasks
stay queryable for a while so ``subagent_status`` can be asked afterwards.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from bot import logger
from bot.life.subagent.models import SubAgentOutcome, SubAgentTask

SubAgentRunner = Callable[[SubAgentTask], Awaitable[SubAgentOutcome]]
SubAgentFinished = Callable[[SubAgentTask], None]


class SubAgentTaskManager:
    """Bounded registry of background sub-agent tasks."""

    def __init__(
        self,
        *,
        max_running: int = 3,
        max_running_per_session: int = 2,
        finished_keep: int = 20,
        timeout_seconds: float = 120.0,
    ) -> None:
        """Initialize an empty registry.

        Args:
            max_running: Tasks allowed to run at the same time, across sessions.
            max_running_per_session: Tasks one session may run at the same time.
            finished_keep: Finished tasks kept per session.
            timeout_seconds: Hard timeout of one task run.
        """
        self.max_running = max(1, int(max_running))
        self.max_running_per_session = max(1, int(max_running_per_session))
        self.finished_keep = max(1, int(finished_keep))
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self._tasks: dict[str, SubAgentTask] = {}
        self._counter = 0

    def start(
        self,
        *,
        session_id: str,
        description: str,
        granted_tools: list[str],
        runner: SubAgentRunner,
        on_finished: SubAgentFinished,
    ) -> SubAgentTask | None:
        """Register a task and start it in the background.

        Args:
            session_id: Unified message origin that asked for the task.
            description: Task text handed to the sub-agent.
            granted_tools: Names of the tools the sub-agent may use.
            runner: Coroutine that performs the run.
            on_finished: Callback invoked once the task reached a terminal state.

        Returns:
            The registered task, or None when the concurrency limits are hit.
        """
        if self._running_total() >= self.max_running:
            return None
        if self._running_for(session_id) >= self.max_running_per_session:
            return None
        self._counter += 1
        task = SubAgentTask(
            task_id=f"subagent-{self._counter}",
            session_id=session_id,
            description=description,
            granted_tools=list(granted_tools),
        )
        self._tasks[task.task_id] = task
        task.asyncio_task = asyncio.create_task(
            self._run(task, runner, on_finished),
            name=f"life_{task.task_id}",
        )
        self._drop_old_finished()
        return task

    async def _run(
        self,
        task: SubAgentTask,
        runner: SubAgentRunner,
        on_finished: SubAgentFinished,
    ) -> None:
        """Run one task, record its outcome and notify the owner.

        Args:
            task: Registered task.
            runner: Coroutine that performs the run.
            on_finished: Callback invoked once the task reached a terminal state.
        """
        try:
            outcome = await asyncio.wait_for(runner(task), timeout=self.timeout_seconds)
        except asyncio.CancelledError:
            task.status = "stopped"
            task.finished_at = time.time()
            raise
        except asyncio.TimeoutError:
            task.status = "failed"
            task.error = f"timeout after {self.timeout_seconds:.0f}s"
        except Exception as exc:
            logger.exception("[life] subagent %s failed", task.task_id)
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
        else:
            task.conclusion = outcome.conclusion.strip()
            task.tool_usage = dict(outcome.tool_usage)
            task.status = "completed" if task.conclusion else "failed"
            if not task.conclusion:
                task.error = "sub-agent returned no conclusion"
        task.finished_at = time.time()
        self._drop_old_finished()
        try:
            on_finished(task)
        except Exception:
            logger.exception("[life] subagent finish callback failed")

    def get(self, task_id: str) -> SubAgentTask | None:
        """Return one task by id.

        Args:
            task_id: Id returned by :meth:`start`.

        Returns:
            The task, or None when it was never registered or already dropped.
        """
        return self._tasks.get(task_id.strip())

    def list_for(self, session_id: str, limit: int = 0) -> list[SubAgentTask]:
        """Return a session's tasks, newest first.

        Args:
            session_id: Unified message origin of the session.
            limit: Maximum tasks returned; 0 returns all kept tasks.

        Returns:
            Tasks belonging to the session.
        """
        tasks = [task for task in self._tasks.values() if task.session_id == session_id]
        if limit > 0:
            tasks = tasks[-limit:]
        return list(reversed(tasks))

    def stop(self, task_id: str) -> bool:
        """Cancel a running task.

        Args:
            task_id: Id returned by :meth:`start`.

        Returns:
            True when the task was running and is now cancelled.
        """
        task = self._tasks.get(task_id.strip())
        if task is None or task.status != "running" or task.asyncio_task is None:
            return False
        task.asyncio_task.cancel()
        task.status = "stopped"
        task.finished_at = time.time()
        return True

    async def cancel_all(self) -> None:
        """Cancel every running task, used when the Life service stops."""
        pending = [
            task.asyncio_task
            for task in self._tasks.values()
            if task.asyncio_task is not None and task.status == "running"
        ]
        for task in self._tasks.values():
            self.stop(task.task_id)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def stats(self) -> dict:
        """Return counters for the status endpoint.

        Returns:
            Running count and per-status totals.
        """
        counts = {"running": 0, "completed": 0, "failed": 0, "stopped": 0}
        for task in self._tasks.values():
            counts[task.status] = counts.get(task.status, 0) + 1
        return {"running": counts.pop("running"), **counts, "kept": len(self._tasks)}

    def _running_total(self) -> int:
        """Return how many tasks are currently running.

        Returns:
            Number of running tasks.
        """
        return sum(1 for task in self._tasks.values() if task.status == "running")

    def _running_for(self, session_id: str) -> int:
        """Return how many tasks one session is currently running.

        Args:
            session_id: Unified message origin of the session.

        Returns:
            Number of running tasks for that session.
        """
        return sum(
            1
            for task in self._tasks.values()
            if task.status == "running" and task.session_id == session_id
        )

    def _drop_old_finished(self) -> None:
        """Drop the oldest finished tasks beyond ``finished_keep`` per session.

        Returns:
            None.
        """
        by_session: dict[str, list[SubAgentTask]] = {}
        for task in self._tasks.values():
            if task.status != "running":
                by_session.setdefault(task.session_id, []).append(task)
        for session_tasks in by_session.values():
            for task in session_tasks[
                : max(0, len(session_tasks) - self.finished_keep)
            ]:
                self._tasks.pop(task.task_id, None)
