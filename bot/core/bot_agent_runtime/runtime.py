"""Own all built-in sessions; Core never owns their runners or history writes."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from bot import logger
from bot.core.agent.runtime import AgentInput, AgentRuntime, AgentSession, AgentTurn
from bot.core.platform.agent_wakeup_event import AgentWakeupEvent

from .main_session import MainAgentSession

if TYPE_CHECKING:
    from bot.core.agent.config_store import AgentConfigStore
    from bot.core.star.context import Context


class BotAgentRuntime(AgentRuntime):
    """LKMBot host services around native main execution."""

    def __init__(
        self,
        context: Context,
        *,
        agent_configs: AgentConfigStore,
    ) -> None:
        """Bind native execution to explicit configuration services.

        Args:
            context: Host provider, plugin, platform and conversation services.
            agent_configs: Shared storage for reusable agent configurations.
        """
        self.context = context
        self.agent_configs = agent_configs
        self._sessions: dict[tuple[str, str], AgentSession] = {}
        self._opening = asyncio.Lock()
        self._active_main: dict[str, AgentTurn] = {}
        self._closed = False

    def normalize_config(self, config: dict, *, delegated: bool = False) -> dict:
        """Validate native options and apply role-appropriate defaults.

        Args:
            config: Native model, persona, execution and delegation options.
            delegated: Whether to retain only reusable persona/model overrides.

        Returns:
            Validated native configuration.
        """
        from bot.core.config.agent import normalize_agent

        return normalize_agent(
            {"runtime": "bot", "config": config}, delegated=delegated
        )["config"]

    async def initialize(self) -> None:
        """Initialize native execution environments before plugins can use them."""
        if sys.platform == "win32" and any(
            config.get("provider_settings", {}).get("computer_use_runtime") == "local"
            for config in self.context.bot_config_mgr.confs.values()
        ):
            try:
                from .computer.process_sandbox.windows_setup import (
                    initialize_windows_sandbox,
                )

                await asyncio.to_thread(initialize_windows_sandbox)
            except Exception as exc:
                logger.warning(f"Windows Local sandbox is unavailable: {exc}")

    async def open_session(
        self,
        umo: str,
        *,
        session_id: str | None = None,
        options: dict | None = None,
        input: AgentInput | None = None,
    ) -> AgentSession:
        """Bind the current main conversation or restore an exact identity.

        Args:
            umo: Source selected by Core.
            session_id: Exact conversation to restore, or None for current main.
            options: Runtime-specific session options.
            input: Initial event used only to create a missing main conversation.

        Returns:
            The sole live owner of the requested native conversation.

        Raises:
            ValueError: If the runtime is closed, the session is missing, or ownership differs.
        """
        if self._closed:
            raise ValueError("LKMBot agent runtime is closed")
        if input is not None and isinstance(input.event, AgentWakeupEvent):
            target = input.event.agent_session
            if target is not None:
                if session_id is not None and session_id != target.session_id:
                    raise ValueError("Wakeup and runtime session targets differ")
                session_id = target.session_id
        manager = self.context.conversation_manager
        conversation = None
        profile = (options or {}).get("profile", "main")
        if session_id is not None and "profile" not in (options or {}):
            conversation = await manager.db.conversation_store.get_metadata(session_id)
        if profile != "main":
            raise ValueError(f"Unknown built-in agent session profile: {profile}")
        select_current = session_id is None
        if select_current and input is not None:
            request = input.event.get_extra("provider_request")
            if request is not None and request.conversation is not None:
                session_id = request.conversation.cid
                select_current = False
        if select_current:
            session_id = await manager.get_curr_conversation_id(umo)
        if conversation is None and session_id is not None:
            conversation = await manager.db.conversation_store.get_metadata(session_id)
        if conversation is None and select_current:
            # Only first creation needs serialization; recheck after acquiring the
            # lock so concurrent first messages cannot create competing sessions.
            async with self._opening:
                if self._closed:
                    raise ValueError("LKMBot agent runtime is closed")
                session_id = await manager.get_curr_conversation_id(umo)
                conversation = (
                    await manager.db.conversation_store.get_metadata(session_id)
                    if session_id is not None
                    else None
                )
                if conversation is None:
                    session_id = await manager.new_conversation(
                        umo,
                        platform_id=input.event.get_platform_id() if input else None,
                    )
                    conversation = await manager.db.conversation_store.get_metadata(
                        session_id
                    )
        if conversation is None or conversation.umo != umo:
            raise ValueError("Agent conversation is missing or belongs to another UMO")
        # Existing conversations perform all database reads outside this registry
        # lock. Their execution lock controls when fresh history is loaded.
        async with self._opening:
            if self._closed:
                raise ValueError("LKMBot agent runtime closed during session creation")
            session = self._sessions.get((profile, session_id))
            if session is None or session.closed:
                if session is not None:
                    await session.close()
                if self._closed:
                    raise ValueError("LKMBot agent runtime is closed")
                session = MainAgentSession(self, session_id, umo)
                self._sessions[(profile, session_id)] = session
            elif session.umo != umo:
                raise ValueError("Agent session belongs to another UMO")
            return session

    def active_turn(self, umo: str) -> AgentTurn | None:
        """Find main's active execution without exposing its native runner.

        Args:
            umo: Source of an arriving message or wakeup.

        Returns:
            The current turn handle, or None while no main turn can accept steering.
        """
        return self._active_main.get(umo)

    async def close(self) -> None:
        """Stop all sessions before releasing the runtime's host services."""
        from .computer.computer_client import shutdown_local_booter

        self._closed = True
        async with self._opening:
            sessions = list(self._sessions.values())
        try:
            await asyncio.gather(*(session.close() for session in sessions))
        finally:
            self._sessions.clear()
            await shutdown_local_booter()
