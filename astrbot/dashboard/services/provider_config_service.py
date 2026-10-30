"""ProviderConfigService operations for the Dashboard configuration API."""

from __future__ import annotations

import copy
from typing import Any

from astrbot.core import logger
from astrbot.core.config.default import (
    CONFIG_METADATA_2,
)
from astrbot.core.config.i18n_utils import ConfigMetadataI18n
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.core.provider.register import provider_registry
from astrbot.core.utils.async_utils import run_maybe_async
from astrbot.dashboard.services import config_service


class ProviderConfigService:
    CAPABILITY_TO_PROVIDER_TYPE = {
        "chat": "chat_completion",
        "stt": "speech_to_text",
        "tts": "text_to_speech",
        "embedding": "embedding",
        "rerank": "rerank",
    }

    def __init__(self, core_lifecycle: AstrBotCoreLifecycle) -> None:
        self.core_lifecycle = core_lifecycle
        self.config = core_lifecycle.astrbot_config
        self.provider_manager = core_lifecycle.provider_manager

    @staticmethod
    def _strip_legacy_reasoning_metadata(provider: dict) -> dict:
        """Remove reasoning metadata accidentally stored as provider configuration.

        Args:
            provider: Provider configuration to sanitize in place.

        Returns:
            The sanitized provider configuration.
        """
        if provider.get("provider_source_id"):
            provider.pop("reasoning", None)
        return provider

    def get_provider_schema(self) -> dict:
        config_metadata: dict[str, Any] = CONFIG_METADATA_2
        provider_metadata = ConfigMetadataI18n.convert_to_i18n_keys(
            {
                "provider_group": {
                    "metadata": {
                        "provider": config_metadata["provider_group"]["metadata"][
                            "provider"
                        ]
                    }
                }
            }
        )
        config_schema = {
            "provider": provider_metadata["provider_group"]["metadata"]["provider"]
        }
        provider_default_tmpl = config_schema["provider"]["config_template"]
        for provider in provider_registry:
            if provider.default_config_tmpl:
                provider_default_tmpl[provider.type] = provider.default_config_tmpl
        providers = [
            copy.deepcopy(provider)
            for provider in self.config.get("provider", [])
            if provider.get("provider_type") != "agent_runner"
        ]
        from astrbot.core.utils.llm_metadata import LLM_METADATAS

        model_metadata = {}
        for provider in providers:
            self._strip_legacy_reasoning_metadata(provider)
            model_id = provider.get("model")
            if isinstance(model_id, str) and model_id in LLM_METADATAS:
                model_metadata[model_id] = LLM_METADATAS[model_id]
        return {
            "config_schema": config_schema,
            "providers": providers,
            "provider_sources": self.config.get("provider_sources", []),
            "model_metadata": model_metadata,
        }

    def list_provider_sources(self) -> dict:
        return {
            "provider_sources": copy.deepcopy(self.config.get("provider_sources", []))
        }

    def get_provider_source(self, source_id: str) -> dict:
        source = self._find_provider_source(source_id)
        if source is None:
            raise ValueError(f"Provider source {source_id} not found")
        return {"provider_source": copy.deepcopy(source)}

    async def upsert_provider_source(self, source_id: str, config: dict) -> None:
        config = copy.deepcopy(config)
        next_source_id = str(config.get("id") or source_id).strip()
        if not next_source_id:
            raise ValueError("Provider source config must have an 'id' field")
        config["id"] = next_source_id
        sources = self.config.setdefault("provider_sources", [])

        for source in sources:
            if source.get("id") == next_source_id and next_source_id != source_id:
                raise ValueError(
                    f"Provider source ID '{next_source_id}' exists already, please try another ID."
                )

        for idx, source in enumerate(sources):
            if source.get("id") == source_id:
                old_source_id = source.get("id") or source_id
                sources[idx] = config
                affected_providers = self._move_providers_to_source(
                    old_source_id,
                    next_source_id,
                )
                config_service.save_config(self.config, self.config, is_core=True)
                self.provider_manager.provider_sources_config = sources
                await self._reload_providers(affected_providers)
                return

        sources.append(config)
        config_service.save_config(self.config, self.config, is_core=True)
        self.provider_manager.provider_sources_config = sources

    async def delete_provider_source(self, source_id: str) -> None:
        sources = self.config.get("provider_sources", [])
        next_sources = [source for source in sources if source.get("id") != source_id]
        if len(next_sources) == len(sources):
            raise ValueError(f"Provider source {source_id} not found")
        self.config["provider_sources"] = next_sources
        await self.provider_manager.delete_provider(provider_source_id=source_id)
        config_service.save_config(self.config, self.config, is_core=True)
        self.provider_manager.provider_sources_config = next_sources

    async def upsert_provider_source_from_dashboard_payload(
        self, payload: object
    ) -> str:
        if not isinstance(payload, dict) or not payload:
            raise ValueError("缺少配置数据")

        new_source_config = payload.get("config") or payload
        original_id = payload.get("original_id")
        if not original_id:
            raise ValueError("缺少 original_id")
        if not isinstance(new_source_config, dict):
            raise ValueError("缺少或错误的配置数据")
        if not new_source_config.get("id"):
            new_source_config["id"] = original_id

        await self.upsert_provider_source(str(original_id), new_source_config)
        return "更新 provider source 成功"

    async def delete_provider_source_from_dashboard_payload(
        self, payload: object
    ) -> str:
        if not isinstance(payload, dict) or not payload:
            raise ValueError("缺少配置数据")

        provider_source_id = payload.get("id")
        if not provider_source_id:
            raise ValueError("缺少 provider_source_id")

        try:
            await self.delete_provider_source(str(provider_source_id))
        except ValueError as exc:
            if "not found" in str(exc):
                raise ValueError("未找到对应的 provider source") from exc
            raise
        return "删除 provider source 成功"

    async def list_provider_source_models(self, source_id: str) -> dict:
        source = self._find_provider_source(source_id)
        if source is None:
            raise ValueError(f"Provider source {source_id} not found")

        from astrbot.core.provider import Provider
        from astrbot.core.provider.register import provider_cls_map
        from astrbot.core.utils.llm_metadata import LLM_METADATAS

        provider_type = source.get("type")
        if not provider_type:
            raise ValueError("Provider source missing type")
        try:
            self.provider_manager.dynamic_import_provider(provider_type)
        except ImportError as exc:
            raise ValueError(f"动态导入提供商适配器失败: {exc!s}") from exc
        provider_metadata = provider_cls_map.get(provider_type)
        cls_type = provider_metadata.cls_type if provider_metadata else None
        if cls_type is None or not issubclass(cls_type, Provider):
            raise ValueError(f"Provider source {source_id} does not support model list")

        inst = cls_type(source, {})
        init_fn = getattr(inst, "initialize", None)
        if callable(init_fn):
            await run_maybe_async(init_fn)
        try:
            models = await inst.get_models()
            models = models or []
            return {
                "models": models,
                "provider_source_id": source_id,
                "model_metadata": {
                    model_id: LLM_METADATAS[model_id]
                    for model_id in models
                    if model_id in LLM_METADATAS
                },
            }
        finally:
            terminate_fn = getattr(inst, "terminate", None)
            if callable(terminate_fn):
                await run_maybe_async(terminate_fn)

    async def list_provider_source_models_for_dashboard(
        self,
        source_id: str | None,
    ) -> dict:
        if not source_id:
            raise ValueError("缺少参数 source_id")
        data = await self.list_provider_source_models(source_id)
        data.pop("provider_source_id", None)
        return data

    async def list_provider_models(self, provider_id: str) -> dict:
        from astrbot.core.provider import Provider
        from astrbot.core.utils.llm_metadata import LLM_METADATAS

        provider = self.provider_manager.inst_map.get(provider_id)
        if not provider:
            raise ValueError(f"未找到 ID 为 {provider_id} 的提供商")
        if not isinstance(provider, Provider):
            raise ValueError(f"提供商 {provider_id} 类型不支持获取模型列表")

        models = await provider.get_models()
        models = models or []
        return {
            "models": models,
            "provider_id": provider_id,
            "model_metadata": {
                model_id: LLM_METADATAS[model_id]
                for model_id in models
                if model_id in LLM_METADATAS
            },
        }

    async def list_provider_models_for_dashboard(
        self,
        provider_id: str | None,
    ) -> dict:
        if not provider_id:
            raise ValueError("缺少参数 provider_id")
        return await self.list_provider_models(provider_id)

    async def list_provider_models_from_dashboard_args(self, args) -> dict:
        return await self.list_provider_models_for_dashboard(
            args.get("provider_id", None)
        )

    async def get_embedding_dimension(self, provider_config: dict | None) -> dict:
        if not provider_config:
            raise ValueError("缺少参数 provider_config")

        from astrbot.core.provider.provider import EmbeddingProvider
        from astrbot.core.provider.register import provider_cls_map

        provider_type = provider_config.get("type")
        if not provider_type:
            raise ValueError("provider_config 缺少 type 字段")

        if provider_type not in provider_cls_map:
            try:
                dynamic_import_provider = getattr(
                    self.provider_manager,
                    "dynamic_import_provider",
                    None,
                )
                if not callable(dynamic_import_provider):
                    raise ImportError(provider_type)
                dynamic_import_provider(provider_type)
            except ImportError as exc:
                raise ValueError(
                    "提供商适配器加载失败，请检查提供商类型配置或查看服务端日志"
                ) from exc

        provider_metadata = provider_cls_map.get(provider_type)
        cls_type = provider_metadata.cls_type if provider_metadata else None
        if not cls_type:
            raise ValueError(f"无法找到 {provider_type} 的类")

        inst = cls_type(provider_config, {})
        try:
            init_fn = getattr(inst, "initialize", None)
            if callable(init_fn):
                await run_maybe_async(init_fn)

            if not isinstance(inst, EmbeddingProvider):
                raise ValueError("提供商不是 EmbeddingProvider 类型")

            vec = await inst.get_embedding("echo")
            dim = len(vec)
            logger.info(
                f"检测到 {provider_config.get('id', 'unknown')} 的嵌入向量维度为 {dim}",
            )
            return {"embedding_dimensions": dim}
        finally:
            terminate_fn = getattr(inst, "terminate", None)
            if callable(terminate_fn):
                await run_maybe_async(terminate_fn)

    async def get_embedding_dimension_from_dashboard_payload(
        self,
        payload: object,
    ) -> dict:
        data = payload if isinstance(payload, dict) else {}
        return await self.get_embedding_dimension(data.get("provider_config"))

    def list_providers(
        self,
        *,
        capability: str | None = None,
        source_id: str | None = None,
        enabled: bool | None = None,
    ) -> dict:
        from astrbot.core.utils.llm_metadata import LLM_METADATAS

        provider_type = self._resolve_provider_type(capability)
        providers = []
        model_metadata = {}
        source_provider_type = {
            source["id"]: source.get("provider_type", "chat_completion")
            for source in self.provider_manager.provider_sources_config
        }
        for provider in self.provider_manager.providers_config:
            if provider.get("provider_type") == "agent_runner":
                continue
            if source_id and provider.get("provider_source_id") != source_id:
                continue
            if enabled is not None and bool(provider.get("enable", False)) != enabled:
                continue
            effective_type = provider.get("provider_type")
            if not effective_type and provider.get("provider_source_id"):
                effective_type = source_provider_type.get(
                    provider.get("provider_source_id"), "chat_completion"
                )
            if provider_type and effective_type != provider_type:
                continue
            if provider.get("provider_source_id"):
                provider_response = self.provider_manager.get_merged_provider_config(
                    provider
                )
            else:
                provider_response = copy.deepcopy(provider)
            self._strip_legacy_reasoning_metadata(provider_response)
            model_id = provider_response.get("model")
            if isinstance(model_id, str) and model_id in LLM_METADATAS:
                model_metadata[model_id] = LLM_METADATAS[model_id]
            providers.append(provider_response)
        return {"providers": providers, "model_metadata": model_metadata}

    def list_providers_for_dashboard_types(
        self, provider_type: str | None
    ) -> list[dict]:
        if not provider_type:
            raise ValueError("缺少参数 provider_type")

        provider_list = []
        seen_ids = set()
        for item in provider_type.split(","):
            providers = self.list_providers(capability=item)["providers"]
            for provider in providers:
                provider_id = provider.get("id")
                if provider_id in seen_ids:
                    continue
                seen_ids.add(provider_id)
                provider_list.append(provider)
        return provider_list

    def list_providers_from_dashboard_args(self, args) -> list[dict]:
        return self.list_providers_for_dashboard_types(args.get("provider_type", None))

    def get_provider(self, provider_id: str, *, merged: bool = False) -> dict:
        provider = self.provider_manager.get_provider_config_by_id(
            provider_id,
            merged=merged,
        )
        if provider is None:
            raise ValueError(f"Provider {provider_id} not found")
        provider_response = copy.deepcopy(provider)
        self._strip_legacy_reasoning_metadata(provider_response)
        from astrbot.core.utils.llm_metadata import LLM_METADATAS

        model_id = provider_response.get("model")
        model_metadata = {}
        if isinstance(model_id, str) and model_id in LLM_METADATAS:
            model_metadata[model_id] = LLM_METADATAS[model_id]
        return {"provider": provider_response, "model_metadata": model_metadata}

    async def create_provider(self, config: dict, source_id: str | None = None) -> None:
        config = copy.deepcopy(config)
        if source_id:
            config["provider_source_id"] = source_id
        self._strip_legacy_reasoning_metadata(config)
        await self.provider_manager.create_provider(config)

    async def update_provider(self, provider_id: str, config: dict) -> None:
        config = copy.deepcopy(config)
        if not config.get("id"):
            config["id"] = provider_id
        self._strip_legacy_reasoning_metadata(config)
        await self.provider_manager.update_provider(provider_id, config)

    async def set_provider_enabled(self, provider_id: str, enabled: bool) -> None:
        provider = self.provider_manager.get_provider_config_by_id(provider_id)
        if provider is None:
            raise ValueError(f"Provider {provider_id} not found")
        provider["enable"] = enabled
        await self.provider_manager.update_provider(provider_id, provider)

    async def delete_provider(self, provider_id: str) -> None:
        await self.provider_manager.delete_provider(provider_id=provider_id)

    async def create_provider_from_dashboard_payload(self, payload: object) -> str:
        if not isinstance(payload, dict):
            raise ValueError("参数错误")
        await self.create_provider(payload)
        return "新增服务提供商配置成功"

    async def update_provider_from_dashboard_payload(self, payload: object) -> str:
        data = payload if isinstance(payload, dict) else {}
        origin_provider_id = data.get("id")
        new_config = data.get("config")
        if not origin_provider_id or not isinstance(new_config, dict):
            raise ValueError("参数错误")

        await self.update_provider(origin_provider_id, new_config)
        return "更新成功，已经实时生效~"

    async def delete_provider_from_dashboard_payload(self, payload: object) -> str:
        data = payload if isinstance(payload, dict) else {}
        provider_id = data.get("id", "")
        if not provider_id:
            raise ValueError("缺少参数 id")

        await self.delete_provider(provider_id)
        return "删除成功，已经实时生效。"

    async def test_provider(self, provider_id: str) -> dict:
        target = self.provider_manager.inst_map.get(provider_id)
        if not target:
            raise ValueError(f"Provider {provider_id} not found")
        meta = target.meta()
        provider_type = getattr(meta, "provider_type", None)
        result = {
            "id": getattr(meta, "id", provider_id),
            "model": getattr(meta, "model", None),
            "type": getattr(provider_type, "value", None),
            "name": provider_id,
            "status": "unavailable",
            "error": None,
        }
        try:
            await target.test()
            result["status"] = "available"
        except Exception as exc:
            result["error"] = str(exc)
        return result

    async def test_provider_from_dashboard_args(self, args) -> dict:
        provider_id = args.get("id")
        if not provider_id:
            raise ValueError("Missing provider_id parameter")
        logger.info(f"API call: /config/provider/check_one id={provider_id}")
        return await self.test_provider(provider_id)

    def _find_provider_source(self, source_id: str) -> dict | None:
        for source in self.config.get("provider_sources", []):
            if source.get("id") == source_id:
                return source
        return None

    def _resolve_provider_type(self, capability: str | None) -> str | None:
        if not capability:
            return None
        return self.CAPABILITY_TO_PROVIDER_TYPE.get(capability, capability)

    async def _reload_providers_for_source(self, source_id: str) -> None:
        await self._reload_providers(
            [
                provider
                for provider in list(self.config.get("provider", []))
                if provider.get("provider_source_id") == source_id
            ]
        )

    def _move_providers_to_source(
        self,
        old_source_id: str,
        next_source_id: str,
    ) -> list[dict]:
        affected_providers = []
        for provider in self.config.get("provider", []):
            if provider.get("provider_source_id") == old_source_id:
                provider["provider_source_id"] = next_source_id
                affected_providers.append(provider)
        return affected_providers

    async def _reload_providers(self, providers: list[dict]) -> None:
        reload_fn = getattr(self.provider_manager, "reload", None)
        if not callable(reload_fn):
            return
        for provider in providers:
            await run_maybe_async(lambda provider=provider: reload_fn(provider))
