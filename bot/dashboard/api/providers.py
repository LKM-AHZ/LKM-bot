from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from bot.dashboard.responses import error, ok
from bot.dashboard.schemas import (
    EnabledPatch,
    ProviderConfigRequest,
    ProviderSourceRequest,
)
from bot.dashboard.services.provider_config_service import ProviderConfigService

from .auth import AuthContext, ScopeDependency

router = APIRouter(tags=["Providers"])


require_provider_scope = ScopeDependency("provider")


def get_service(request: Request) -> ProviderConfigService:
    return request.app.state.services.providers


def _required_text(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Missing key: {name}")
    return text


def _model_dict(payload) -> dict:
    if payload is None:
        return {}
    if hasattr(payload, "model_dump"):
        return payload.model_dump(exclude_none=True)
    return payload if isinstance(payload, dict) else {}


def _config_from_body(body: dict) -> dict:
    config = body.get("config")
    if isinstance(config, dict):
        return config
    return {
        key: value
        for key, value in body.items()
        if key
        not in {
            "provider_id",
            "source_id",
            "config",
            "enabled",
            "provider_config",
        }
    }


def _provider_config_for_dimension(
    service: ProviderConfigService,
    provider_id: str,
    body: dict,
) -> dict:
    provider = service.get_provider(provider_id, merged=True)
    base_config = provider.get("provider") if isinstance(provider, dict) else {}
    if not isinstance(base_config, dict):
        base_config = {}
    provider_config = body.get("provider_config")
    if isinstance(provider_config, dict):
        return {**base_config, **provider_config}
    return base_config


def _alias_error(message: str):
    return error(message)


@router.get("/providers/schema")
async def get_provider_schema(
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    return ok(service.get_provider_schema())


@router.get("/provider-sources")
async def list_provider_sources(
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    return ok(service.list_provider_sources())


@router.post("/provider-sources")
async def create_provider_source(
    payload: ProviderSourceRequest,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    config = payload.to_dashboard_config()
    source_id = config.get("id")
    if not source_id:
        raise ValueError("Provider source config must have an 'id' field")
    await service.upsert_provider_source(source_id, config)
    return ok(message="更新 provider source 成功")


@router.get("/provider-sources/by-id")
async def get_provider_source_by_id(
    source_id: str = Query(...),
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    return ok(service.get_provider_source(source_id))


@router.put("/provider-sources/by-id")
async def upsert_provider_source_by_id(
    payload: ProviderSourceRequest,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    source_id = _required_text(payload.source_id, "source_id")
    await service.upsert_provider_source(
        source_id,
        payload.to_dashboard_config(fallback_id=source_id),
    )
    return ok(message="更新 provider source 成功")


@router.delete("/provider-sources/by-id")
async def delete_provider_source_by_id(
    source_id: str = Query(...),
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    await service.delete_provider_source(source_id)
    return ok(message="删除 provider source 成功")


@router.get("/provider-sources/models")
async def list_provider_source_models_by_id(
    source_id: str = Query(...),
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    return ok(await service.list_provider_source_models(source_id))


@router.get("/provider-sources/providers")
async def list_providers_by_source_id(
    source_id: str = Query(...),
    capability: str | None = Query(default=None),
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    return ok(service.list_providers(capability=capability, source_id=source_id))


@router.post("/provider-sources/providers")
async def create_provider_in_source_by_id(
    payload: ProviderConfigRequest,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    source_id = _required_text(payload.source_id, "source_id")
    await service.create_provider(
        payload.to_dashboard_config(source_id=source_id),
        source_id,
    )
    return ok(message="新增服务提供商配置成功")


@router.get("/provider-sources/{source_id:path}/models")
async def list_provider_source_models(
    source_id: str,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    return ok(await service.list_provider_source_models(source_id))


@router.get("/provider-sources/{source_id:path}/providers")
async def list_providers_by_source(
    source_id: str,
    capability: str | None = Query(default=None),
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    return ok(service.list_providers(capability=capability, source_id=source_id))


@router.post("/provider-sources/{source_id:path}/providers")
async def create_provider_in_source(
    source_id: str,
    payload: ProviderConfigRequest,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    await service.create_provider(
        payload.to_dashboard_config(source_id=source_id), source_id
    )
    return ok(message="新增服务提供商配置成功")


@router.get("/provider-sources/{source_id:path}")
async def get_provider_source(
    source_id: str,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    return ok(service.get_provider_source(source_id))


@router.put("/provider-sources/{source_id:path}")
async def upsert_provider_source(
    source_id: str,
    payload: ProviderSourceRequest,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    await service.upsert_provider_source(
        source_id,
        payload.to_dashboard_config(),
    )
    return ok(message="更新 provider source 成功")


@router.delete("/provider-sources/{source_id:path}")
async def delete_provider_source(
    source_id: str,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    await service.delete_provider_source(source_id)
    return ok(message="删除 provider source 成功")


@router.get("/providers")
async def list_providers(
    capability: str | None = Query(default=None),
    source_id: str | None = Query(default=None),
    enabled: bool | None = Query(default=None),
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    return ok(
        service.list_providers(
            capability=capability,
            source_id=source_id,
            enabled=enabled,
        )
    )


@router.post("/providers")
async def create_provider(
    payload: ProviderConfigRequest,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    await service.create_provider(payload.to_dashboard_config())
    return ok(message="新增服务提供商配置成功")


@router.get("/providers/by-id")
async def get_provider_by_id(
    provider_id: str = Query(...),
    merged: bool = Query(default=False),
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    return ok(service.get_provider(provider_id, merged=merged))


@router.put("/providers/by-id")
async def update_provider_by_id(
    payload: ProviderConfigRequest,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    provider_id = _required_text(payload.provider_id, "provider_id")
    await service.update_provider(
        provider_id,
        payload.to_dashboard_config(fallback_id=provider_id),
    )
    return ok(message="更新成功，已经实时生效~")


@router.delete("/providers/by-id")
async def delete_provider_by_id(
    provider_id: str = Query(...),
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    await service.delete_provider(provider_id)
    return ok(message="删除成功，已经实时生效。")


@router.patch("/providers/enabled")
async def set_provider_enabled_by_id(
    payload: ProviderConfigRequest,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    provider_id = _required_text(payload.provider_id, "provider_id")
    await service.set_provider_enabled(provider_id, bool(payload.enabled))
    return ok(message="更新成功，已经实时生效~")


@router.post("/providers/test")
async def test_provider_by_id(
    payload: ProviderConfigRequest,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    provider_id = _required_text(payload.provider_id, "provider_id")
    return ok(await service.test_provider(provider_id))


@router.post("/providers/embedding-dimension")
async def get_embedding_dimension_by_id(
    payload: ProviderConfigRequest,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    body = _model_dict(payload)
    provider_id = _required_text(payload.provider_id, "provider_id")
    return ok(
        await service.get_embedding_dimension(
            _provider_config_for_dimension(service, provider_id, body)
        )
    )


@router.patch("/providers/{provider_id:path}/enabled")
async def set_provider_enabled(
    provider_id: str,
    payload: EnabledPatch,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    await service.set_provider_enabled(provider_id, payload.enabled)
    return ok(message="更新成功，已经实时生效~")


@router.post("/providers/{provider_id:path}/test")
async def test_provider(
    provider_id: str,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    return ok(await service.test_provider(provider_id))


@router.post("/providers/{provider_id:path}/embedding-dimension")
async def get_embedding_dimension(
    provider_id: str,
    payload: ProviderConfigRequest | None = None,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    body = _model_dict(payload)
    return ok(
        await service.get_embedding_dimension(
            _provider_config_for_dimension(service, provider_id, body)
        )
    )


@router.get("/providers/{provider_id:path}")
async def get_provider(
    provider_id: str,
    merged: bool = Query(default=False),
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    return ok(service.get_provider(provider_id, merged=merged))


@router.put("/providers/{provider_id:path}")
async def update_provider(
    provider_id: str,
    payload: ProviderConfigRequest,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    await service.update_provider(
        provider_id,
        payload.to_dashboard_config(fallback_id=provider_id),
    )
    return ok(message="更新成功，已经实时生效~")


@router.delete("/providers/{provider_id:path}")
async def delete_provider(
    provider_id: str,
    _auth: AuthContext = Depends(require_provider_scope),
    service: ProviderConfigService = Depends(get_service),
):
    await service.delete_provider(provider_id)
    return ok(message="删除成功，已经实时生效。")
