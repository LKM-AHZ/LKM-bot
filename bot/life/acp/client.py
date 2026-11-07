"""Subprocess side of the ACP client: handshake, sessions and one prompt.

The ACP agent is launched once and kept alive; each chat gets its own ACP
session id so the external harness keeps its own context. The bot only reads
text chunks and the stop reason; tool calls and plans are ignored.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from bot import logger
from bot.life.acp.protocol import AcpConnection, AcpError, LineTransport


class SubprocessTransport:
    """ACP line transport over a child process's stdio."""

    def __init__(
        self,
        command: str,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        """Configure the child process.

        Args:
            command: Executable to launch.
            args: Arguments.
            env: Extra environment variables layered over the current one.
            cwd: Working directory of the child.
        """
        self.command = command
        self.args = list(args)
        self.env = dict(env or {})
        self.cwd = cwd
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self.stderr_tail: deque[str] = deque(maxlen=20)

    @property
    def alive(self) -> bool:
        """Return whether the child process is still running."""
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """Launch the child process.

        Raises:
            RuntimeError: The process could not be launched.
        """
        env = None
        if self.env:
            import os

            env = {**os.environ, **self.env}
        self._process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.cwd or None,
        )
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name="life_acp_stderr"
        )

    async def read_line(self) -> str | None:
        """Read one stdout line.

        Returns:
            The line without its newline, or None at end of stream.
        """
        process = self._process
        if process is None or process.stdout is None:
            return None
        line = await process.stdout.readline()
        if not line:
            return None
        return line.decode("utf-8", "replace").rstrip("\r\n")

    async def write_line(self, line: str) -> None:
        """Write one line to stdin.

        Args:
            line: JSON payload without a newline.

        Raises:
            RuntimeError: The child is not running.
        """
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise RuntimeError("ACP process is not running")
        process.stdin.write((line + "\n").encode("utf-8"))
        await process.stdin.drain()

    async def close(self) -> None:
        """Close stdin, then terminate and finally kill the child."""
        process = self._process
        if process is not None:
            try:
                if process.stdin is not None and not process.stdin.is_closing():
                    process.stdin.close()
            except Exception:
                logger.debug("[life] ACP stdin close failed", exc_info=True)
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

    async def _read_stderr(self) -> None:
        """Drain stderr so the child never blocks on a full pipe."""
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                self.stderr_tail.append(text)
                logger.debug("[life] acp stderr: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("[life] ACP stderr reader stopped", exc_info=True)


class AcpClient:
    """One ACP agent process: handshake, sessions and prompts."""

    def __init__(
        self,
        transport: LineTransport,
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        """Initialize the client without launching anything.

        Args:
            transport: Line transport to the agent process.
            timeout_seconds: Default request timeout.
        """
        self.transport = transport
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.capabilities: dict[str, Any] = {}
        self.connection = AcpConnection(
            transport,
            on_notification=self._on_notification,
            on_request=self._on_request,
        )
        self._chunks: list[str] = []
        self._session_id = ""

    async def start(self, client_name: str = "lkmbot") -> None:
        """Start the reader, then perform the initialize handshake.

        Args:
            client_name: Name reported to the external agent.

        Raises:
            ValueError: The peer did not negotiate ACP v1.
        """
        starter = getattr(self.transport, "start", None)
        if callable(starter):
            await starter()
        self.connection.start()
        result = await self.connection.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": client_name, "version": "1"},
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            },
            self.timeout_seconds,
        )
        if result.get("protocolVersion") != 1:
            raise ValueError("external agent did not negotiate ACP v1")
        self.capabilities = result.get("agentCapabilities") or {}

    async def new_session(self, cwd: str) -> str:
        """Create one ACP session.

        Args:
            cwd: Working directory handed to the agent.

        Returns:
            The session id.
        """
        result = await self.connection.request(
            "session/new",
            {"cwd": cwd, "mcpServers": []},
            self.timeout_seconds,
        )
        session_id = str(result.get("sessionId") or "")
        if not session_id:
            raise AcpError({"code": -32603, "message": "session/new returned no id"})
        self._session_id = session_id
        return session_id

    async def prompt(
        self,
        session_id: str,
        text: str,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[str, str]:
        """Send one prompt and collect its text chunks.

        Args:
            session_id: ACP session to use.
            text: Prompt text.
            timeout_seconds: Override of the default timeout.

        Returns:
            ``(collected_text, stop_reason)``.
        """
        self._chunks = []
        result = await self.connection.request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            },
            timeout_seconds or self.timeout_seconds,
        )
        return "".join(self._chunks).strip(), str(result.get("stopReason") or "")

    async def cancel(self, session_id: str) -> None:
        """Ask the agent to cancel the running turn.

        Args:
            session_id: ACP session to cancel.
        """
        try:
            await self.connection.notify("session/cancel", {"sessionId": session_id})
        except Exception:
            logger.debug("[life] ACP cancel failed", exc_info=True)

    async def close(self) -> None:
        """Close the connection and the child process."""
        await self.connection.close()

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        """Collect agent text chunks.

        Args:
            method: Notification method.
            params: Notification parameters.
        """
        if method != "session/update":
            return
        update = params.get("update") or {}
        if update.get("sessionUpdate") != "agent_message_chunk":
            return
        content = update.get("content") or {}
        if content.get("type") == "text":
            self._chunks.append(str(content.get("text") or ""))

    async def _on_request(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Answer peer requests; the Life layer grants no filesystem or terminal.

        Args:
            method: Request method.
            params: Request parameters.

        Returns:
            A result object for known requests.

        Raises:
            AcpError: For anything the Life layer does not implement.
        """
        if method == "session/request_permission":
            return {"outcome": {"outcome": "cancelled"}}
        raise AcpError({"code": -32601, "message": f"unsupported request {method}"})


def build_client(config: Any, cwd: str) -> AcpClient:
    """Create a client with a subprocess transport from the configuration.

    Args:
        config: ``AcpConfig``.
        cwd: Working directory for the child process.

    Returns:
        An unstarted client.
    """
    transport = SubprocessTransport(
        config.command,
        list(config.args),
        env=dict(config.env),
        cwd=cwd,
    )
    return AcpClient(transport, timeout_seconds=config.timeout_seconds)


__all__ = ["AcpClient", "SubprocessTransport", "build_client"]
