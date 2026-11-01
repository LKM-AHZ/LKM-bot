from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from bot.core import logger
from bot.core.desktop_runtime import DESKTOP_MANAGED_RESTART_MESSAGE
from bot.core.utils.async_utils import run_maybe_async
from bot.dashboard.schemas import PipInstallRequest, UpdateRequest
from bot.dashboard.services.update_service import (
    UpdateService,
    UpdateServiceError,
    UpdateServiceResult,
)

from .auth import AuthContext, require_scope

router = APIRouter(tags=["Updates"])


def get_service(request: Request) -> UpdateService:
    return request.app.state.services.updates


async def require_system_scope(request: Request) -> AuthContext:
    return await require_scope(request, "system")


def _model_dict(payload) -> dict:
    return payload.model_dump(exclude_none=True)


def _result_payload(result: UpdateServiceResult) -> dict:
    if result.status == "success":
        return {
            "status": "success",
            "message": result.message,
            "data": result.data,
        }
    return {
        "status": "ok",
        "message": result.message,
        "data": {} if result.data is None else result.data,
    }


def _service_response(result: UpdateServiceResult) -> JSONResponse:
    return JSONResponse(
        _result_payload(result),
        status_code=200,
        headers=result.headers or None,
    )


def _service_error(exc: UpdateServiceError) -> JSONResponse:
    logger.error(f"Dashboard update operation failed: {exc}", exc_info=True)
    if exc.code == "desktop_managed":
        return JSONResponse(
            {
                "status": "error",
                "message": DESKTOP_MANAGED_RESTART_MESSAGE,
                "data": None,
            },
            status_code=200,
        )
    return JSONResponse(
        {"status": "error", "message": "An internal error has occurred.", "data": None},
        status_code=200,
    )


async def _run(operation) -> JSONResponse:
    try:
        result = await run_maybe_async(operation)
        return _service_response(result)
    except UpdateServiceError as exc:
        return _service_error(exc)


@router.get("/updates/check")
async def check_updates(
    update_type: str | None = Query(default=None, alias="type"),
    _auth: AuthContext = Depends(require_system_scope),
    service: UpdateService = Depends(get_service),
):
    return await _run(lambda: service.check_update(update_type))


@router.get("/updates/releases")
async def update_releases(
    _auth: AuthContext = Depends(require_system_scope),
    service: UpdateService = Depends(get_service),
):
    return await _run(service.get_releases)


@router.get("/updates/progress/{task_id}")
async def update_progress(
    task_id: str,
    _auth: AuthContext = Depends(require_system_scope),
    service: UpdateService = Depends(get_service),
):
    return await _run(lambda: service.get_update_progress(task_id))


@router.post("/updates/core")
async def update_core(
    payload: UpdateRequest,
    _auth: AuthContext = Depends(require_system_scope),
    service: UpdateService = Depends(get_service),
):
    return await _run(lambda: service.update_project(_model_dict(payload)))


@router.post("/updates/dashboard")
async def update_dashboard(
    _auth: AuthContext = Depends(require_system_scope),
    service: UpdateService = Depends(get_service),
):
    return await _run(service.update_dashboard)


@router.post("/pip/install")
async def install_pip_package(
    payload: PipInstallRequest,
    _auth: AuthContext = Depends(require_system_scope),
    service: UpdateService = Depends(get_service),
):
    return await _run(lambda: service.install_pip_package(_model_dict(payload)))
