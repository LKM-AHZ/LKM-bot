from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from astrbot.dashboard.responses import error, ok
from astrbot.dashboard.schemas import BotConfigRequest, EnabledPatch
from astrbot.dashboard.services.bot_config_service import BotConfigService

from .auth import AuthContext, ScopeDependency

router = APIRouter(tags=["Bots"])


require_bot_scope = ScopeDependency("bot")


def get_service(request: Request) -> BotConfigService:
    return request.app.state.services.bots


def _required_text(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Missing key: {name}")
    return text


def _config_from_body(body: dict) -> dict:
    config = body.get("config")
    if isinstance(config, dict):
        return config
    return {
        key: value
        for key, value in body.items()
        if key not in {"bot_id", "config", "enabled"}
    }


def _alias_error(message: str):
    return error(message)


@router.get("/bot-types")
async def list_bot_types(
    _auth: AuthContext = Depends(require_bot_scope),
    service: BotConfigService = Depends(get_service),
):
    return ok(service.list_bot_types())


@router.get("/bots")
async def list_bots(
    enabled: bool | None = Query(default=None),
    type_: str | None = Query(default=None, alias="type"),
    _auth: AuthContext = Depends(require_bot_scope),
    service: BotConfigService = Depends(get_service),
):
    return ok(service.list_bots(enabled=enabled, type_=type_))


@router.post("/bots")
async def create_bot(
    payload: BotConfigRequest,
    _auth: AuthContext = Depends(require_bot_scope),
    service: BotConfigService = Depends(get_service),
):
    await service.create_bot(payload.to_dashboard_config())
    return ok(message="新增平台配置成功~")


@router.get("/bots/stats")
async def list_bot_stats(
    _auth: AuthContext = Depends(require_bot_scope),
    service: BotConfigService = Depends(get_service),
):
    return ok(service.get_bot_stats())


@router.get("/bots/by-id")
async def get_bot_by_id(
    bot_id: str = Query(...),
    _auth: AuthContext = Depends(require_bot_scope),
    service: BotConfigService = Depends(get_service),
):
    return ok(service.get_bot(bot_id))


@router.put("/bots/by-id")
async def update_bot_by_id(
    payload: BotConfigRequest,
    _auth: AuthContext = Depends(require_bot_scope),
    service: BotConfigService = Depends(get_service),
):
    bot_id = _required_text(payload.bot_id, "bot_id")
    await service.update_bot(
        bot_id,
        payload.to_dashboard_config(fallback_id=bot_id),
    )
    return ok(message="更新平台配置成功~")


@router.delete("/bots/by-id")
async def delete_bot_by_id(
    bot_id: str = Query(...),
    _auth: AuthContext = Depends(require_bot_scope),
    service: BotConfigService = Depends(get_service),
):
    await service.delete_bot(bot_id)
    return ok(message="删除平台配置成功~")


@router.patch("/bots/enabled")
async def set_bot_enabled_by_id(
    payload: BotConfigRequest,
    _auth: AuthContext = Depends(require_bot_scope),
    service: BotConfigService = Depends(get_service),
):
    bot_id = _required_text(payload.bot_id, "bot_id")
    await service.set_bot_enabled(bot_id, bool(payload.enabled))
    return ok(message="更新平台配置成功~")


@router.post("/bots/test")
async def test_bot_by_id(
    payload: BotConfigRequest,
    _auth: AuthContext = Depends(require_bot_scope),
):
    bot_id = _required_text(payload.bot_id, "bot_id")
    return ok({"id": bot_id, "status": "unsupported"})


@router.patch("/bots/{bot_id:path}/enabled")
async def set_bot_enabled(
    bot_id: str,
    payload: EnabledPatch,
    _auth: AuthContext = Depends(require_bot_scope),
    service: BotConfigService = Depends(get_service),
):
    await service.set_bot_enabled(bot_id, payload.enabled)
    return ok(message="更新平台配置成功~")


@router.post("/bots/{bot_id:path}/test")
async def test_bot(
    bot_id: str,
    _auth: AuthContext = Depends(require_bot_scope),
):
    return ok({"id": bot_id, "status": "unsupported"})


@router.get("/bots/{bot_id:path}")
async def get_bot(
    bot_id: str,
    _auth: AuthContext = Depends(require_bot_scope),
    service: BotConfigService = Depends(get_service),
):
    return ok(service.get_bot(bot_id))


@router.put("/bots/{bot_id:path}")
async def update_bot(
    bot_id: str,
    payload: BotConfigRequest,
    _auth: AuthContext = Depends(require_bot_scope),
    service: BotConfigService = Depends(get_service),
):
    await service.update_bot(bot_id, payload.to_dashboard_config(fallback_id=bot_id))
    return ok(message="更新平台配置成功~")


@router.delete("/bots/{bot_id:path}")
async def delete_bot(
    bot_id: str,
    _auth: AuthContext = Depends(require_bot_scope),
    service: BotConfigService = Depends(get_service),
):
    await service.delete_bot(bot_id)
    return ok(message="删除平台配置成功~")
