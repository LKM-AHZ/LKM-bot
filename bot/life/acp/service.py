"""Life-layer wrapper around one ACP agent process.

The process is shared by every chat (one subprocess, serialized prompts), while
each chat gets its own ACP session id so the external harness keeps its own
context. Any failure closes the process and returns None: the caller then falls
back to the local pipeline.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from bot import logger
from bot.life.acp.client import AcpClient, build_client
from bot.life.config import AcpConfig


class AcpService:
    """Serialized access to one external ACP agent."""

    def __init__(
        self,
        config: AcpConfig,
        data_dir: Path,
    ) -> None:
        """Initialize the service without launching anything.

        Args:
            config: ACP settings.
            data_dir: Life data directory; the default working directory lives
                under ``<data_dir>/acp``.
        """
        self.config = config
        self.data_dir = Path(data_dir)
        self._client: AcpClient | None = None
        self._sessions: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._counters = {
            "asks": 0,
            "ok": 0,
            "empty": 0,
            "failed": 0,
            "timeouts": 0,
            "chars": 0,
        }

    @property
    def enabled(self) -> bool:
        """Return whether an ACP agent is configured and switched on."""
        return bool(self.config.enabled and self.config.command.strip())

    async def ask(self, session_key: str, prompt: str) -> str | None:
        """Send one prompt and return the agent's answer.

        Args:
            session_key: Chat the prompt belongs to; one ACP session per chat.
            prompt: Prompt text.

        Returns:
            The answer, or None when the adapter is off, the agent failed or
            returned nothing.
        """
        if not self.enabled:
            return None
        text = (prompt or "").strip()
        if not text:
            return None
        self._counters["asks"] += 1
        async with self._lock:
            try:
                answer = await self._ask_locked(session_key, text)
            except asyncio.TimeoutError:
                self._counters["timeouts"] += 1
                logger.warning("[life] ACP prompt timed out")
                await self._reset()
                return None
            except Exception as exc:
                self._counters["failed"] += 1
                logger.warning("[life] ACP prompt failed: %s", exc)
                await self._reset()
                return None
            if not answer:
                self._counters["empty"] += 1
                return None
            self._counters["ok"] += 1
            self._counters["chars"] += len(answer)
            return answer

    async def close(self) -> None:
        """Close the agent process."""
        await self._reset()

    def status(self) -> dict:
        """Return counters and configuration bounds.

        Returns:
            ACP status block.
        """
        return {
            "enabled": self.enabled,
            "takeover": bool(self.config.takeover),
            "command": self.config.command,
            "sessions": len(self._sessions),
            "connected": self._client is not None,
            "counters": dict(self._counters),
            "timeout_seconds": self.config.timeout_seconds,
            "max_output_chars": self.config.max_output_chars,
        }

    async def _ask_locked(self, session_key: str, prompt: str) -> str:
        """Run one prompt while holding the lock.

        Args:
            session_key: Chat the prompt belongs to.
            prompt: Prompt text.

        Returns:
            The capped answer.
        """
        client = await self._ensure_client()
        session_id = self._sessions.get(session_key)
        if not session_id:
            session_id = await client.new_session(self._work_dir())
            self._sessions[session_key] = session_id
        text, stop_reason = await client.prompt(
            session_id,
            prompt,
            timeout_seconds=self.config.timeout_seconds,
        )
        if stop_reason in {"cancelled", "refusal"} and not text:
            return ""
        limit = max(1, self.config.max_output_chars)
        return text[:limit]

    async def _ensure_client(self) -> AcpClient:
        """Return a live client, launching the process when needed.

        Returns:
            A started client.
        """
        if isinstance(self._client, AcpClient) and getattr(
            self._client.transport, "alive", False
        ):
            return self._client
        await self._reset()
        work_dir = self._work_dir()
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        client = build_client(self.config, work_dir)
        await client.start()
        self._client = client
        logger.info("[life] ACP agent started: %s", self.config.command)
        return client

    async def _reset(self) -> None:
        """Close the client and forget its sessions."""
        client = self._client
        self._client = None
        self._sessions.clear()
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("[life] ACP close failed", exc_info=True)

    def _work_dir(self) -> str:
        """Return the working directory for the agent process.

        Returns:
            Configured directory, or ``<life>/acp``.
        """
        if self.config.cwd:
            return self.config.cwd
        return str(self.data_dir / "acp")
