"""Storage for people, facts and cached profiles."""

from __future__ import annotations

import math
import time
import uuid
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import col

from bot import logger
from bot.core.db import BaseDatabase
from bot.life.config import PersonConfig
from bot.life.person.models import LifePerson, LifePersonFact, LifePersonProfile
from bot.life.person.names import clean_name, name_tokens, normalize_name

_ACTIVE = "active"
_CONFLICTED = "conflicted"
_EXPIRED = "expired"


def fact_hash(content: str) -> str:
    """Return the dedupe hash of one fact.

    Args:
        content: Fact text.

    Returns:
        Hex digest used by the unique constraint.
    """
    import hashlib

    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def decay_factor(age_days: float, half_life_days: float) -> float:
    """Return the exponential decay factor for an age.

    Args:
        age_days: Age in days; negative values are treated as zero.
        half_life_days: Days after which the factor halves; ``<= 0`` disables decay.

    Returns:
        Factor in ``(0, 1]``.
    """
    if half_life_days <= 0:
        return 1.0
    return math.pow(0.5, max(age_days, 0.0) / half_life_days)


class PersonStore:
    """Async SQLite store for people, facts and profiles."""

    def __init__(self, db_helper: BaseDatabase, config: PersonConfig | None = None) -> None:
        """Initialize the store.

        Args:
            db_helper: Core async database helper.
            config: Person settings controlling decay and conflict thresholds.
        """
        self.db = db_helper
        self.config = config or PersonConfig()

    async def ensure_schema(self) -> None:
        """Create the person tables and indexes when missing."""
        async with self.db.get_db() as session:
            connection = await session.connection()
            for model in (LifePerson, LifePersonFact, LifePersonProfile):
                await connection.run_sync(
                    lambda sync_connection, model=model: model.__table__.create(  # ty: ignore[unresolved-attribute]  # SQLModel adds __table__ at runtime
                        sync_connection,
                        checkfirst=True,
                    )
                )
            await session.commit()

    async def observe_batch(self, observations: Sequence[dict[str, Any]]) -> int:
        """Upsert people and update their relationship scores.

        Args:
            observations: Per-person payloads with ``session_key``, ``person_id``,
                ``display_name``, ``group_id``, ``ts`` and ``count``.

        Returns:
            Number of people written.
        """
        if not observations:
            return 0
        now = int(time.time())
        written = 0
        async with self.db.get_db() as session:
            for observation in observations:
                session_key = str(observation["session_key"])
                person_id = str(observation["person_id"])
                display_name = clean_name(str(observation.get("display_name") or ""))
                count = max(int(observation.get("count") or 1), 1)
                ts = int(observation.get("ts") or now)
                result = await session.execute(
                    select(LifePerson).where(
                        col(LifePerson.session_key) == session_key,
                        col(LifePerson.person_id) == person_id,
                    )
                )
                person = result.scalars().first()
                if person is None:
                    person = LifePerson(
                        session_key=session_key,
                        person_id=person_id,
                        display_name=display_name,
                        normalized_name=normalize_name(display_name),
                        aliases=[display_name] if display_name else [],
                        group_id=observation.get("group_id"),
                        relation_score=min(
                            float(self.config.relation_increment) * count,
                            float(self.config.relation_max),
                        ),
                        interactions=count,
                        last_seen_ts=ts,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(person)
                    written += 1
                    continue
                aliases = [str(alias) for alias in (person.aliases or [])]
                if display_name and display_name not in aliases:
                    aliases.append(display_name)
                    person.display_name = display_name
                    person.normalized_name = normalize_name(display_name)
                idle_days = max((ts - int(person.last_seen_ts or 0)) / 86400.0, 0.0)
                decayed = float(person.relation_score or 0.0) * decay_factor(
                    idle_days, float(self.config.relation_half_life_days)
                )
                person.relation_score = min(
                    decayed + float(self.config.relation_increment) * count,
                    float(self.config.relation_max),
                )
                person.aliases = aliases[-12:]
                person.interactions = int(person.interactions or 0) + count
                person.last_seen_ts = max(int(person.last_seen_ts or 0), ts)
                if observation.get("group_id"):
                    person.group_id = str(observation["group_id"])
                person.updated_at = now
                written += 1
            await session.commit()
        return written

    async def get(self, session_key: str, person_id: str) -> dict[str, Any] | None:
        """Return one person row.

        Args:
            session_key: Session scope.
            person_id: Person identifier.

        Returns:
            Row dictionary, or None when the person is unknown.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifePerson).where(
                    col(LifePerson.session_key) == session_key,
                    col(LifePerson.person_id) == person_id,
                )
            )
            person = result.scalars().first()
            return person.model_dump() if person else None

    async def find(self, session_key: str, query: str) -> dict[str, Any] | None:
        """Resolve a person by id, name or alias.

        Args:
            session_key: Session scope.
            query: Person id, display name, alias or a name fragment.

        Returns:
            Best matching row, or None when nothing matches.
        """
        needle = normalize_name(query)
        if not needle:
            return None
        async with self.db.get_db() as session:
            exact = await session.execute(
                select(LifePerson).where(
                    col(LifePerson.session_key) == session_key,
                    (col(LifePerson.person_id) == query.strip())
                    | (col(LifePerson.normalized_name) == needle),
                )
            )
            person = exact.scalars().first()
            if person is not None:
                return person.model_dump()
            result = await session.execute(
                select(LifePerson)
                .where(col(LifePerson.session_key) == session_key)
                .order_by(desc(col(LifePerson.relation_score)))
                .limit(200)
            )
            for candidate in result.scalars().all():
                names = [candidate.display_name, *(candidate.aliases or [])]
                if any(needle in normalize_name(name) for name in names if name):
                    return candidate.model_dump()
            return None

    async def list_people(
        self,
        session_key: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return the most relevant people of a session.

        Args:
            session_key: Session scope.
            limit: Maximum rows.

        Returns:
            Row dictionaries ordered by relationship score.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifePerson)
                .where(col(LifePerson.session_key) == session_key)
                .order_by(desc(col(LifePerson.relation_score)))  # ty: ignore[invalid-argument-type]  # SQLModel column descriptor
                .limit(max(limit, 1))
            )
            return [row.model_dump() for row in result.scalars().all()]

    async def add_facts(self, rows: Sequence[dict[str, Any]]) -> dict[str, int]:
        """Insert facts and flag conflicts with existing active facts.

        Args:
            rows: Fact payloads with ``session_key``, ``person_id``, ``content``,
                ``category`` and ``confidence``.

        Returns:
            Counters with ``inserted`` and ``conflicts``.
        """
        if not rows:
            return {"inserted": 0, "conflicts": 0}
        now = int(time.time())
        payload: list[dict[str, Any]] = []
        conflicts = 0
        for row in rows:
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            session_key = str(row["session_key"])
            person_id = str(row["person_id"])
            category = str(row.get("category") or "other")[:32]
            existing = await self._active_facts(session_key, person_id, category)
            new_tokens = name_tokens(content)
            conflicting = [
                fact
                for fact in existing
                if fact["fact_hash"] != fact_hash(content)
                and self._similar(new_tokens, name_tokens(fact["content"]))
            ]
            if conflicting:
                group = uuid.uuid4().hex[:16]
                conflicts += 1
                for fact in conflicting:
                    await self._set_status(int(fact["id"]), _CONFLICTED, group)
                payload.append(
                    {
                        "session_key": session_key,
                        "person_id": person_id,
                        "content": content,
                        "fact_hash": fact_hash(content),
                        "category": category,
                        "confidence": float(row.get("confidence") or 0.6),
                        "status": _CONFLICTED,
                        "conflict_group": group,
                        "source_memory_id": row.get("source_memory_id"),
                        "verified_at": now,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                continue
            payload.append(
                {
                    "session_key": session_key,
                    "person_id": person_id,
                    "content": content,
                    "fact_hash": fact_hash(content),
                    "category": category,
                    "confidence": float(row.get("confidence") or 0.6),
                    "status": _ACTIVE,
                    "conflict_group": "",
                    "source_memory_id": row.get("source_memory_id"),
                    "verified_at": now,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        if not payload:
            return {"inserted": 0, "conflicts": conflicts}
        async with self.db.get_db() as session:
            result = await session.execute(
                sqlite_insert(LifePersonFact)
                .values(payload)
                .on_conflict_do_nothing(
                    index_elements=["session_key", "person_id", "fact_hash"],
                )
            )
            await session.commit()
            inserted = max(int(result.rowcount or 0), 0)  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount
        return {"inserted": inserted, "conflicts": conflicts}

    def _similar(self, left: set[str], right: set[str]) -> bool:
        """Return whether two token sets overlap above the conflict threshold.

        Args:
            left: Tokens of the new fact.
            right: Tokens of an existing fact.

        Returns:
            True when the Jaccard similarity reaches ``conflict_similarity``.
        """
        if not left or not right:
            return False
        overlap = len(left & right)
        union = len(left | right)
        return union > 0 and overlap / union >= float(self.config.conflict_similarity)

    async def _active_facts(
        self,
        session_key: str,
        person_id: str,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return active facts of a person for conflict detection.

        Args:
            session_key: Session scope.
            person_id: Person identifier.
            category: Optional category filter.

        Returns:
            Row dictionaries.
        """
        query = select(LifePersonFact).where(
            col(LifePersonFact.session_key) == session_key,
            col(LifePersonFact.person_id) == person_id,
            col(LifePersonFact.status) == _ACTIVE,
        )
        if category:
            query = query.where(col(LifePersonFact.category) == category)
        async with self.db.get_db() as session:
            result = await session.execute(query)
            return [row.model_dump() for row in result.scalars().all()]

    async def _set_status(
        self,
        fact_id: int,
        status: str,
        conflict_group: str = "",
    ) -> None:
        """Update the lifecycle status of one fact.

        Args:
            fact_id: Row id.
            status: New status.
            conflict_group: Conflict group shared by the conflicting facts.
        """
        async with self.db.get_db() as session:
            fact = await session.get(LifePersonFact, fact_id)
            if fact is None:
                return
            fact.status = status
            if conflict_group:
                fact.conflict_group = conflict_group
                fact.verified_at = int(time.time())
            fact.updated_at = int(time.time())
            await session.commit()

    async def list_facts(
        self,
        session_key: str,
        person_id: str,
        statuses: Iterable[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return facts of one person with their effective confidence.

        Args:
            session_key: Session scope.
            person_id: Person identifier.
            statuses: Status filter; defaults to active facts only.
            limit: Maximum rows.

        Returns:
            Fact dictionaries ordered by effective confidence, each carrying a
            computed ``effective_confidence``.
        """
        wanted = tuple(statuses) if statuses else (_ACTIVE,)
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifePersonFact)
                .where(
                    col(LifePersonFact.session_key) == session_key,
                    col(LifePersonFact.person_id) == person_id,
                    col(LifePersonFact.status).in_(wanted),
                )
                .order_by(desc(col(LifePersonFact.confidence)))  # ty: ignore[invalid-argument-type]  # SQLModel column descriptor
                .limit(max(limit, 1))
            )
            facts = [row.model_dump() for row in result.scalars().all()]
        now = int(time.time())
        for fact in facts:
            fact["effective_confidence"] = round(
                float(fact["confidence"])
                * decay_factor(
                    (now - int(fact["verified_at"] or fact["created_at"] or now)) / 86400.0,
                    float(self.config.confidence_half_life_days),
                ),
                4,
            )
        facts.sort(key=lambda fact: fact["effective_confidence"], reverse=True)
        return facts

    async def delete_fact(self, fact_id: int) -> bool:
        """Delete one fact.

        Args:
            fact_id: Row id.

        Returns:
            True when a row was removed.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                delete(LifePersonFact).where(col(LifePersonFact.id) == fact_id)
            )
            await session.commit()
            return max(int(result.rowcount or 0), 0) > 0  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount

    async def delete_person(self, session_key: str, person_id: str) -> dict[str, int]:
        """Delete a person together with facts and profile.

        Args:
            session_key: Session scope.
            person_id: Person identifier.

        Returns:
            Counters with ``people``, ``facts`` and ``profiles``.
        """
        async with self.db.get_db() as session:
            facts = await session.execute(
                delete(LifePersonFact).where(
                    col(LifePersonFact.session_key) == session_key,
                    col(LifePersonFact.person_id) == person_id,
                )
            )
            profiles = await session.execute(
                delete(LifePersonProfile).where(
                    col(LifePersonProfile.session_key) == session_key,
                    col(LifePersonProfile.person_id) == person_id,
                )
            )
            people = await session.execute(
                delete(LifePerson).where(
                    col(LifePerson.session_key) == session_key,
                    col(LifePerson.person_id) == person_id,
                )
            )
            await session.commit()
        return {
            "people": max(int(people.rowcount or 0), 0),  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount
            "facts": max(int(facts.rowcount or 0), 0),  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount
            "profiles": max(int(profiles.rowcount or 0), 0),  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount
        }

    async def get_profile(self, session_key: str, person_id: str) -> dict[str, Any] | None:
        """Return the cached profile row.

        Args:
            session_key: Session scope.
            person_id: Person identifier.

        Returns:
            Profile row, or None when it was never generated.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifePersonProfile).where(
                    col(LifePersonProfile.session_key) == session_key,
                    col(LifePersonProfile.person_id) == person_id,
                )
            )
            profile = result.scalars().first()
            return profile.model_dump() if profile else None

    async def set_profile(
        self,
        session_key: str,
        person_id: str,
        summary: str,
        highlight: list[str],
        fact_ids: list[int],
        generator: str,
    ) -> None:
        """Insert or replace the cached profile of a person.

        Args:
            session_key: Session scope.
            person_id: Person identifier.
            summary: Profile text.
            highlight: Short bullet lines of the profile.
            fact_ids: Fact ids the profile was built from.
            generator: ``llm`` or ``extractive``.
        """
        now = int(time.time())
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifePersonProfile).where(
                    col(LifePersonProfile.session_key) == session_key,
                    col(LifePersonProfile.person_id) == person_id,
                )
            )
            profile = result.scalars().first()
            if profile is None:
                session.add(
                    LifePersonProfile(
                        session_key=session_key,
                        person_id=person_id,
                        summary=summary,
                        highlight=list(highlight),
                        fact_ids=list(fact_ids),
                        generator=generator,
                        updated_at=now,
                    )
                )
            else:
                profile.summary = summary
                profile.highlight = list(highlight)
                profile.fact_ids = list(fact_ids)
                profile.generator = generator
                profile.updated_at = now
            await session.commit()

    async def decay(self) -> int:
        """Mark facts whose effective confidence fell below the floor.

        Returns:
            Number of facts marked ``expired``.
        """
        now = int(time.time())
        expired = 0
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifePersonFact).where(col(LifePersonFact.status) == _ACTIVE)
            )
            for fact in result.scalars().all():
                age_days = (
                    now - int(fact.verified_at or fact.created_at or now)
                ) / 86400.0
                effective = float(fact.confidence) * decay_factor(
                    age_days, float(self.config.confidence_half_life_days)
                )
                if effective < float(self.config.min_confidence):
                    fact.status = _EXPIRED
                    fact.updated_at = now
                    expired += 1
            if expired:
                await session.commit()
        if expired:
            logger.info(f"[life] person fact decay expired {expired} facts")
        return expired

    async def stats(self) -> dict[str, Any]:
        """Return counters of the person layer.

        Returns:
            Mapping with people, facts by status and profile counts.
        """
        async with self.db.get_db() as session:
            people = (
                await session.execute(select(func.count()).select_from(LifePerson))
            ).scalar_one()
            facts = (
                await session.execute(select(func.count()).select_from(LifePersonFact))
            ).scalar_one()
            profiles = (
                await session.execute(select(func.count()).select_from(LifePersonProfile))
            ).scalar_one()
            by_status = await session.execute(
                select(col(LifePersonFact.status), func.count()).group_by(  # ty: ignore[invalid-argument-type]  # SQLModel column descriptor
                    col(LifePersonFact.status)
                )
            )
            conflicts = (
                await session.execute(
                    select(func.count())
                    .select_from(LifePersonFact)
                    .where(col(LifePersonFact.status) == _CONFLICTED)
                )
            ).scalar_one()
        return {
            "people": int(people or 0),
            "facts": int(facts or 0),
            "profiles": int(profiles or 0),
            "conflicts": int(conflicts or 0),
            "facts_by_status": {status: int(count) for status, count in by_status.all()},
        }
