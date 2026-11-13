"""Jargon service: learn terms, review them, explain them in context.

The injection rule is deliberately narrow: a term is only explained when it
actually appears in the current conversation. That kills both failure modes the
plan calls out - "学了不用" (never surfaced) and "乱用" (plugging a glossary into
every reply, or using a term the bot only half understands).
"""

from __future__ import annotations

from functools import partial
from typing import Any, Sequence

from bot import logger
from bot.life.common import (
    SessionScoped,
    append_prompt_block,
    event_session_enabled,
    event_umo,
    has_prompt_block,
    llm_complete,
)
from bot.life.config import JargonConfig
from bot.life.jargon.learn import JargonLearner
from bot.life.jargon.store import JargonStore

_BLOCK_TAG = "life_jargon"


class JargonService(SessionScoped):
    """Runtime owner of slang terms."""

    def __init__(
        self,
        context: Any,
        config: JargonConfig,
        db_helper: Any,
        life_store: Any = None,
        life_config: Any = None,
    ) -> None:
        """Initialize the service without opening resources.

        Args:
            context: Star context exposing ``provider_manager``.
            config: Jargon settings.
            db_helper: Core async database helper.
            life_store: Archive store used as learning input.
            life_config: Root Life configuration used for the allowlist.
        """
        self.context = context
        self.config = config
        self.life_store = life_store
        self.life_config = life_config
        self.store = JargonStore(db_helper, config)
        self.learner: JargonLearner | None = None
        self._started = False
        self._counters = {
            "learned": 0,
            "merged": 0,
            "conflicts": 0,
            "injections": 0,
            "explained_terms": 0,
        }

    @property
    def enabled(self) -> bool:
        """Return whether the jargon runtime is active."""
        return self._started

    async def initialize(self) -> None:
        """Create the tables and wire the learner."""
        await self.store.ensure_schema()
        if self.life_store is not None:
            self.learner = JargonLearner(
                source=self.life_store,
                target=self.store,
                config=self.config,
                summarizer=(
                    partial(llm_complete, self.context) if self.config.use_llm else None
                ),
            )
        self._started = True
        logger.info("[life] jargon layer ready")

    async def close(self) -> None:
        """Mark the runtime stopped."""
        self._started = False

    async def learn_round(self, session_keys: Sequence[str] | None = None) -> dict:
        """Learn slang from the archive.

        Args:
            session_keys: Sessions to process; None discovers active ones.

        Returns:
            Round counters.
        """
        if not self._started or self.learner is None:
            return {"enabled": False}
        result = await self.learner.run(session_keys)
        self._counters["learned"] += int(result.get("inserted") or 0)
        self._counters["merged"] += int(result.get("updated") or 0)
        self._counters["conflicts"] += int(result.get("conflicts") or 0)
        return result

    async def inject(self, event: Any, req: Any) -> bool:
        """Explain slang terms that appear in the current prompt.

        Args:
            event: Bot message event carrying the session identity.
            req: ``ProviderRequest`` about to be sent to the model.

        Returns:
            True when the prompt was modified.
        """
        if not self._started or not self.config.enabled:
            return False
        umo = event_umo(event)
        if not umo or not event_session_enabled(self, event):
            return False
        if has_prompt_block(req, _BLOCK_TAG):
            return False
        prompt = (getattr(req, "prompt", "") or "").strip()
        if not prompt:
            return False

        terms = await self.store.list_terms(umo, statuses=["active"], limit=200)
        matched = [
            row
            for row in terms
            if float(row.get("confidence") or 0.0) >= self.config.min_confidence
            and str(row.get("term") or "")
            and str(row["term"]) in prompt
            and str(row.get("meaning") or "").strip()
        ]
        if not matched:
            return False
        matched.sort(
            key=lambda row: (len(str(row["term"])), -float(row["confidence"])),
            reverse=True,
        )
        selected = matched[: max(int(self.config.top_k), 1)]
        lines = [
            f"- {row['term']}：{str(row['meaning']).strip()[: self.config.max_meaning_chars]}"
            for row in selected
        ]
        block = (
            f"\n\n<{_BLOCK_TAG}>\n"
            "群里这些词是黑话，以下只是释义，供你理解对话；"
            "不要主动使用你没把握的黑话，也不要向用户解释这份列表。\n"
            + "\n".join(lines)
            + f"\n</{_BLOCK_TAG}>"
        )
        if not append_prompt_block(req, _BLOCK_TAG, block):
            return False
        await self.store.bump_usage([int(row["id"]) for row in selected])
        self._counters["injections"] += 1
        self._counters["explained_terms"] += len(selected)
        return True

    async def review(
        self,
        jargon_id: int,
        status: str,
        meaning: str | None = None,
    ) -> bool:
        """Confirm, reject or correct one term.

        Args:
            jargon_id: Row id.
            status: ``active``, ``pending`` or ``rejected``.
            meaning: Optional corrected explanation.

        Returns:
            True when the row was updated.
        """
        if not self._started:
            return False
        return await self.store.review(jargon_id, status, meaning)

    async def list_jargon(
        self,
        session_key: str,
        statuses: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return terms of one session.

        Args:
            session_key: Session scope.
            statuses: Optional status filter.
            limit: Maximum rows.

        Returns:
            Term rows.
        """
        if not self._started:
            return []
        return await self.store.list_terms(session_key, statuses=statuses, limit=limit)

    async def forget(
        self,
        jargon_ids: Sequence[int] | None = None,
        session_key: str | None = None,
    ) -> int:
        """Delete terms by id or session.

        Args:
            jargon_ids: Explicit row ids.
            session_key: Delete every term of one session.

        Returns:
            Number of deleted rows.
        """
        if not self._started:
            return 0
        return await self.store.delete(
            jargon_ids=list(jargon_ids or []) or None,
            session_key=session_key,
        )

    async def status(self) -> dict[str, Any]:
        """Return a serializable status snapshot.

        Returns:
            Mapping with storage, learning counters and configuration.
        """
        storage: dict[str, Any] = {}
        if self._started:
            try:
                storage = await self.store.stats()
            except Exception as exc:
                storage = {"error": str(exc)}
        return {
            "enabled": self._started,
            "configured": self.config.enabled,
            "storage": storage,
            "learning": self.learner.snapshot() if self.learner else {},
            "counters": dict(self._counters),
            "config": {
                "top_k": self.config.top_k,
                "min_confidence": self.config.min_confidence,
                "use_llm": self.config.use_llm,
            },
        }
