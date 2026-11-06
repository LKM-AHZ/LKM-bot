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
