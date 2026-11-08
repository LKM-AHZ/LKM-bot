"""People service: observe members, ingest facts and build profiles.

The service is the surface the runtime uses: the ingest flush reports who spoke,
the memory digest reports extracted facts, and the ``get_person_profile`` tool
asks for a profile. Profiles are cached so a chatty group does not trigger one
LLM call per question, and every profile keeps the fact ids it was built from so
the answer stays explainable and deletable.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Sequence

from bot import logger
from bot.core.provider.entities import ProviderType
from bot.life.common import SessionScoped
from bot.life.config import PersonConfig
from bot.life.person.names import clean_name, derive_person_id, normalize_name
from bot.life.person.store import PersonStore

_PREFERENCE_HINTS = ("喜欢", "讨厌", "爱吃", "爱喝", "偏好", "不喜欢", "习惯")
_IDENTITY_HINTS = ("是", "叫", "来自", "住在", "职业")
_RELATION_HINTS = ("朋友", "同事", "同学", "家人", "认识", "关系")


def infer_category(content: str) -> str:
    """Classify a fact by simple keyword hints.

    Args:
        content: Fact text.

    Returns:
        ``preference``, ``identity``, ``relation`` or ``other``.
    """
    if any(hint in content for hint in _PREFERENCE_HINTS):
        return "preference"
    if any(hint in content for hint in _RELATION_HINTS):
        return "relation"
    if any(hint in content for hint in _IDENTITY_HINTS):
        return "identity"
    return "other"


class PersonService(SessionScoped):
    """Runtime owner of people, facts and profiles."""

    def __init__(
        self,
        context: Any,
        config: PersonConfig,
        db_helper: Any,
        life_config: Any = None,
    ) -> None:
        """Initialize the service without touching the database.

        Args:
            context: Star context exposing ``provider_manager``.
            config: Person settings.
            db_helper: Core async database helper.
            life_config: Root Life configuration used for the session allowlist.
        """
        self.context = context
        self.config = config
        self.life_config = life_config
        self.store = PersonStore(db_helper, config)
        self._started = False
        self._counters = {
            "observed": 0,
            "facts": 0,
            "conflicts": 0,
            "profiles": 0,
            "profile_llm_failures": 0,
        }

    @property
    def enabled(self) -> bool:
        """Return whether the person runtime is active."""
        return self._started

    async def initialize(self) -> None:
        """Create the person tables and mark the runtime ready."""
        await self.store.ensure_schema()
        self._started = True
        logger.info("[life] person layer ready")

    async def close(self) -> None:
        """Mark the runtime stopped."""
        self._started = False

    async def observe_messages(self, messages: Sequence[Any]) -> int:
        """Record who spoke in a batch of archived messages.

        Args:
            messages: Ingested messages from the archive flush.

        Returns:
            Number of people written.
        """
        if not self._started or not messages:
            return 0
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for message in messages:
            session_key = getattr(message, "session_key", "")
            if not session_key or not self.session_enabled(session_key):
                continue
            person_id = derive_person_id(
                getattr(message, "platform_id", "") or "",
                getattr(message, "sender_id", None),
                getattr(message, "sender_name", "") or "",
            )
            key = (session_key, person_id)
            entry = grouped.get(key)
            if entry is None:
                grouped[key] = {
                    "session_key": session_key,
                    "person_id": person_id,
                    "display_name": getattr(message, "sender_name", "") or "",
                    "group_id": getattr(message, "group_id", None),
                    "ts": int(getattr(message, "ts", 0) or 0),
                    "count": 1,
                }
            else:
                entry["count"] += 1
                entry["ts"] = max(entry["ts"], int(getattr(message, "ts", 0) or 0))
        written = await self.store.observe_batch(list(grouped.values()))
        self._counters["observed"] += written
        return written

    async def ingest_facts(
        self,
        session_key: str,
        facts: Sequence[dict[str, Any]],
    ) -> dict[str, int]:
        """Attach extracted facts to people.

        Args:
            session_key: Session the facts belong to.
            facts: Fact rows from the memory digest; ``person_id`` is a name.

        Returns:
            Counters with ``inserted`` and ``conflicts``.
        """
        if not self._started or not facts:
            return {"inserted": 0, "conflicts": 0}
        if not self.session_enabled(session_key):
            return {"inserted": 0, "conflicts": 0}
        rows: list[dict[str, Any]] = []
        for fact in facts:
            name = clean_name(str(fact.get("person_id") or ""))
            content = str(fact.get("content") or "").strip()
            if not name or not content:
                continue
            person = await self.store.find(session_key, name)
            if person is None:
                platform = (
                    session_key.split(":")[0] if ":" in session_key else "unknown"
                )
                person_id = derive_person_id(platform, None, name)
                await self.store.observe_batch(
                    [
                        {
                            "session_key": session_key,
                            "person_id": person_id,
                            "display_name": name,
                            "group_id": None,
                            "ts": int(time.time()),
                            "count": 1,
                        }
                    ]
                )
            else:
                person_id = str(person["person_id"])
            rows.append(
                {
                    "session_key": session_key,
                    "person_id": person_id,
                    "content": content,
                    "category": infer_category(content),
                    "confidence": float(
                        fact.get("importance") or fact.get("confidence") or 0.6
                    ),
                    "source_memory_id": fact.get("memory_id"),
                }
            )
        result = await self.store.add_facts(rows)
        self._counters["facts"] += result["inserted"]
        self._counters["conflicts"] += result["conflicts"]
        return result

    async def resolve(self, session_key: str, query: str) -> dict[str, Any] | None:
        """Resolve a person by id, name or alias.

        Args:
            session_key: Session scope.
            query: Person id or name.

        Returns:
            Person row, or None when unknown.
        """
        if not self._started:
            return None
        return await self.store.find(session_key, query)

    async def profile(
        self,
        session_key: str,
        query: str,
        refresh: bool = False,
    ) -> dict[str, Any] | None:
        """Build or return the cached profile of one person.

        Args:
            session_key: Session scope.
            query: Person id, name or alias.
            refresh: Rebuild even when the cache is still fresh.

        Returns:
            Profile dictionary with facts, or None when the person is unknown.
        """
        if not self._started:
            return None
        person = await self.store.find(session_key, query)
        if person is None:
            return None
        session_key = str(person["session_key"])
        person_id = str(person["person_id"])
        cached = await self.store.get_profile(session_key, person_id)
        ttl = max(int(self.config.profile_ttl_minutes), 0) * 60
        fresh = cached is not None and (time.time() - int(cached["updated_at"])) < ttl
        if cached is not None and fresh and not refresh:
            return self._profile_payload(person, cached)

        facts = await self.store.list_facts(
            session_key,
            person_id,
            limit=max(int(self.config.profile_max_facts), 1),
        )
        usable = [
            fact
            for fact in facts
            if fact["effective_confidence"] >= float(self.config.min_confidence)
        ]
        if not usable:
            payload = self._profile_payload(
                person,
                {
                    "summary": "",
                    "highlight": [],
                    "fact_ids": [],
                    "generator": "extractive",
                    "updated_at": int(time.time()),
                },
            )
            return payload
        highlight = [
            f"{fact['content']}（{fact['category']}，置信 "
            f"{fact['effective_confidence']:.2f}）"
            for fact in usable
        ]
        summary = ""
        generator = "extractive"
        if self.config.use_llm_profile:
            summary = await self._llm_summary(person, usable)
            generator = "llm" if summary else "extractive"
        if not summary:
            summary = self._extractive_summary(person, usable)
        await self.store.set_profile(
            session_key,
            person_id,
            summary,
            highlight,
            [int(fact["id"]) for fact in usable],
            generator,
        )
        self._counters["profiles"] += 1
        return self._profile_payload(
            person,
            {
                "summary": summary,
                "highlight": highlight,
                "fact_ids": [int(fact["id"]) for fact in usable],
                "generator": generator,
                "updated_at": int(time.time()),
            },
            facts=usable,
        )

    def _profile_payload(
        self,
        person: dict[str, Any],
        profile: dict[str, Any],
        facts: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Assemble the profile return value.

        Args:
            person: Person row.
            profile: Profile row.
            facts: Facts the profile was built from.

        Returns:
            JSON-serializable profile dictionary.
        """
        return {
            "person_id": person["person_id"],
            "display_name": person["display_name"],
            "aliases": list(person.get("aliases") or []),
            "relation_score": round(float(person.get("relation_score") or 0.0), 2),
            "interactions": int(person.get("interactions") or 0),
            "last_seen_ts": int(person.get("last_seen_ts") or 0),
            "summary": profile.get("summary") or "",
            "highlight": list(profile.get("highlight") or []),
            "fact_ids": list(profile.get("fact_ids") or []),
            "generator": profile.get("generator") or "extractive",
            "updated_at": int(profile.get("updated_at") or 0),
            "facts": [
                {
                    "id": fact["id"],
                    "content": fact["content"],
                    "category": fact["category"],
                    "confidence": fact["effective_confidence"],
                    "status": fact["status"],
                }
                for fact in (facts or [])
            ],
        }

    async def _llm_summary(
        self,
        person: dict[str, Any],
        facts: Sequence[dict[str, Any]],
    ) -> str:
        """Ask the chat model for a short profile paragraph.

        Args:
            person: Person row.
            facts: Usable facts of the person.

        Returns:
            Summary text, or an empty string when the model is unavailable.
        """
        manager = getattr(self.context, "provider_manager", None)
        if manager is None:
            return ""
        try:
            provider = await manager.get_using_provider_async(
                ProviderType.CHAT_COMPLETION
            )
        except Exception:
            provider = None
        if provider is None:
            return ""
        lines = "\n".join(f"- {fact['content']}" for fact in facts)
        prompt = (
            f"根据下面这些已确认的事实，用两三句中文描述「{person['display_name']}」。\n"
            "要求：第三人称；只能使用给定事实；不要编造；不要罗列条目；不要加标题。\n\n"
            f"事实：\n{lines}"
        )
        try:
            async with asyncio.timeout(30):
                response = await provider.text_chat(
                    prompt=prompt,
                    system_prompt="你是严谨的人物小传撰写助手，只依据给定事实。",
                )
        except Exception as exc:
            self._counters["profile_llm_failures"] += 1
            logger.warning(f"[life] person profile LLM failed: {exc}")
            return ""
        return (response.completion_text or "").strip()[:600]

    def _extractive_summary(
        self,
        person: dict[str, Any],
        facts: Sequence[dict[str, Any]],
    ) -> str:
        """Compose a deterministic summary from the top facts.

        Args:
            person: Person row.
            facts: Usable facts of the person.

        Returns:
            Summary text.
        """
        name = person.get("display_name") or person.get("person_id")
        joined = "；".join(fact["content"] for fact in facts[:5])
        return f"{name}：{joined}"

    async def forget(
        self,
        session_key: str,
        person: str,
        fact_id: int | None = None,
    ) -> dict[str, int]:
        """Delete one fact or the whole person.

        Args:
            session_key: Session scope.
            person: Person id, name or alias.
            fact_id: Delete only this fact when given.

        Returns:
            Deletion counters; all zeros when the person is unknown.
        """
        if not self._started:
            return {"people": 0, "facts": 0, "profiles": 0}
        if fact_id is not None:
            return {
                "people": 0,
                "facts": 1 if await self.store.delete_fact(fact_id) else 0,
                "profiles": 0,
            }
        found = await self.store.find(session_key, person)
        if found is None:
            return {"people": 0, "facts": 0, "profiles": 0}
        return await self.store.delete_person(
            str(found["session_key"]),
            str(found["person_id"]),
        )

    async def resolve_fact(self, fact_id: int, keep: bool) -> int:
        """Resolve one conflicted fact as a reviewer.

        Args:
            fact_id: Fact the reviewer decided on.
            keep: Keep the fact and supersede its rivals, or reject it.

        Returns:
            Number of facts whose status changed.
        """
        if not self._started:
            return 0
        return await self.store.resolve_fact(fact_id, keep)

    async def status(self) -> dict[str, Any]:
        """Return a serializable status snapshot.

        Returns:
            Mapping with counters, storage statistics and configuration.
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
            "counters": dict(self._counters),
            "config": {
                "confidence_half_life_days": self.config.confidence_half_life_days,
                "min_confidence": self.config.min_confidence,
                "relation_half_life_days": self.config.relation_half_life_days,
                "profile_ttl_minutes": self.config.profile_ttl_minutes,
            },
        }


def normalize_query(query: str) -> str:
    """Normalize a tool query before person resolution.

    Args:
        query: Raw user input.

    Returns:
        Cleaned query text.
    """
    return normalize_name(query) or query.strip()
