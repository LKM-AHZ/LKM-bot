"""BotConfigService operations for the Dashboard configuration API."""

from __future__ import annotations

import copy
from typing import get_args

from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.core.platform.register import platform_registry
from astrbot.core.utils.webhook_utils import ensure_platform_webhook_config
from astrbot.dashboard.schemas import BotCleanupTarget
from astrbot.dashboard.services import config_service
from astrbot.dashboard.services.bot_cleanup_service import delete_bot_data


class BotConfigService:
    def __init__(self, core_lifecycle: AstrBotCoreLifecycle) -> None:
        self.core_lifecycle = core_lifecycle
        self.config = core_lifecycle.astrbot_config

    def list_bot_types(self) -> dict:
        bot_types = []
        for platform in platform_registry:
            bot_types.append(
                {
                    "type": platform.name,
                    "id": platform.id,
                    "description": platform.description,
                    "display_name": platform.adapter_display_name or platform.name,
                    "default_config": copy.deepcopy(platform.default_config_tmpl),
                    "schema": copy.deepcopy(platform.config_metadata or {}),
                    "support_streaming_message": platform.support_streaming_message,
                    "support_proactive_message": platform.support_proactive_message,
                }
            )
        return {"bot_types": bot_types}

    def list_bots(
        self, *, enabled: bool | None = None, type_: str | None = None
    ) -> dict:
        bots = []
        for bot in self.config.get("platform", []):
            if enabled is not None and bool(bot.get("enable", False)) != enabled:
                continue
            if type_ and bot.get("type") != type_:
                continue
            bots.append(copy.deepcopy(bot))
        return {"bots": bots}

    def get_bot(self, bot_id: str) -> dict:
        bot = self._find_bot(bot_id)
        if bot is None:
            raise ValueError(f"Bot {bot_id} not found")
        return {"bot": copy.deepcopy(bot)}

    def get_bot_stats(self) -> dict:
        return self.core_lifecycle.platform_manager.get_all_stats()

    async def create_bot(self, config: dict) -> None:
        bot_id = config.get("id")
        if not bot_id:
            raise ValueError("Bot config must have an 'id' field")
        if self._find_bot(bot_id) is not None:
            raise ValueError(f"Bot {bot_id} already exists")
        ensure_platform_webhook_config(config)
        self.config["platform"].append(config)
        config_service.save_config(self.config, self.config, is_core=True)
        if event_bus := getattr(self.core_lifecycle, "event_bus", None):
            event_bus.stopped_platforms.discard(bot_id)
        await self.core_lifecycle.platform_manager.load_platform(config)

    async def update_bot(self, bot_id: str, config: dict) -> None:
        if config.get("id") != bot_id:
            raise ValueError("Bot id cannot be changed")
        ensure_platform_webhook_config(config)
        for idx, bot in enumerate(self.config.get("platform", [])):
            if bot.get("id") == bot_id:
                self.config["platform"][idx] = config
                config_service.save_config(self.config, self.config, is_core=True)
                await self.core_lifecycle.platform_manager.reload(config)
                return
        raise ValueError(f"Bot {bot_id} not found")

    async def set_bot_enabled(self, bot_id: str, enabled: bool) -> None:
        bot = self._find_bot(bot_id)
        if bot is None:
            raise ValueError(f"Bot {bot_id} not found")
        new_config = copy.deepcopy(bot)
        new_config["enable"] = enabled
        await self.update_bot(bot_id, new_config)

    async def delete_bot(
        self, bot_id: str, *, cleanup: list[BotCleanupTarget] | None = None
    ) -> None:
        """Stop an adapter and delete only explicitly selected associated data.

        Args:
            bot_id: Exact platform instance ID.
            cleanup: Data categories to remove; omitted categories are retained.

        Raises:
            ValueError: The bot or a cleanup category is invalid.
            Exception: Cleanup or configuration persistence fails.
        """
        bot = self._find_bot(bot_id)
        if bot is None:
            raise ValueError(f"Bot {bot_id} not found")
        targets = set(cleanup or [])
        if targets - set(get_args(BotCleanupTarget)):
            raise ValueError("Invalid bot cleanup category")
        event_bus = getattr(self.core_lifecycle, "event_bus", None)
        await self.core_lifecycle.platform_manager.terminate_platform(bot_id)
        try:
            if event_bus:
                await event_bus.stop_platform(bot_id)
            if targets:
                await delete_bot_data(self.core_lifecycle, bot_id, targets)
            self.config["platform"].remove(bot)
            try:
                config_service.save_config(self.config, self.config, is_core=True)
            except Exception:
                self.config["platform"].append(bot)
                raise
        except Exception:
            # Keep the adapter available for inspection or retry after a failure.
            if event_bus:
                event_bus.stopped_platforms.discard(bot_id)
            if bot.get("enable"):
                await self.core_lifecycle.platform_manager.load_platform(bot)
            raise

    def list_platforms_for_dashboard(self) -> dict:
        return {"platforms": self.list_bots()["bots"]}

    async def create_bot_from_dashboard_payload(self, payload: object) -> str:
        if not isinstance(payload, dict):
            raise ValueError("参数错误")
        await self.create_bot(payload)
        return "新增平台配置成功~"

    async def update_bot_from_dashboard_payload(self, payload: object) -> str:
        data = payload if isinstance(payload, dict) else {}
        origin_platform_id = data.get("id")
        new_config = data.get("config")
        if not origin_platform_id or not isinstance(new_config, dict):
            raise ValueError("参数错误")
        if origin_platform_id != new_config.get("id"):
            raise ValueError("机器人名称不允许修改")

        try:
            await self.update_bot(origin_platform_id, new_config)
        except ValueError as exc:
            if "not found" in str(exc):
                raise ValueError("未找到对应平台") from exc
            raise
        return "更新平台配置成功~"

    async def delete_bot_from_dashboard_payload(self, payload: object) -> str:
        data = payload if isinstance(payload, dict) else {}
        platform_id = data.get("id")
        if not platform_id:
            raise ValueError("缺少平台 ID")
        try:
            await self.delete_bot(str(platform_id))
        except ValueError as exc:
            if "not found" in str(exc):
                raise ValueError("未找到对应平台") from exc
            raise
        return "删除平台配置成功~"

    def _find_bot(self, bot_id: str) -> dict | None:
        for bot in self.config.get("platform", []):
            if bot.get("id") == bot_id:
                return bot
        return None
