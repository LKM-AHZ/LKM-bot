"""ConfigProfileService operations for the Dashboard configuration API."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.core.computer import computer_client
from astrbot.core.computer.booters.local import LocalShellComponent
from astrbot.core.config.agent_runner import normalize_agent_runner
from astrbot.core.config.default import (
    CONFIG_METADATA_2,
    CONFIG_METADATA_3,
    CONFIG_METADATA_3_SYSTEM,
    DEFAULT_CONFIG,
)
from astrbot.core.config.i18n_utils import ConfigMetadataI18n
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.core.db import BaseDatabase
from astrbot.core.utils.totp import (
    is_totp_enabled,
    revoke_user_trusted_devices,
    set_pending_totp_secret,
    verify_configured_2fa_code,
)
from astrbot.dashboard.responses import ApiError
from astrbot.dashboard.services import config_service


class ConfigProfileService:
    def __init__(
        self,
        core_lifecycle: AstrBotCoreLifecycle,
        db: BaseDatabase | None = None,
        *,
        runtime: dict,
    ) -> None:
        self.core_lifecycle = core_lifecycle
        self.acm = core_lifecycle.astrbot_config_mgr
        self.db = db
        self.runtime = runtime

    def get_profile_schema(self) -> dict:
        return {
            "config": DEFAULT_CONFIG,
            "metadata": ConfigMetadataI18n.convert_to_i18n_keys(CONFIG_METADATA_3),
        }

    def get_system_schema(self) -> dict:
        return {
            "config": self.acm.confs["default"],
            "metadata": ConfigMetadataI18n.convert_to_i18n_keys(
                CONFIG_METADATA_3_SYSTEM
            ),
        }

    def get_system_config(self) -> dict:
        """Return the system configuration with the server's effective time.

        Returns:
            System configuration metadata, an aware UTC timestamp, and the
            effective UTC offset in minutes.
        """
        data = self.get_system_schema()
        server_utc_time = datetime.now(timezone.utc)
        timezone_name = str(data["config"].get("timezone") or "").strip()
        if timezone_name:
            try:
                configured_time = server_utc_time.astimezone(ZoneInfo(timezone_name))
            except (ValueError, ZoneInfoNotFoundError):
                configured_time = server_utc_time.astimezone()
        else:
            configured_time = server_utc_time.astimezone()
        utc_offset = configured_time.utcoffset()
        data["server_utc_time"] = server_utc_time.isoformat()
        data["server_utc_offset_minutes"] = (
            int(utc_offset.total_seconds() / 60) if utc_offset else 0
        )
        return data

    def list_profiles(self) -> dict:
        return {"info_list": self.acm.get_conf_list()}

    async def create_profile(
        self,
        name: str | None,
        config: dict | None,
        *,
        allow_admin_id_change: bool = True,
    ) -> dict:
        """Create a config profile with explicit admin-ID permission.

        Args:
            name: Display name for the new profile.
            config: Optional initial config content.
            allow_admin_id_change: Whether caller may define non-default admin IDs.

        Returns:
            Identifier of the created config profile.

        Raises:
            ApiError: If caller attempts to define administrator IDs without scope.
            ValueError: If configuration validation fails.
        """
        if (
            not allow_admin_id_change
            and isinstance(config, dict)
            and config.get("admins_id", DEFAULT_CONFIG.get("admins_id"))
            != DEFAULT_CONFIG.get("admins_id")
        ):
            raise ApiError(
                "config:edit_admin scope is required to change admins_id",
                status_code=403,
            )
        profile_config = copy.deepcopy(config or DEFAULT_CONFIG)
        if "agent_runner" in profile_config:
            profile_config["agent_runner"] = normalize_agent_runner(
                profile_config["agent_runner"]
            )
        errors, profile_config = config_service.validate_config(
            profile_config, CONFIG_METADATA_2, is_core=True, runtime=self.runtime
        )
        if errors:
            raise ValueError(f"Configuration validation failed: {errors}")
        conf_id = await self.acm.create_conf(
            name=name,
            config=profile_config,
        )
        await self.core_lifecycle.reload_pipeline_scheduler(conf_id)
        return {"conf_id": conf_id}

    async def create_profile_from_dashboard_payload(
        self,
        payload: object,
    ) -> dict:
        data = payload if isinstance(payload, dict) else {}
        if not data:
            raise ValueError("缺少配置数据")
        return await self.create_profile(data.get("name"), data.get("config"))

    def get_profile(self, config_id: str) -> dict:
        if config_id not in self.acm.confs:
            raise ValueError(f"Config file {config_id} does not exist")
        return {
            "config": self.acm.confs[config_id],
            "metadata": ConfigMetadataI18n.convert_to_i18n_keys(CONFIG_METADATA_3),
        }

    def get_profile_from_dashboard_query(
        self,
        *,
        config_id: str | None,
        system_config: bool,
    ) -> dict:
        if not config_id and not system_config:
            raise ValueError("缺少配置文件 ID")
        if system_config:
            return self.get_system_schema()
        if config_id is None:
            raise ValueError("abconf_id cannot be None")
        return self.get_profile(config_id)

    def get_profile_from_dashboard_args(self, args) -> dict:
        system_config = str(args.get("system_config", "0")).lower() == "1"
        return self.get_profile_from_dashboard_query(
            config_id=args.get("id"),
            system_config=system_config,
        )

    async def update_profile(
        self,
        config_id: str,
        config: dict,
        *,
        two_factor_code: str | None = None,
        allow_admin_id_change: bool = True,
    ) -> str | None:
        """Update a config profile with explicit admin-ID permission.

        Args:
            config_id: Identifier of the profile to update.
            config: Complete replacement config content.
            two_factor_code: Optional TOTP code for protected dashboard changes.
            allow_admin_id_change: Whether caller may change administrator IDs.

        Returns:
            Success message, optionally including a connectivity warning.

        Raises:
            ApiError: If admin IDs change without permission or TOTP is invalid.
            ValueError: If the profile does not exist or validation fails.
        """
        if config_id not in self.acm.confs:
            raise ValueError(f"Config file {config_id} does not exist")
        config = copy.deepcopy(config)
        if config_id == "default":
            default_conf = getattr(self.acm, "default_conf", self.acm.confs["default"])
            for key in ("provider_sources", "provider", "platform"):
                config[key] = default_conf.get(key, [])

        current_config = self.acm.confs[config_id]
        if (
            not allow_admin_id_change
            and "admins_id" in config
            and config.get("admins_id") != current_config.get("admins_id")
        ):
            raise ApiError(
                "config:edit_admin scope is required to change admins_id",
                status_code=403,
            )
        protected_2fa_changed = config_service._protected_2fa_config_changed(
            current_config, config
        )
        if (
            is_totp_enabled(current_config)
            and protected_2fa_changed
            and not await self._verify_config_2fa(current_config, two_factor_code)
        ):
            raise ApiError(
                "需要 TOTP 验证",
                status_code=401,
                data={"totp_required": True},
            )

        if not config_service._get_nested_value(
            config, ("dashboard", "totp", "enable")
        ):
            config_service._set_nested_value(
                config, ("dashboard", "totp", "secret"), ""
            )
            config_service._set_nested_value(
                config, ("dashboard", "totp", "recovery_code_hash"), ""
            )

        set_pending_totp_secret(None)
        config_service.save_config(
            config, self.acm.confs[config_id], is_core=True, runtime=self.runtime
        )
        booter = computer_client.local_booter
        if booter is not None and isinstance(booter.shell, LocalShellComponent):
            await booter.shell.shutdown_sessions(invalid_only=True)
        if protected_2fa_changed and self.db is not None:
            await revoke_user_trusted_devices(self.db)
        await self.core_lifecycle.reload_pipeline_scheduler(config_id)
        warning = await config_service._validate_neo_connectivity(config)
        if warning:
            return f"保存成功。{warning}"
        return "保存成功~"

    async def update_profile_from_dashboard_payload(
        self,
        payload: object,
        *,
        two_factor_code: str | None = None,
    ) -> str | None:
        data = payload if isinstance(payload, dict) else {}
        if not isinstance(payload, dict):
            raise ValueError("Invalid request payload")
        config = data.get("config")
        conf_id = data.get("conf_id")
        if not isinstance(config, dict):
            raise ValueError("Invalid config payload")
        return await self.update_profile(
            str(conf_id),
            config,
            two_factor_code=two_factor_code,
        )

    @staticmethod
    async def _verify_config_2fa(
        current_config: dict,
        two_factor_code: str | None,
    ) -> bool:
        code = (two_factor_code or "").strip()
        if not code:
            return False
        return bool(
            await verify_configured_2fa_code(
                current_config,
                code,
                include_pending=True,
                allow_recovery=False,
            )
        )

    async def rename_profile(self, config_id: str, name: str | None) -> None:
        if not await self.acm.update_conf_info(config_id, name=name):
            raise ValueError("Failed to update config profile")

    async def rename_profile_from_dashboard_payload(self, payload: object) -> str:
        data = payload if isinstance(payload, dict) else {}
        if not data:
            raise ValueError("缺少配置数据")
        conf_id = data.get("id")
        if not conf_id:
            raise ValueError("缺少配置文件 ID")
        await self.rename_profile(str(conf_id), name=data.get("name"))
        return "更新成功"

    async def delete_profile(self, config_id: str) -> None:
        if not await self.acm.delete_conf(config_id):
            raise ValueError("Failed to delete config profile")
        self.core_lifecycle.pipeline_scheduler_mapping.pop(config_id, None)

    async def delete_profile_from_dashboard_payload(self, payload: object) -> str:
        data = payload if isinstance(payload, dict) else {}
        if not data:
            raise ValueError("缺少配置数据")
        conf_id = data.get("id")
        if not conf_id:
            raise ValueError("缺少配置文件 ID")
        await self.delete_profile(str(conf_id))
        return "删除成功"
