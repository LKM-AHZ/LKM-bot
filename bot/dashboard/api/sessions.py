from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from bot.core import logger
from bot.core.utils.async_utils import run_maybe_async
from bot.dashboard.responses import error, ok
from bot.dashboard.schemas import (
    SessionGroupRequest,
    SessionRuleRequest,
    UmoListRequest,
)
from bot.dashboard.services.session_management_service import (
    SessionManagementService,
    SessionManagementServiceError,
)

from ._helpers import json_or_empty as _json_or_empty
from .auth import AuthContext, ScopeDependency

router = APIRouter(tags=["Sessions"])


def get_service(request: Request) -> SessionManagementService:
    return request.app.state.services.sessions


require_data_scope = ScopeDependency("data")


def _service_error(exc: SessionManagementServiceError) -> dict:
    return error(str(exc))


def _unexpected_error(prefix: str, exc: Exception) -> dict:
    logger.error(f"{prefix}: {exc!s}")
    return error(f"{prefix}: {exc!s}")


async def _run(operation, *, label: str) -> dict:
    try:
        result = await run_maybe_async(operation)
        return ok(result)
    except SessionManagementServiceError as exc:
        return _service_error(exc)
    except Exception as exc:
        return _unexpected_error(label, exc)


async def _run_dashboard_json(
    request: Request,
    operation,
    *,
    label: str,
) -> dict:
    body = await _json_or_empty(request)
    return await _run(lambda: operation(body), label=label)


@router.get("/sessions/active-umos")
async def list_active_umos(
    platform_id: str = Query(""),
    _auth: AuthContext = Depends(require_data_scope),
    service: SessionManagementService = Depends(get_service),
):
    try:
        return ok(await service.list_active_umos(platform_id=platform_id))
    except SessionManagementServiceError as exc:
        return _service_error(exc)
    except Exception as exc:
        return _unexpected_error("获取 UMO 列表失败", exc)


@router.get("/sessions/senders")
async def list_session_senders(
    search: str = Query("", max_length=512),
    limit: int = Query(50, ge=1, le=100),
    page: int = Query(1, ge=1),
    sender_ids: list[str] | None = Query(None, max_length=100),
    _auth: AuthContext = Depends(require_data_scope),
    service: SessionManagementService = Depends(get_service),
):
    """Search known senders for ID selection.

    Args:
        search: Substring matched against sender and session metadata.
        limit: Maximum number of suggestions per page.
        page: One-based page of deduplicated suggestions.
        sender_ids: Resolve the latest record for each exact sender ID.
        _auth: Authenticated caller with the data scope.
        service: Session management service.

    Returns:
        Response envelope containing sender suggestions.
    """
    return await _run(
        lambda: service.list_senders(search, limit, sender_ids, page),
        label="Failed to list session senders",
    )


@router.get("/sessions/rules")
async def list_session_rules(
    page: int = Query(1),
    page_size: int = Query(10),
    search: str = Query(""),
    _auth: AuthContext = Depends(require_data_scope),
    service: SessionManagementService = Depends(get_service),
):
    try:
        return ok(
            await service.list_session_rules(
                page=page,
                page_size=page_size,
                search=search.strip(),
            )
        )
    except SessionManagementServiceError as exc:
        return _service_error(exc)
    except Exception as exc:
        return _unexpected_error("获取规则列表失败", exc)


@router.get("/sessions/overrides")
async def get_session_overrides(
    umo: str = Query(..., min_length=1, max_length=512),
    profile_id: str | None = Query(None, max_length=255),
    _auth: AuthContext = Depends(require_data_scope),
    service: SessionManagementService = Depends(get_service),
):
    """Read a session's explicit values and inherited profile settings.

    Args:
        umo: Exact unified message origin.
        profile_id: Optional profile preview; empty string follows routing.
        _auth: Authenticated caller with data access.
        service: Session management service.

    Returns:
        Session editor data in the standard response envelope.
    """
    return await _run(
        lambda: service.get_session_overrides(umo, profile_id),
        label="Failed to read session overrides",
    )


@router.post("/sessions/rules")
async def update_session_rule(
    payload: SessionRuleRequest,
    _auth: AuthContext = Depends(require_data_scope),
    service: SessionManagementService = Depends(get_service),
):
    try:
        return ok(
            await service.update_session_rule(payload.model_dump(exclude_unset=True))
        )
    except SessionManagementServiceError as exc:
        return _service_error(exc)
    except Exception as exc:
        return _unexpected_error("更新会话规则失败", exc)


@router.post("/sessions/rules/delete")
async def delete_session_rule(
    payload: UmoListRequest,
    _auth: AuthContext = Depends(require_data_scope),
    service: SessionManagementService = Depends(get_service),
):
    try:
        return ok(
            await service.delete_session_rules(payload.model_dump(exclude_none=True))
        )
    except SessionManagementServiceError as exc:
        return _service_error(exc)
    except Exception as exc:
        return _unexpected_error("删除会话规则失败", exc)


@router.get("/session-groups")
async def list_session_groups(
    _auth: AuthContext = Depends(require_data_scope),
    service: SessionManagementService = Depends(get_service),
):
    try:
        return ok(await service.list_groups())
    except SessionManagementServiceError as exc:
        return _service_error(exc)
    except Exception as exc:
        return _unexpected_error("获取分组列表失败", exc)


@router.post("/session-groups")
async def create_session_group(
    payload: SessionGroupRequest,
    _auth: AuthContext = Depends(require_data_scope),
    service: SessionManagementService = Depends(get_service),
):
    try:
        return ok(await service.create_group(payload.model_dump(exclude_none=True)))
    except SessionManagementServiceError as exc:
        return _service_error(exc)
    except Exception as exc:
        return _unexpected_error("创建分组失败", exc)


@router.put("/session-groups/{group_id}")
async def update_session_group(
    group_id: str,
    payload: SessionGroupRequest,
    _auth: AuthContext = Depends(require_data_scope),
    service: SessionManagementService = Depends(get_service),
):
    try:
        body = payload.model_dump(exclude_none=True)
        return ok(await service.update_group({"group_id": group_id, **body}))
    except SessionManagementServiceError as exc:
        return _service_error(exc)
    except Exception as exc:
        return _unexpected_error("更新分组失败", exc)


@router.delete("/session-groups/{group_id}")
async def delete_session_group(
    group_id: str,
    _auth: AuthContext = Depends(require_data_scope),
    service: SessionManagementService = Depends(get_service),
):
    try:
        return ok(await service.delete_group({"group_id": group_id}))
    except SessionManagementServiceError as exc:
        return _service_error(exc)
    except Exception as exc:
        return _unexpected_error("删除分组失败", exc)
