"""ConfigDisplayService operations for the Dashboard configuration API."""

from __future__ import annotations

import asyncio
import copy
import inspect
from pathlib import Path
from typing import Any

from bot.core import file_token_service, logger
from bot.core.config.default import (
    CONFIG_METADATA_2,
)
from bot.core.config.i18n_utils import ConfigMetadataI18n
from bot.core.core_lifecycle import BotCoreLifecycle
from bot.core.platform.register import platform_cls_map, platform_registry
from bot.core.provider.register import provider_registry
from bot.core.star.star import star_registry


class ConfigDisplayService:
    def __init__(self, core_lifecycle: BotCoreLifecycle) -> None:
        self.core_lifecycle = core_lifecycle
        self.config = core_lifecycle.bot_config
        self._logo_token_cache: dict[str, str] = {}

    async def get_configs(self, plugin_name: str | None = None) -> dict:
        if not plugin_name:
            return await self.get_bot_config()
        return self.get_plugin_config(plugin_name)

    async def get_configs_from_dashboard_args(self, args) -> dict:
        return await self.get_configs(args.get("plugin_name", None))

    async def get_bot_config(self) -> dict:
        metadata: dict[str, Any] = copy.deepcopy(CONFIG_METADATA_2)
        platform_i18n = ConfigMetadataI18n.convert_to_i18n_keys(
            {
                "platform_group": {
                    "metadata": {
                        "platform": metadata["platform_group"]["metadata"]["platform"]
                    }
                }
            }
        )
        metadata["platform_group"]["metadata"]["platform"] = platform_i18n[
            "platform_group"
        ]["metadata"]["platform"]

        platform_default_tmpl = metadata["platform_group"]["metadata"]["platform"][
            "config_template"
        ]
        platform_i18n_translations = {}
        logo_registration_tasks = []

        for platform in platform_registry:
            if not platform.default_config_tmpl:
                continue

            platform_default_tmpl[platform.name] = copy.deepcopy(
                platform.default_config_tmpl
            )
            if platform.config_metadata:
                self.inject_platform_metadata_with_i18n(
                    platform,
                    metadata,
                    platform_i18n_translations,
                )
            if platform.logo_path:
                logo_registration_tasks.append(
                    self.register_platform_logo(platform, platform_default_tmpl),
                )

        if logo_registration_tasks:
            await asyncio.gather(*logo_registration_tasks, return_exceptions=True)

        provider_default_tmpl = metadata["provider_group"]["metadata"]["provider"][
            "config_template"
        ]
        for provider in provider_registry:
            if provider.default_config_tmpl:
                provider_default_tmpl[provider.type] = provider.default_config_tmpl

        return {
            "metadata": metadata,
            "config": self.config,
            "platform_i18n_translations": platform_i18n_translations,
        }

    def get_plugin_config(self, plugin_name: str) -> dict:
        result: dict = {"metadata": None, "config": None, "i18n": {}}

        for plugin_md in star_registry:
            if plugin_md.name != plugin_name:
                continue
            if not plugin_md.config:
                break
            result["config"] = plugin_md.config
            result["metadata"] = {
                plugin_name: {
                    "description": f"{plugin_name} 配置",
                    "type": "object",
                    "items": plugin_md.config.schema,
                },
            }
            result["i18n"] = plugin_md.i18n
            break

        return result

    async def register_platform_logo(self, platform, platform_default_tmpl) -> None:
        """Attach a reusable logo token to a platform's configuration template.

        Args:
            platform: Registered adapter metadata including its logo path.
            platform_default_tmpl: Configuration templates updated with the token.
        """
        if not platform.logo_path:
            return

        try:
            cache_key = f"{platform.name}:{platform.logo_path}"
            if cached_token := self._logo_token_cache.get(cache_key):
                if not await file_token_service.check_token_expired(cached_token):
                    self._set_platform_logo_token(
                        platform_default_tmpl,
                        platform.name,
                        cached_token,
                    )
                    logger.debug(
                        f"Using cached logo token for platform {platform.name}"
                    )
                    return
                self._logo_token_cache.pop(cache_key, None)

            platform_cls = platform_cls_map.get(platform.name)
            if not platform_cls:
                logger.warning(f"Platform class not found for {platform.name}")
                return

            logo_file_path = (
                Path(inspect.getfile(platform_cls)).parent / platform.logo_path
            )

            if not logo_file_path.exists():
                logger.warning(
                    f"Platform {platform.name} logo file not found: {logo_file_path}",
                )
                return

            logo_token = await file_token_service.register_file(
                str(logo_file_path),
                timeout=3600,
                single_use=False,
            )
            self._set_platform_logo_token(
                platform_default_tmpl,
                platform.name,
                logo_token,
            )
            self._logo_token_cache[cache_key] = logo_token
            logger.debug(f"Logo token registered for platform {platform.name}")

        except (ImportError, AttributeError) as exc:
            logger.warning(
                f"Failed to import required modules for platform {platform.name}: {exc}",
            )
        except OSError as exc:
            logger.warning(
                f"File system error for platform {platform.name} logo: {exc}"
            )
        except Exception as exc:
            logger.warning(
                f"Unexpected error registering logo for platform {platform.name}: {exc}",
            )

    @staticmethod
    def _set_platform_logo_token(
        platform_default_tmpl: dict,
        platform_name: str,
        logo_token: str,
    ) -> None:
        if platform_name not in platform_default_tmpl or not isinstance(
            platform_default_tmpl[platform_name],
            dict,
        ):
            platform_default_tmpl[platform_name] = {}
        platform_default_tmpl[platform_name]["logo_token"] = logo_token

    @staticmethod
    def inject_platform_metadata_with_i18n(
        platform,
        metadata,
        platform_i18n_translations: dict,
    ) -> None:
        metadata["platform_group"]["metadata"]["platform"].setdefault("items", {})
        platform_items_to_inject = copy.deepcopy(platform.config_metadata)

        if platform.i18n_resources:
            i18n_prefix = f"platform_group.platform.{platform.name}"

            for lang, lang_data in platform.i18n_resources.items():
                platform_i18n_translations.setdefault(lang, {}).setdefault(
                    "platform_group", {}
                ).setdefault("platform", {})[platform.name] = lang_data

            for field_key, field_value in platform_items_to_inject.items():
                for key in ("description", "hint", "labels"):
                    if key in field_value:
                        field_value[key] = f"{i18n_prefix}.{field_key}.{key}"

        metadata["platform_group"]["metadata"]["platform"]["items"].update(
            platform_items_to_inject
        )
