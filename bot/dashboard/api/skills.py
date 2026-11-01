from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from bot.core import logger
from bot.core.utils.async_utils import run_maybe_async
from bot.dashboard.responses import error, ok
from bot.dashboard.schemas import (
    SkillByNameUpdateRequest,
    SkillFileUpdateRequest,
    SkillNeoRequest,
    SkillUpdateRequest,
)
from bot.dashboard.services.skills_service import (
    SkillArchive,
    SkillsOperationResult,
    SkillsService,
    SkillsServiceError,
)

from .auth import AuthContext, ScopeDependency
from .multipart import multipart_parts, single_upload

router = APIRouter(tags=["Skills"])


def get_service(request: Request) -> SkillsService:
    return request.app.state.services.skills


require_skill_scope = ScopeDependency("skill")


def _required_text(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Missing key: {name}")
    return text


def _model_dict(payload) -> dict[str, Any]:
    if payload is None:
        return {}
    if hasattr(payload, "model_dump"):
        return payload.model_dump(exclude_none=True)
    return payload if isinstance(payload, dict) else {}


def _serialize_result(result: SkillsOperationResult):
    if result.ok:
        return ok(result.data, result.message)
    return error(result.message or "", result.data)


async def _run(operation, *, trace: bool = True):
    try:
        result = await run_maybe_async(operation)
        if isinstance(result, SkillsOperationResult):
            return _serialize_result(result)
        return ok(result)
    except SkillsServiceError as exc:
        return error(str(exc))
    except Exception as exc:
        logger.error(str(exc), exc_info=trace)
        return error(str(exc))


def _archive_response(archive: SkillArchive):
    return FileResponse(
        archive.path,
        filename=archive.filename,
        media_type="application/zip",
    )


async def _download_skill(service: SkillsService, name: str):
    try:
        return _archive_response(service.prepare_skill_archive(name))
    except SkillsServiceError as exc:
        message = str(exc)
        raise HTTPException(status_code=exc.status_code, detail=message) from exc
    except Exception as exc:
        logger.error(str(exc), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to prepare skill archive",
        ) from exc


@router.get("/skills")
async def list_skills(
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(service.get_skills)


@router.post("/skills")
async def upload_skill(
    request: Request,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    async def _operation():
        return await service.upload_skill(await single_upload(request))

    return await _run(_operation)


@router.post("/skills/batch")
async def upload_skills_batch(
    request: Request,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    async def _operation():
        _, files = await multipart_parts(request)
        return await service.batch_upload_skills(files.getlist("files"))

    return await _run(_operation)


@router.patch("/skills/by-name")
async def update_skill_by_name(
    payload: SkillByNameUpdateRequest,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    skill_name = _required_text(payload.skill_name, "skill_name")
    return await _run(
        lambda: service.update_skill(
            {
                "name": skill_name,
                "active": payload.active_value(),
            }
        )
    )


@router.delete("/skills/by-name")
async def delete_skill_by_name(
    skill_name: str,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(lambda: service.delete_skill({"name": skill_name}))


@router.get("/skills/archive")
async def download_skill_by_name(
    skill_name: str,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _download_skill(service, skill_name)


@router.get("/skills/files")
async def list_skill_files_by_name(
    request: Request,
    skill_name: str,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(
        lambda: service.list_skill_files(
            skill_name,
            request.query_params.get("path", ""),
        )
    )


@router.get("/skills/file")
async def get_skill_file_by_name(
    skill_name: str,
    path: str,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(lambda: service.get_skill_file(skill_name, path))


@router.put("/skills/file")
async def update_skill_file_by_name(
    payload: SkillFileUpdateRequest,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    skill_name = _required_text(payload.skill_name, "skill_name")
    path = _required_text(payload.path, "path")
    return await _run(
        lambda: service.update_skill_file(
            {
                "name": skill_name,
                "path": path,
                "content": payload.content,
            }
        )
    )


@router.get("/skills/{skill_name:path}/archive")
async def download_skill(
    skill_name: str,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _download_skill(service, skill_name)


@router.get("/skills/{skill_name:path}/files")
async def list_skill_files(
    skill_name: str,
    request: Request,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(
        lambda: service.list_skill_files(
            skill_name,
            request.query_params.get("path", ""),
        )
    )


@router.get("/skills/{skill_name:path}/files/{file_path:path}")
async def get_skill_file(
    skill_name: str,
    file_path: str,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(lambda: service.get_skill_file(skill_name, file_path))


@router.put("/skills/{skill_name:path}/files/{file_path:path}")
async def update_skill_file(
    skill_name: str,
    file_path: str,
    request: Request,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    content = (await request.body()).decode("utf-8")
    return await _run(
        lambda: service.update_skill_file(
            {"name": skill_name, "path": file_path, "content": content}
        )
    )


@router.patch("/skills/{skill_name:path}")
async def update_skill(
    skill_name: str,
    payload: SkillUpdateRequest,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(
        lambda: service.update_skill(
            {
                "name": skill_name,
                "active": payload.active_value(),
            }
        )
    )


@router.delete("/skills/{skill_name:path}")
async def delete_skill(
    skill_name: str,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(lambda: service.delete_skill({"name": skill_name}))


@router.get("/skills/neo/candidates")
async def list_neo_skill_candidates(
    request: Request,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(
        service.get_neo_candidates(
            dict(request.query_params),
        )
    )


@router.get("/skills/neo/releases")
async def list_neo_skill_releases(
    request: Request,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(
        service.get_neo_releases(
            dict(request.query_params),
        )
    )


@router.get("/skills/neo/payload")
async def get_neo_skill_payload(
    request: Request,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(service.get_neo_payload(dict(request.query_params)))


@router.post("/skills/neo/evaluate")
async def evaluate_neo_skill_candidate(
    payload: SkillNeoRequest,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(lambda: service.evaluate_neo_candidate(_model_dict(payload)))


@router.post("/skills/neo/promote")
async def promote_neo_skill_candidate(
    payload: SkillNeoRequest,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(lambda: service.promote_neo_candidate(_model_dict(payload)))


@router.post("/skills/neo/rollback")
async def rollback_neo_skill_release(
    payload: SkillNeoRequest,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(lambda: service.rollback_neo_release(_model_dict(payload)))


@router.post("/skills/neo/sync")
async def sync_neo_skill_release(
    payload: SkillNeoRequest,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(lambda: service.sync_neo_release(_model_dict(payload)))


@router.post("/skills/neo/candidates/delete")
async def delete_neo_skill_candidate(
    payload: SkillNeoRequest,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(lambda: service.delete_neo_candidate(_model_dict(payload)))


@router.post("/skills/neo/releases/delete")
async def delete_neo_skill_release(
    payload: SkillNeoRequest,
    _auth: AuthContext = Depends(require_skill_scope),
    service: SkillsService = Depends(get_service),
):
    return await _run(lambda: service.delete_neo_release(_model_dict(payload)))
