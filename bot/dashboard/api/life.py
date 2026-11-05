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
