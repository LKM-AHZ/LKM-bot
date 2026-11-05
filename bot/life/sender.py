"""Proactive sending channel for the Life layer.

Sending must work outside the current message event, so the sender prefers the
platform API directly and only falls back to the plugin context when one is
provided.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from bot import logger
from bot.core.message.message_event_result import MessageChain
from bot.core.platform.message_session import MessageSession


@dataclass(slots=True)
class SendResult:
    """Outcome of one proactive send attempt."""

    ok: bool
    platform_message_id: str | None = None
    error: str | None = None


class LifeSender:
    """Send message chains to a unified_msg_origin."""

    def __init__(
        self,
        platform_manager: Any = None,
        context: Any = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        """Initialize the sender.

        Args:
            platform_manager: Platform manager exposing ``platform_insts``.
            context: Optional plugin context exposing ``send_message``.
            timeout_seconds: Hard timeout for a single send.
        """
        self.platform_manager = platform_manager
        self.context = context
        self.timeout_seconds = timeout_seconds

    async def send_chain(self, umo: str, chain: MessageChain) -> SendResult:
        """Send a message chain to a session.

        Args:
            umo: Target unified_msg_origin.
            chain: Message chain to send.

        Returns:
            Send outcome; never raises for platform failures.
        """
        try:
            async with asyncio.timeout(self.timeout_seconds):
                if self.context is not None:
                    ok = await self.context.send_message(umo, chain)
                    return SendResult(ok=bool(ok))
                return await self._send_via_platform(umo, chain)
        except TimeoutError:
            logger.warning(
                f"[life] send timed out after {self.timeout_seconds}s: {umo}"
            )
            return SendResult(ok=False, error="timeout")
        except Exception as exc:
            logger.error(f"[life] send failed for {umo}: {exc}")
            return SendResult(ok=False, error=str(exc))

    async def _send_via_platform(self, umo: str, chain: MessageChain) -> SendResult:
        """Send through the matching platform instance.

        Args:
            umo: Target unified_msg_origin.
            chain: Message chain to send.

        Returns:
            Send outcome.
        """
        if self.platform_manager is None:
            return SendResult(ok=False, error="no platform manager")
        session = MessageSession.from_str(umo)
        for platform in self.platform_manager.platform_insts:
            if platform.meta().id == session.platform_name:
                await platform.send_by_session(session, chain)
                return SendResult(ok=True)
        return SendResult(
            ok=False, error=f"platform not found: {session.platform_name}"
        )

    @staticmethod
    def new_message_id() -> str:
        """Return a locally unique outbound message id."""
        return f"life-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
