"""Life layer dashboard API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request

from bot.dashboard.api.auth import AuthContext, require_scope
from bot.dashboard.responses import ApiError, ok

router = APIRouter(tags=["Life"])


async def require_life_scope(request: Request) -> AuthContext:
    """Require the system scope for Life endpoints.

    Args:
        request: Incoming request.

    Returns:
        Authentication context.
    """
    return await require_scope(request, "system")


@router.get("/life/status")
async def life_status(
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Return Life runtime status.

    Returns:
        Wrapped status payload.
    """
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None:
        return ok({"enabled": False, "configured": False})
    return ok(await service.status())


@router.get("/life/messages")
async def life_messages(
    limit: int = Query(default=20, ge=1, le=200),
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Return the latest archived messages.

    Args:
        limit: Maximum number of rows.

    Returns:
        Wrapped message list.
    """
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None or service.store is None:
        return ok([])
    return ok(await service.store.recent(limit))


@router.get("/life/memory")
async def life_memory(
    limit: int = Query(default=20, ge=1, le=200),
    kind: str | None = Query(default=None),
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Return memory status and the newest memory rows.

    Args:
        limit: Maximum number of rows.
        kind: Optional kind filter (summary, fact, episode, note).

    Returns:
        Wrapped memory payload.
    """
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None or service.memory is None:
        return ok({"enabled": False, "items": []})
    status = await service.memory.status()
    items = await service.memory.store.list_recent(limit=limit, kind=kind)
    return ok({**status, "items": items})


@router.get("/life/memory/search")
async def life_memory_search(
    q: str = Query(min_length=1),
    umo: str = Query(min_length=1),
    mode: str = Query(default="hybrid"),
    limit: int = Query(default=10, ge=1, le=50),
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Search one session's memories, used by the panel preview.

    Args:
        q: Query text.
        umo: Session scope.
        mode: One of search, time, hybrid, episode or aggregate.
        limit: Maximum number of hits.

    Returns:
        Wrapped hit list.
    """
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None or service.memory is None:
        return ok([])
    if mode not in ("search", "time", "hybrid", "episode", "aggregate"):
        raise ApiError(f"unsupported memory mode: {mode}")
    return ok(await service.memory.search(q, umo, mode=mode, limit=limit))


@router.delete("/life/memory")
async def delete_life_memory(
    umo: str | None = Query(default=None),
    ids: str | None = Query(default=None),
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Delete memories for user-level erasure.

    Args:
        umo: Delete every memory of one session.
        ids: Comma-separated memory ids.

    Returns:
        Wrapped deletion result.

    Raises:
        ApiError: When neither filter is provided.
    """
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None or service.memory is None:
        return ok({"deleted": 0})
    memory_ids = [int(item) for item in ids.split(",") if item.strip()] if ids else None
    if not memory_ids and not umo:
        raise ApiError("either umo or ids is required")
    deleted = await service.memory.delete(memory_ids=memory_ids, session_key=umo)
    return ok({"deleted": deleted})


@router.get("/life/memory/export")
async def export_life_memory(
    umo: str | None = Query(default=None),
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Export memories as JSON for backup or migration.

    Args:
        umo: Restrict to one session.

    Returns:
        Wrapped export payload.
    """
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None or service.memory is None:
        return ok([])
    return ok(await service.memory.export(umo))


@router.get("/life/people")
async def life_people(
    umo: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=200),
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Return the people known in one session.

    Args:
        umo: Session scope.
        limit: Maximum number of rows.

    Returns:
        Wrapped list ordered by relationship score.
    """
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None or service.person is None:
        return ok([])
    return ok(await service.person.store.list_people(umo, limit=limit))


@router.get("/life/people/profile")
async def life_person_profile(
    umo: str = Query(min_length=1),
    person: str = Query(min_length=1),
    refresh: bool = Query(default=False),
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Return the profile of one person.

    Args:
        umo: Session scope.
        person: Person id, display name or alias.
        refresh: Rebuild the cached profile.

    Returns:
        Wrapped profile payload.

    Raises:
        ApiError: When the person is unknown.
    """
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None or service.person is None:
        raise ApiError("person layer is not enabled")
    profile = await service.person.profile(umo, person, refresh=refresh)
    if profile is None:
        raise ApiError(f"unknown person: {person}")
    return ok(profile)


@router.delete("/life/people")
async def delete_life_person(
    umo: str = Query(min_length=1),
    person: str = Query(min_length=1),
    fact_id: int | None = Query(default=None),
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Forget one person or one fact.

    Args:
        umo: Session scope.
        person: Person id, display name or alias.
        fact_id: Delete only this fact when given.

    Returns:
        Wrapped deletion counters.
    """
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None or service.person is None:
        raise ApiError("person layer is not enabled")
    deleted = await service.person.forget(umo, person, fact_id=fact_id)
    return ok(deleted)


@router.patch("/life/people/fact")
async def resolve_life_person_fact(
    payload: dict[str, Any] = Body(...),
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Resolve a conflicted fact during review.

    Args:
        payload: ``{"fact_id": int, "keep": bool}``.

    Returns:
        Wrapped change counter.

    Raises:
        ApiError: When the payload is incomplete.
    """
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None or service.person is None:
        raise ApiError("person layer is not enabled")
    fact_id = payload.get("fact_id")
    if not isinstance(fact_id, int):
        raise ApiError("fact_id must be an integer")
    changed = await service.person.resolve_fact(fact_id, bool(payload.get("keep")))
    return ok({"changed": changed})


@router.get("/life/expressions")
async def life_expressions(
    umo: str = Query(min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Return learned expressions of one session.

    Args:
        umo: Session scope.
        limit: Maximum number of rows.

    Returns:
        Wrapped expression list ordered by weight.
    """
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None or service.expression is None:
        return ok([])
    return ok(await service.expression.list_expressions(umo, limit=limit))


@router.delete("/life/expressions")
async def delete_life_expressions(
    umo: str | None = Query(default=None),
    ids: str | None = Query(default=None),
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Delete learned expressions.

    Args:
        umo: Delete every expression of one session.
        ids: Comma-separated expression ids.

    Returns:
        Wrapped deletion counter.

    Raises:
        ApiError: When neither filter is provided.
    """
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None or service.expression is None:
        return ok({"deleted": 0})
    expression_ids = (
        [int(item) for item in ids.split(",") if item.strip()] if ids else None
    )
    if not expression_ids and not umo:
        raise ApiError("either umo or ids is required")
    deleted = await service.expression.forget(
        expression_ids=expression_ids,
        session_key=umo,
    )
    return ok({"deleted": deleted})


@router.get("/life/jargon")
async def life_jargon(
    umo: str = Query(min_length=1),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Return learned slang terms of one session.

    Args:
        umo: Session scope.
        status: Optional status filter (active, pending, rejected).
        limit: Maximum number of rows.

    Returns:
        Wrapped term list ordered by confidence.
    """
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None or service.jargon is None:
        return ok([])
    statuses = [status] if status else None
    return ok(await service.jargon.list_jargon(umo, statuses=statuses, limit=limit))


@router.patch("/life/jargon")
async def review_life_jargon(
    payload: dict[str, Any] = Body(...),
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Confirm, reject or correct one slang term.

    Args:
        payload: ``{"id": int, "status": "active"|"pending"|"rejected",
            "meaning": optional corrected explanation}``.

    Returns:
        Wrapped review result.

    Raises:
        ApiError: When the payload is incomplete or the term is unknown.
    """
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None or service.jargon is None:
        raise ApiError("jargon layer is not enabled")
    jargon_id = payload.get("id")
    status = payload.get("status")
    if not isinstance(jargon_id, int) or status not in (
        "active",
        "pending",
        "rejected",
    ):
        raise ApiError("id must be an integer and status must be valid")
    meaning = payload.get("meaning")
    updated = await service.jargon.review(
        jargon_id,
        status,
        meaning if isinstance(meaning, str) else None,
    )
    if not updated:
        raise ApiError(f"unknown jargon id: {jargon_id}")
    return ok({"updated": updated})


@router.delete("/life/jargon")
async def delete_life_jargon(
    umo: str | None = Query(default=None),
    ids: str | None = Query(default=None),
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Delete learned slang terms.

    Args:
        umo: Delete every term of one session.
        ids: Comma-separated term ids.

    Returns:
        Wrapped deletion counter.

    Raises:
        ApiError: When neither filter is provided.
    """
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None or service.jargon is None:
        return ok({"deleted": 0})
    jargon_ids = [int(item) for item in ids.split(",") if item.strip()] if ids else None
    if not jargon_ids and not umo:
        raise ApiError("either umo or ids is required")
    deleted = await service.jargon.forget(jargon_ids=jargon_ids, session_key=umo)
    return ok({"deleted": deleted})


@router.get("/life/config")
async def get_life_config(
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Return the current Life configuration.

    Returns:
        Wrapped configuration payload.
    """
    from bot.life.config import load_life_config

    return ok(load_life_config().model_dump(mode="json"))


@router.put("/life/config")
async def update_life_config(
    payload: dict[str, Any] = Body(...),
    _auth: AuthContext = Depends(require_life_scope),
) -> dict:
    """Validate and persist the Life configuration.

    Args:
        payload: Full configuration document.

    Returns:
        Wrapped save result.

    Raises:
        ApiError: When the payload fails validation.
    """
    from pydantic import ValidationError

    from bot.life.config import LifeConfig, save_life_config

    try:
        config = LifeConfig.model_validate(payload)
    except ValidationError as exc:
        raise ApiError(f"invalid life config: {exc}") from exc
    path = save_life_config(config)
    return ok({"path": str(path), "restart_required": True})
