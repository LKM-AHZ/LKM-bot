from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.api import sp
from bot.core import DEMO_MODE, file_token_service, logger
from bot.core.bot_agent_runtime.computer.computer_client import (
    sync_skills_to_active_sandboxes,
)
from bot.core.repository import (
    GitUnavailableError,
    normalize_repository_url,
    parse_repository_url,
)
from bot.core.skills.skill_manager import SkillManager
from bot.core.star.filter.command import CommandFilter
from bot.core.star.filter.command_group import CommandGroupFilter
from bot.core.star.filter.permission import PermissionTypeFilter
from bot.core.star.filter.regex import RegexFilter
from bot.core.star.star import StarMetadata
from bot.core.star.star_handler import EventType, star_handlers_registry
from bot.core.star.star_manager import (
    PluginManager,
    PluginVersionUnsupportedError,
)
from bot.core.utils.bot_path import get_bot_temp_path

PLUGIN_UPDATE_CONCURRENCY = 3
PLUGIN_OPERATION_FAILED_MESSAGE = "插件操作失败，请查看服务端日志。"
PLUGIN_UPDATE_FAILED_MESSAGE = "更新失败，请查看服务端日志。"
PLUGIN_INSTALL_SOURCES_KEY = "plugin_install_sources"
PLUGIN_UPDATE_DISABLED_MESSAGE = "该插件没有可用的仓库安装源，无法更新。"
PLUGIN_UPDATE_SOURCE_REQUIRED_MESSAGE = "该插件缺少安装源记录，无法更新。"
PLUGIN_COMPONENT_TYPE_ORDER = {
    "page": 0,
    "skill": 1,
    "command": 2,
    "llm_tool": 3,
    "listener": 4,
    "hook": 5,
}

LogoTokenResolver = Callable[[str], Awaitable[str | None]]
InstalledAtResolver = Callable[[StarMetadata], str | None]
PluginPagesSerializer = Callable[[StarMetadata], Awaitable[list[dict]]]


class PluginServiceError(Exception):
    def __init__(
        self,
        message: str,
        *,
        public_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.public_message = public_message or message


class PluginServiceWarning(Exception):
    def __init__(
        self,
        message: str,
        data: dict[str, Any],
        *,
        public_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.public_message = public_message or message
        self.data = data


class PluginService:
    def __init__(
        self,
        core_lifecycle,
        plugin_manager: PluginManager,
    ) -> None:
        self.core_lifecycle = core_lifecycle
        self.plugin_manager = plugin_manager
        self.translated_event_type = {
            EventType.AdapterMessageEvent: "平台消息下发时",
            EventType.OnLLMRequestEvent: "LLM 请求时",
            EventType.OnLLMResponseEvent: "LLM 响应后",
            EventType.OnAgentBeginEvent: "Agent 开始运行时",
            EventType.OnAgentDoneEvent: "Agent 运行完成后",
            EventType.OnDecoratingResultEvent: "回复消息前",
            EventType.OnCallingFuncToolEvent: "函数工具",
            EventType.OnAfterMessageSentEvent: "发送消息后",
            EventType.OnPluginErrorEvent: "插件报错时",
        }
        self._logo_cache: dict[str, str] = {}

    @staticmethod
    def _payload(data: object) -> dict[str, Any]:
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _ensure_not_demo() -> None:
        if DEMO_MODE:
            raise PluginServiceError(
                "You are not permitted to do this operation in demo mode"
            )

    async def sync_skills_after_plugin_change(self) -> None:
        try:
            await sync_skills_to_active_sandboxes()
        except Exception:
            logger.warning("Failed to sync plugin-provided skills to active sandboxes.")

    async def check_plugin_version_support(self, data: object) -> dict:
        payload = self._payload(data)
        version_spec = payload.get("astrbot_version", "")
        is_valid, message = self.plugin_manager._validate_astrbot_version_specifier(
            version_spec
        )
        return {
            "supported": is_valid,
            "message": message,
            "astrbot_version": version_spec,
        }

    async def reload_failed_plugin(self, data: object) -> tuple[None, str]:
        self._ensure_not_demo()
        payload = data if isinstance(data, dict) else {}
        dir_name = payload.get("dir_name")
        if not dir_name:
            raise PluginServiceError("缺少插件目录名")

        success, err = await self.plugin_manager.reload_failed_plugin(dir_name)
        if not success:
            raise PluginServiceError(f"重载失败: {err}", public_message="重载失败")
        await self.sync_skills_after_plugin_change()
        return None, f"插件 {dir_name} 重载成功。"

    async def reload_plugin(self, data: object) -> tuple[None, str]:
        self._ensure_not_demo()
        payload = data if isinstance(data, dict) else {}
        plugin_name = payload.get("name", None)
        success, message = await self.plugin_manager.reload(plugin_name)
        if not success:
            raise PluginServiceError(
                message or "插件重载失败",
                public_message="插件重载失败",
            )
        await self.sync_skills_after_plugin_change()
        return None, "重载成功。"

    async def list_plugins(
        self,
        *,
        plugin_name: str | None,
        logo_token_resolver: LogoTokenResolver,
        installed_at_resolver: InstalledAtResolver,
        serialize_pages: PluginPagesSerializer,
    ) -> tuple[list[dict], str | None]:
        plugins = [
            plugin
            for plugin in self.plugin_manager.context.get_all_stars()
            if not (plugin_name and plugin.name != plugin_name)
        ]
        install_sources = await self.get_plugin_install_sources()

        async def process_plugin(plugin: StarMetadata):
            logo_url = await self.resolve_plugin_logo_url(plugin, logo_token_resolver)
            pages = await serialize_pages(plugin)
            return plugin, logo_url, pages

        results = await asyncio.gather(*(process_plugin(plugin) for plugin in plugins))
        payload = []
        for plugin, logo_url, pages in results:
            if self.is_ghost_plugin(plugin):
                continue
            payload.append(
                {
                    **self.serialize_plugin_base(
                        plugin,
                        logo_url=logo_url,
                        installed_at=installed_at_resolver(plugin),
                        install_source=self.resolve_effective_plugin_install_source(
                            plugin,
                            install_sources,
                        ),
                    ),
                    "pages": [page["name"] for page in pages],
                    "views": pages,
                }
            )
        return payload, getattr(self.plugin_manager, "failed_plugin_info", None)

    async def list_plugins_from_dashboard_query(
        self,
        *,
        plugin_name: str | None,
        logo_token_resolver: LogoTokenResolver,
        installed_at_resolver: InstalledAtResolver,
        serialize_pages: PluginPagesSerializer,
    ) -> tuple[list[dict], str | None]:
        return await self.list_plugins(
            plugin_name=plugin_name,
            logo_token_resolver=logo_token_resolver,
            installed_at_resolver=installed_at_resolver,
            serialize_pages=serialize_pages,
        )

    async def get_plugin_detail(
        self,
        *,
        plugin_name: str | None,
        logo_token_resolver: LogoTokenResolver,
        installed_at_resolver: InstalledAtResolver,
        serialize_pages: PluginPagesSerializer,
    ) -> dict:
        if not plugin_name:
            raise PluginServiceError("缺少插件名")

        install_sources = await self.get_plugin_install_sources()
        for plugin in self.plugin_manager.context.get_all_stars():
            if plugin.name != plugin_name:
                continue

            logo_url = await self.resolve_plugin_logo_url(plugin, logo_token_resolver)
            return {
                **self.serialize_plugin_base(
                    plugin,
                    logo_url=logo_url,
                    installed_at=installed_at_resolver(plugin),
                    install_source=self.resolve_effective_plugin_install_source(
                        plugin,
                        install_sources,
                    ),
                ),
                "components": await self.get_plugin_components_info(
                    plugin,
                    serialize_pages,
                ),
            }

        raise PluginServiceError("插件不存在")

    async def get_plugin_detail_from_dashboard_query(
        self,
        *,
        plugin_name: str | None,
        logo_token_resolver: LogoTokenResolver,
        installed_at_resolver: InstalledAtResolver,
        serialize_pages: PluginPagesSerializer,
    ) -> dict:
        return await self.get_plugin_detail(
            plugin_name=plugin_name,
            logo_token_resolver=logo_token_resolver,
            installed_at_resolver=installed_at_resolver,
            serialize_pages=serialize_pages,
        )

    async def resolve_plugin_logo_url(
        self,
        plugin: StarMetadata,
        logo_token_resolver: LogoTokenResolver,
    ) -> str | None:
        if not plugin.logo_path:
            return None
        logo_token = await logo_token_resolver(plugin.logo_path)
        return f"/api/v1/files/tokens/{logo_token}" if logo_token else None

    async def get_plugin_logo_token(self, logo_path: str) -> str | None:
        """Get a reusable logo token, replacing expired cached tokens.

        Args:
            logo_path: The local path to the plugin logo.

        Returns:
            An active file token, or None if the file cannot be registered.
        """
        try:
            if token := self._logo_cache.get(logo_path):
                if not await file_token_service.check_token_expired(token):
                    return token
            token = await file_token_service.register_file(
                logo_path, timeout=300, single_use=False
            )
            self._logo_cache[logo_path] = token
            return token
        except Exception as exc:
            logger.warning(f"Failed to get the plugin logo: {exc}")
            return None

    def resolve_plugin_metadata_dir(self, plugin: StarMetadata) -> Path | None:
        if not plugin.root_dir_name:
            return None

        base_dir = Path(
            self.plugin_manager.reserved_plugin_path
            if plugin.reserved
            else self.plugin_manager.plugin_store_path
        )
        plugin_dir = base_dir / plugin.root_dir_name
        if not plugin_dir.is_dir():
            return None
        return plugin_dir

    def get_plugin_installed_at(self, plugin: StarMetadata) -> str | None:
        plugin_dir = self.resolve_plugin_metadata_dir(plugin)
        if plugin_dir is None:
            return None

        try:
            return datetime.fromtimestamp(
                plugin_dir.stat().st_mtime,
                timezone.utc,
            ).isoformat()
        except OSError as exc:
            logger.warning(
                f"Failed to get the installation time for plugin {plugin.name}: {exc!s}"
            )
            return None

    @staticmethod
    async def get_plugin_install_sources() -> dict[str, dict[str, Any]]:
        """Return persisted plugin installation source records.

        Returns:
            A mapping keyed by local plugin root directory name.
        """
        records = await sp.global_get(PLUGIN_INSTALL_SOURCES_KEY, {})
        if not isinstance(records, dict):
            return {}
        return {
            str(key): value for key, value in records.items() if isinstance(value, dict)
        }

    @staticmethod
    async def save_plugin_install_sources(
        records: dict[str, dict[str, Any]],
    ) -> None:
        """Persist plugin installation source records.

        Args:
            records: Mapping keyed by local plugin root directory name.
        """
        await sp.global_put(PLUGIN_INSTALL_SOURCES_KEY, records)

    @staticmethod
    def resolve_plugin_install_source(
        plugin: StarMetadata,
        records: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Resolve a source record for a loaded plugin.

        Args:
            plugin: Loaded plugin metadata.
            records: Persisted source records.

        Returns:
            The source record when one is available.
        """
        for key in (plugin.root_dir_name, plugin.name):
            if key and isinstance(records.get(key), dict):
                return records[key]
        return None

    def resolve_effective_plugin_install_source(
        self,
        plugin: StarMetadata,
        records: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Resolve a plugin source for display.

        Args:
            plugin: Loaded plugin metadata.
            records: Persisted source records.

        Returns:
            The persisted source record, an implicit display-only source record, or None.
        """
        record = self.resolve_plugin_install_source(plugin, records)
        if isinstance(record, dict):
            return record
        if plugin.reserved:
            return None

        plugin_name = str(plugin.name or "").strip()
        implicit_record = {
            "schema_version": 1,
            "root_dir_name": plugin.root_dir_name,
            "install_method": "repository",
            "repo": str(plugin.repo or "").strip(),
            "download_url": "",
            "implicit": True,
        }
        if plugin_name:
            implicit_record["name"] = plugin_name
            implicit_record["marketplace_name"] = plugin_name.replace("_", "-")
        return implicit_record

    def find_plugin_by_name(self, plugin_name: str | None) -> StarMetadata | None:
        """Find a loaded plugin by metadata name.

        Args:
            plugin_name: Metadata plugin name.

        Returns:
            The matching plugin metadata, if loaded.
        """
        if not plugin_name:
            return None
        plugin = self.plugin_manager.context.get_registered_star(plugin_name)
        if plugin:
            return plugin
        for item in self.plugin_manager.context.get_all_stars():
            if item.name == plugin_name or item.root_dir_name == plugin_name:
                return item
        return None

    @staticmethod
    def repo_identifier_from_url(repo_url: object) -> str:
        """Build an owner/repo identifier from a supported repository URL.

        Args:
            repo_url: Repository URL.

        Returns:
            The owner/repo identifier, or an empty string when unavailable.
        """
        text = str(repo_url or "").strip().rstrip("/")
        if not text:
            return ""
        try:
            repository = parse_repository_url(text)
        except ValueError:
            return ""
        return f"{repository.owner}/{repository.name}"

    def build_install_source_record(
        self,
        plugin: StarMetadata,
        payload: dict[str, Any],
        *,
        fallback_method: str,
        repo_url: str,
        download_url: str,
    ) -> dict[str, Any]:
        """Build a persisted installation source record.

        Args:
            plugin: Loaded plugin metadata after installation.
            payload: Installation request payload.
            fallback_method: Method to use when the request did not declare one.
            repo_url: Repository URL used for installation.
            download_url: Download URL used for installation.

        Returns:
            A serializable installation source record.
        """
        del payload
        return {
            "schema_version": 1,
            "root_dir_name": plugin.root_dir_name,
            "install_method": fallback_method,
            "repo": str(repo_url or plugin.repo or "").strip(),
            "download_url": str(download_url or "").strip(),
            "installed_at": datetime.now(timezone.utc).isoformat(),
        }

    async def persist_plugin_install_source(
        self,
        plugin_info: dict[str, Any] | None,
        payload: dict[str, Any],
        *,
        fallback_method: str,
        repo_url: str,
        download_url: str,
    ) -> None:
        """Persist an installation source record for a newly installed plugin.

        Args:
            plugin_info: Plugin information returned by PluginManager.
            payload: Installation request payload.
            fallback_method: Installation method to persist.
            repo_url: Repository URL used for installation.
            download_url: Download URL used for installation.
        """
        plugin_name = None
        if isinstance(plugin_info, dict):
            plugin_name = str(plugin_info.get("name") or "").strip()
        plugin = self.find_plugin_by_name(plugin_name)
        if not plugin or not plugin.root_dir_name:
            logger.warning(
                "The plugin was installed, but its installation source could not "
                "be recorded because plugin metadata is missing."
            )
            return

        records = await self.get_plugin_install_sources()
        records[plugin.root_dir_name] = self.build_install_source_record(
            plugin,
            payload,
            fallback_method=fallback_method,
            repo_url=repo_url,
            download_url=download_url,
        )
        await self.save_plugin_install_sources(records)

    async def remove_plugin_install_source(self, root_dir_name: str | None) -> None:
        """Remove an installation source record.

        Args:
            root_dir_name: Local plugin root directory name.
        """
        if not root_dir_name:
            return
        records = await self.get_plugin_install_sources()
        if root_dir_name not in records:
            return
        del records[root_dir_name]
        await self.save_plugin_install_sources(records)

    @staticmethod
    def is_ghost_plugin(plugin: StarMetadata) -> bool:
        return not any(
            [
                plugin.name,
                plugin.author,
                plugin.desc,
                plugin.version,
                plugin.display_name,
            ]
        )

    @staticmethod
    def serialize_plugin_base(
        plugin: StarMetadata,
        *,
        logo_url: str | None,
        installed_at: str | None,
        install_source: dict[str, Any] | None,
    ) -> dict:
        install_method = (
            str(install_source.get("install_method") or "").strip().lower()
            if isinstance(install_source, dict)
            else ""
        )
        updates_enabled = install_method == "repository" and not plugin.reserved
        return {
            "name": plugin.name,
            "marketplace_name": (plugin.name or "").replace("_", "-"),
            "repo": "" if plugin.repo is None else str(plugin.repo),
            "author": plugin.author,
            "desc": plugin.desc,
            "version": plugin.version,
            "reserved": plugin.reserved,
            "activated": plugin.activated,
            "online_version": "",
            "display_name": plugin.display_name,
            "logo": logo_url,
            "support_platforms": plugin.support_platforms,
            "astrbot_version": plugin.astrbot_version,
            "installed_at": installed_at,
            "i18n": plugin.i18n,
            "root_dir_name": plugin.root_dir_name,
            "install_source": install_source,
            "updates_enabled": updates_enabled,
            "update_disabled_reason": ""
            if updates_enabled
            else PLUGIN_UPDATE_DISABLED_MESSAGE,
        }

    def get_failed_plugins(self) -> dict:
        return self.plugin_manager.failed_plugin_dict

    async def get_plugin_components_info(
        self,
        plugin: StarMetadata,
        serialize_pages: PluginPagesSerializer,
    ) -> list[dict]:
        components = [
            *await self.get_plugin_page_components(plugin, serialize_pages),
            *self.get_plugin_skill_components(plugin),
            *await self.get_plugin_handler_components(plugin.star_handler_full_names),
        ]
        return sorted(
            components,
            key=lambda item: PLUGIN_COMPONENT_TYPE_ORDER.get(item["type"], 99),
        )

    async def get_plugin_page_components(
        self,
        plugin: StarMetadata,
        serialize_pages: PluginPagesSerializer,
    ) -> list[dict]:
        pages = await serialize_pages(plugin)
        return [
            {
                "type": "page",
                "name": page["title"],
                "title": page["title"],
                "page_name": page["name"],
                "i18n_key": page["i18n_key"],
                "description": page.get("description", ""),
                "plugin_name": plugin.name,
                "plugin_marketplace_name": (plugin.name or "").replace("_", "-"),
            }
            for page in pages
        ]

    async def get_plugin_handler_components(
        self,
        handler_full_names: list[str],
    ) -> list[dict]:
        components = []

        for handler_full_name in handler_full_names:
            info = {}
            handler = star_handlers_registry.star_handlers_map.get(
                handler_full_name,
                None,
            )
            if handler is None:
                continue
            info["event_type"] = handler.event_type.name
            info["event_type_h"] = self.translated_event_type.get(
                handler.event_type,
                handler.event_type.name,
            )
            info["handler_full_name"] = handler.handler_full_name
            info["description"] = handler.desc or "无描述"
            info["handler_name"] = handler.handler_name

            component_type = "hook"
            component: dict[str, Any] | None = None
            if handler.event_type == EventType.AdapterMessageEvent:
                has_admin = False
                for event_filter in handler.event_filters:
                    if isinstance(event_filter, CommandFilter):
                        component_type = "command"
                        info["display_type"] = "指令"
                        info["cmd"] = self._get_command_filter_display_name(
                            event_filter
                        )
                        component = self._build_command_filter_component(
                            event_filter,
                            handler.desc,
                        )
                    elif isinstance(event_filter, CommandGroupFilter):
                        component_type = "command"
                        info["display_type"] = "指令组"
                        info["cmd"] = event_filter.get_complete_command_names()[
                            0
                        ].strip()
                        component = self._build_command_group_component(
                            event_filter,
                            handler.desc,
                        )
                    elif isinstance(event_filter, RegexFilter):
                        component_type = "command"
                        info["display_type"] = "正则匹配"
                        info["cmd"] = event_filter.regex_str
                        component = {
                            "type": "command",
                            "name": event_filter.regex_str,
                            "description": handler.desc or "无描述",
                            "match": "regex",
                        }
                    elif isinstance(event_filter, PermissionTypeFilter):
                        has_admin = True
                info["has_admin"] = has_admin
                if "cmd" not in info:
                    info["cmd"] = "未知"
                if "display_type" not in info:
                    info["display_type"] = "事件监听器"
                    component_type = "listener"
            else:
                info["cmd"] = "自动触发"
                info["display_type"] = "无"
                if handler.event_type == EventType.OnCallingFuncToolEvent:
                    component_type = "llm_tool"

            if component is None:
                component = {
                    "type": component_type,
                    "name": handler.handler_name or handler.event_type.name,
                    "description": handler.desc or "无描述",
                }
            else:
                component["type"] = component_type

            if component_type == "command":
                component["event_type"] = info["event_type"]
                component["event_type_h"] = info["event_type_h"]
                component["handler_name"] = info["handler_name"]
                component["has_admin"] = info.get("has_admin", False)
                if "display_type" in info:
                    component["display_type"] = info["display_type"]
                if "cmd" in info:
                    component["command"] = info["cmd"]
            else:
                component.update(info)
            components.append(component)

        return self._merge_command_components(components)

    def get_plugin_skill_components(self, plugin: StarMetadata) -> list[dict]:
        plugin_names = {
            str(name)
            for name in (plugin.root_dir_name, plugin.name)
            if str(name or "").strip()
        }
        if not plugin_names:
            return []

        try:
            skills = SkillManager().list_skills(
                active_only=False,
                runtime="local",
                show_sandbox_path=False,
            )
        except Exception as exc:
            logger.warning(f"Failed to get skills for plugin {plugin.name}: {exc!s}")
            return []

        components = []
        for skill in skills:
            if skill.source_type != "plugin" or skill.plugin_name not in plugin_names:
                continue
            components.append(
                {
                    "type": "skill",
                    "name": skill.name,
                    "description": skill.description or "无描述",
                    "path": skill.path,
                }
            )
        return components

    @staticmethod
    def _get_command_filter_display_name(command_filter: CommandFilter) -> str:
        return command_filter.get_complete_command_names()[0].strip()

    @staticmethod
    def _get_command_description(
        command_filter: CommandFilter | CommandGroupFilter,
        fallback: str = "",
    ) -> str:
        handler_md = getattr(command_filter, "handler_md", None)
        desc = getattr(handler_md, "desc", "") if handler_md else ""
        return desc or fallback or "无描述"

    def _build_command_filter_component(
        self,
        command_filter: CommandFilter,
        fallback_desc: str = "",
    ) -> dict:
        parts = self._get_command_filter_display_name(command_filter).split()
        if not parts:
            parts = [command_filter.command_name]
        component: dict[str, Any] = {
            "type": "command",
            "name": parts[-1],
            "description": self._get_command_description(
                command_filter,
                fallback_desc,
            ),
        }
        handler_md = getattr(command_filter, "handler_md", None)
        if handler_md:
            component["handler_full_name"] = handler_md.handler_full_name
            component["handler_name"] = handler_md.handler_name
        return self._wrap_command_component(parts[:-1], component)

    def _build_command_group_component(
        self,
        command_group_filter: CommandGroupFilter,
        fallback_desc: str = "",
    ) -> dict:
        parts = command_group_filter.get_complete_command_names()[0].strip().split()
        if not parts:
            parts = [command_group_filter.group_name]
        subcommands = [
            self._build_command_group_child(sub_filter)
            for sub_filter in command_group_filter.sub_command_filters
        ]
        component: dict[str, Any] = {
            "type": "command",
            "name": parts[-1],
            "description": self._get_command_description(
                command_group_filter,
                fallback_desc,
            ),
        }
        if subcommands:
            component["subcommands"] = subcommands
        return self._wrap_command_component(parts[:-1], component)

    def _build_command_group_child(
        self,
        command_filter: CommandFilter | CommandGroupFilter,
    ) -> dict:
        if isinstance(command_filter, CommandGroupFilter):
            component: dict[str, Any] = {
                "name": command_filter.group_name,
                "description": self._get_command_description(command_filter),
            }
            subcommands = [
                self._build_command_group_child(sub_filter)
                for sub_filter in command_filter.sub_command_filters
            ]
            if subcommands:
                component["subcommands"] = subcommands
            return component

        component = {
            "name": command_filter.command_name,
            "description": self._get_command_description(command_filter),
        }
        handler_md = getattr(command_filter, "handler_md", None)
        if handler_md:
            component["handler_full_name"] = handler_md.handler_full_name
            component["handler_name"] = handler_md.handler_name
        return component

    @staticmethod
    def _wrap_command_component(parent_names: list[str], component: dict) -> dict:
        for parent_name in reversed(parent_names):
            component = {
                "type": "command",
                "name": parent_name,
                "description": "无描述",
                "subcommands": [component],
            }
        return component

    def _merge_command_components(self, components: list[dict]) -> list[dict]:
        merged: list[dict] = []
        for component in components:
            if component.get("type") != "command":
                merged.append(component)
                continue
            existing = next(
                (
                    item
                    for item in merged
                    if item.get("type") == "command"
                    and item.get("name") == component.get("name")
                    and item.get("match") == component.get("match")
                ),
                None,
            )
            if existing is None:
                merged.append(component)
                continue
            self._merge_command_component(existing, component)
        return merged

    def _merge_command_component(self, target: dict, source: dict) -> None:
        if target.get("description") == "无描述" and source.get("description"):
            target["description"] = source["description"]
        for key, value in source.items():
            if key in {"subcommands", "description"}:
                continue
            target.setdefault(key, value)

        source_subcommands = source.get("subcommands")
        if not isinstance(source_subcommands, list):
            return
        target_subcommands = target.setdefault("subcommands", [])
        for source_subcommand in source_subcommands:
            if not isinstance(source_subcommand, dict):
                continue
            existing = next(
                (
                    item
                    for item in target_subcommands
                    if isinstance(item, dict)
                    and item.get("name") == source_subcommand.get("name")
                ),
                None,
            )
            if existing is None:
                target_subcommands.append(source_subcommand)
                continue
            self._merge_command_component(existing, source_subcommand)

    async def resolve_plugin_update_info(self, plugin_name: str) -> dict[str, Any]:
        """Resolve update information from the recorded repository source.

        Args:
            plugin_name: Installed plugin metadata name.

        Returns:
            Update information containing the recorded repository URL.

        Raises:
            PluginServiceError: If the plugin cannot be updated from a repository.
        """
        plugin = self.find_plugin_by_name(plugin_name)
        if not plugin:
            raise PluginServiceError("插件不存在")
        records = await self.get_plugin_install_sources()
        record = self.resolve_plugin_install_source(plugin, records)
        if not isinstance(record, dict) or record.get("implicit"):
            raise PluginServiceError(
                PLUGIN_UPDATE_SOURCE_REQUIRED_MESSAGE,
                public_message=PLUGIN_UPDATE_SOURCE_REQUIRED_MESSAGE,
            )

        repo_url = str(record.get("repo") or plugin.repo or "").strip()
        if not repo_url:
            raise PluginServiceError(
                PLUGIN_UPDATE_DISABLED_MESSAGE,
                public_message=PLUGIN_UPDATE_DISABLED_MESSAGE,
            )
        return {"repo": repo_url}

    async def refresh_plugin_install_source_after_update(
        self,
        plugin_name: str,
    ) -> None:
        """Refresh the recorded install source timestamp after an update.

        Args:
            plugin_name: Installed plugin metadata name.
        """
        plugin = self.find_plugin_by_name(plugin_name)
        if not plugin or not plugin.root_dir_name:
            return
        records = await self.get_plugin_install_sources()
        record = records.get(plugin.root_dir_name)
        if not isinstance(record, dict):
            return
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        records[plugin.root_dir_name] = record
        await self.save_plugin_install_sources(records)

    async def install_plugin(self, data: object) -> tuple[dict, str]:
        self._ensure_not_demo()
        payload = data if isinstance(data, dict) else {}
        repo_url = str(payload.get("url") or "").strip()
        download_url = str(payload.get("download_url") or "").strip()
        ignore_version_check = bool(payload.get("ignore_version_check", False))
        if not repo_url:
            raise PluginServiceError("缺少插件仓库地址")

        try:
            repo_url = normalize_repository_url(repo_url)
            repository = parse_repository_url(repo_url)
        except ValueError as exc:
            raise PluginServiceError(
                str(exc),
                public_message="请输入有效的 Git 仓库地址。",
            ) from exc

        expected_transport = str(payload.get("repository_transport") or "").strip()
        if expected_transport == "github" and (
            repository.provider != "github" or repository.transport != "archive"
        ):
            raise PluginServiceError(
                "GitHub archive endpoint received a non-GitHub repository.",
                public_message="请输入有效的 GitHub HTTP(S) 仓库地址。",
            )
        if expected_transport == "git" and repository.transport != "git":
            raise PluginServiceError(
                "Git clone endpoint received a GitHub archive repository.",
                public_message="该地址应通过 GitHub 仓库安装入口处理。",
            )

        proxy: str | None = payload.get("proxy", None)
        if proxy:
            proxy = proxy.removesuffix("/")

        try:
            logger.info(f"Installing plugin {repo_url}")
            plugin_info = await self.plugin_manager.install_plugin(
                repo_url,
                proxy or "",
                ignore_version_check=ignore_version_check,
                download_url=download_url,
            )
            await self.persist_plugin_install_source(
                plugin_info,
                payload,
                fallback_method="repository"
                if self.repo_identifier_from_url(repo_url)
                else "url",
                repo_url=repo_url,
                download_url=download_url,
            )
            await self.sync_skills_after_plugin_change()
            logger.info(f"Installed plugin {repo_url} successfully.")
            return plugin_info or {}, "安装成功。"
        except PluginVersionUnsupportedError as exc:
            raise PluginServiceWarning(
                str(exc),
                {
                    "warning_type": "astrbot_version_unsupported",
                    "can_ignore": True,
                },
                public_message="当前 LKMBot 版本不满足插件要求",
            ) from exc
        except GitUnavailableError as exc:
            raise PluginServiceError(str(exc), public_message=str(exc)) from exc

    async def validate_plugin_repo(self, data: object) -> tuple[dict[str, Any], str]:
        """Validate whether a repository contains LKMBot plugin metadata.

        Args:
            data: Dashboard request payload containing repository or url.

        Returns:
            Plugin metadata fetched from the repository and a success message.

        Raises:
            PluginServiceError: If the repository is not a valid LKMBot plugin.
        """
        payload = self._payload(data)
        repo_url = str(payload.get("url") or payload.get("repository") or "").strip()
        if not repo_url:
            raise PluginServiceError("缺少插件仓库地址")
        try:
            repo_url = normalize_repository_url(repo_url)
        except ValueError as exc:
            raise PluginServiceError(
                str(exc),
                public_message="请输入有效的 Git 仓库地址。",
            ) from exc

        proxy = str(payload.get("proxy") or "").strip()
        try:
            metadata = await self.plugin_manager.inspect_plugin_repository(
                repo_url,
                proxy,
            )
            return metadata, "插件校验通过。"
        except ValueError as exc:
            raise PluginServiceError(
                str(exc),
                public_message=str(exc),
            ) from exc
        except GitUnavailableError as exc:
            raise PluginServiceError(str(exc), public_message=str(exc)) from exc
        except Exception as exc:
            logger.warning(
                "Plugin repository validation failed for %s: %s", repo_url, exc
            )
            raise PluginServiceError(
                "插件校验失败",
                public_message="插件校验失败，请查看服务端日志。",
            ) from exc

    async def install_plugin_upload(
        self,
        *,
        upload_file,
        ignore_version_check: bool,
    ) -> tuple[dict, str]:
        self._ensure_not_demo()
        logger.info(f"Installing uploaded plugin {upload_file.filename}")
        filename = str(upload_file.filename or "plugin.zip").replace("\\", "/")
        file_path = os.path.join(
            get_bot_temp_path(),
            f"plugin_upload_{os.path.basename(filename) or 'plugin.zip'}",
        )
        await upload_file.save(file_path)
        try:
            plugin_info = await self.plugin_manager.install_plugin_from_file(
                file_path,
                ignore_version_check=ignore_version_check,
            )
            await self.persist_plugin_install_source(
                plugin_info,
                {},
                fallback_method="upload",
                repo_url="",
                download_url="",
            )
            await self.sync_skills_after_plugin_change()
            logger.info(f"Installed plugin {upload_file.filename} successfully.")
            return plugin_info or {}, "安装成功。"
        except PluginVersionUnsupportedError as exc:
            raise PluginServiceWarning(
                str(exc),
                {
                    "warning_type": "astrbot_version_unsupported",
                    "can_ignore": True,
                },
                public_message="当前 LKMBot 版本不满足插件要求",
            ) from exc

    async def install_plugin_upload_from_dashboard_form(
        self,
        *,
        upload_file,
        ignore_version_check,
    ) -> tuple[dict, str]:
        return await self.install_plugin_upload(
            upload_file=upload_file,
            ignore_version_check=self._to_bool(ignore_version_check),
        )

    async def uninstall_plugin(self, data: object) -> tuple[None, str]:
        self._ensure_not_demo()
        payload = data if isinstance(data, dict) else {}
        plugin_name = payload["name"]
        delete_config = payload.get("delete_config", False)
        delete_data = payload.get("delete_data", False)
        logger.info(f"Uninstalling plugin {plugin_name}")
        plugin = self.find_plugin_by_name(plugin_name)
        root_dir_name = plugin.root_dir_name if plugin else None
        await self.plugin_manager.uninstall_plugin(
            plugin_name,
            delete_config=delete_config,
            delete_data=delete_data,
        )
        await self.remove_plugin_install_source(root_dir_name)
        await self.sync_skills_after_plugin_change()
        logger.info(f"Uninstalled plugin {plugin_name} successfully.")
        return None, "卸载成功"

    async def uninstall_failed_plugin(self, data: object) -> tuple[None, str]:
        self._ensure_not_demo()
        payload = data if isinstance(data, dict) else {}
        dir_name = payload.get("dir_name", "")
        delete_config = payload.get("delete_config", False)
        delete_data = payload.get("delete_data", False)
        if not dir_name:
            raise PluginServiceError("缺少失败插件目录名")

        logger.info(f"Uninstalling failed plugin {dir_name}")
        await self.plugin_manager.uninstall_failed_plugin(
            dir_name,
            delete_config=delete_config,
            delete_data=delete_data,
        )
        await self.sync_skills_after_plugin_change()
        logger.info(f"Uninstalled failed plugin {dir_name} successfully.")
        return None, "卸载成功"

    async def update_plugin(self, data: object) -> tuple[None, str]:
        self._ensure_not_demo()
        payload = data if isinstance(data, dict) else {}
        plugin_name = payload["name"]
        proxy: str | None = payload.get("proxy", None)
        update_info = await self.resolve_plugin_update_info(plugin_name)
        download_url = str(update_info.get("download_url") or "").strip()
        repo_url = str(update_info.get("repo") or "").strip()
        logger.info(f"Updating plugin {plugin_name}")
        await self.plugin_manager.update_plugin(
            plugin_name,
            proxy or "",
            download_url=download_url,
            repo_url=repo_url,
        )
        await self.refresh_plugin_install_source_after_update(plugin_name)
        await self.plugin_manager.reload(plugin_name)
        await self.sync_skills_after_plugin_change()
        logger.info(f"Updated plugin {plugin_name} successfully.")
        return None, "更新成功。"

    async def update_all_plugins(self, data: object) -> tuple[dict, str]:
        self._ensure_not_demo()
        payload = data if isinstance(data, dict) else {}
        plugin_names: list[str] = payload.get("names") or []
        proxy: str = payload.get("proxy", "")

        if not isinstance(plugin_names, list) or not plugin_names:
            raise PluginServiceError("插件列表不能为空")

        results = []
        sem = asyncio.Semaphore(PLUGIN_UPDATE_CONCURRENCY)

        async def _update_one(name: str):
            async with sem:
                try:
                    logger.info(f"Updating plugin {name} as part of a batch update.")
                    update_info = await self.resolve_plugin_update_info(name)
                    download_url = str(update_info.get("download_url") or "").strip()
                    repo_url = str(update_info.get("repo") or "").strip()
                    await self.plugin_manager.update_plugin(
                        name,
                        proxy,
                        download_url=download_url,
                        repo_url=repo_url,
                    )
                    await self.refresh_plugin_install_source_after_update(name)
                    return {"name": name, "status": "ok", "message": "更新成功"}
                except PluginServiceError as exc:
                    logger.error(
                        f"/api/plugin/update-all: Failed to update plugin {name}: {exc}",
                    )
                    return {
                        "name": name,
                        "status": "error",
                        "message": exc.public_message,
                    }
                except Exception:
                    logger.error(
                        f"/api/plugin/update-all: Failed to update plugin {name}",
                        exc_info=True,
                    )
                    return {
                        "name": name,
                        "status": "error",
                        "message": PLUGIN_UPDATE_FAILED_MESSAGE,
                    }

        raw_results = await asyncio.gather(
            *(_update_one(name) for name in plugin_names),
            return_exceptions=True,
        )
        for name, result in zip(plugin_names, raw_results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                logger.error(
                    f"/api/plugin/update-all: Update task for plugin {name} failed: "
                    f"{result!r}"
                )
                results.append(
                    {
                        "name": name,
                        "status": "error",
                        "message": PLUGIN_UPDATE_FAILED_MESSAGE,
                    }
                )
            else:
                results.append(result)

        failed = [result for result in results if result["status"] == "error"]
        if len(failed) < len(results):
            await self.sync_skills_after_plugin_change()
        message = (
            "批量更新完成，全部成功。"
            if not failed
            else f"批量更新完成，其中 {len(failed)}/{len(results)} 个插件失败。"
        )

        return {"results": results}, message

    async def set_plugin_enabled(
        self, data: object, *, enabled: bool
    ) -> tuple[None, str]:
        self._ensure_not_demo()
        payload = data if isinstance(data, dict) else {}
        plugin_name = payload["name"]
        if enabled:
            await self.plugin_manager.turn_on_plugin(plugin_name)
            message = "启用成功。"
            log_action = "Enabled"
        else:
            await self.plugin_manager.turn_off_plugin(plugin_name)
            message = "停用成功。"
            log_action = "Disabled"
        await self.sync_skills_after_plugin_change()
        logger.info(f"{log_action} plugin {plugin_name}.")
        return None, message

    def resolve_plugin_dir(self, plugin_name: str) -> Path:
        if not plugin_name:
            raise PluginServiceError("插件名称不能为空")

        plugin_obj = None
        for plugin in self.plugin_manager.context.get_all_stars():
            if plugin.name == plugin_name:
                plugin_obj = plugin
                break

        if not plugin_obj:
            raise PluginServiceError(f"插件 {plugin_name} 不存在")
        if not plugin_obj.root_dir_name:
            raise PluginServiceError(f"插件 {plugin_name} 目录不存在")

        plugin_dir = (
            Path(
                self.plugin_manager.reserved_plugin_path
                if plugin_obj.reserved
                else self.plugin_manager.plugin_store_path
            )
            / plugin_obj.root_dir_name
        )
        if not plugin_dir.is_dir():
            raise PluginServiceError(f"无法找到插件 {plugin_name} 的目录")
        return plugin_dir

    def get_plugin_readme(self, plugin_name: str | None) -> tuple[dict, str]:
        if not plugin_name:
            logger.warning("The plugin name is empty.")
            raise PluginServiceError("插件名称不能为空")

        plugin_dir = self.resolve_plugin_dir(plugin_name)
        readme_path = plugin_dir / "README.md"

        if not readme_path.is_file():
            logger.warning(f"Plugin {plugin_name} has no README file.")
            raise PluginServiceError(f"插件 {plugin_name} 没有README文件")

        try:
            return {
                "content": readme_path.read_text(encoding="utf-8")
            }, "成功获取README内容"
        except Exception as exc:
            logger.warning(f"Failed to read README for plugin {plugin_name}: {exc}")
            raise PluginServiceError(
                "读取README文件失败",
                public_message="读取README文件失败",
            ) from exc

    def get_plugin_readme_from_dashboard_query(
        self,
        plugin_name: str | None,
    ) -> tuple[dict, str]:
        return self.get_plugin_readme(plugin_name)

    def get_plugin_changelog(self, plugin_name: str | None) -> tuple[dict, str]:
        logger.debug(f"Getting the changelog for plugin {plugin_name}")
        if not plugin_name:
            logger.warning("The plugin name is empty.")
            raise PluginServiceError("插件名称不能为空")

        plugin_dir = self.resolve_plugin_dir(plugin_name)
        changelog_names = ["CHANGELOG.md", "changelog.md", "CHANGELOG", "changelog"]
        for name in changelog_names:
            changelog_path = plugin_dir / name
            if not changelog_path.is_file():
                continue
            try:
                return (
                    {"content": changelog_path.read_text(encoding="utf-8")},
                    "成功获取更新日志",
                )
            except Exception as exc:
                logger.warning(
                    f"Failed to read the changelog for plugin {plugin_name}: {exc}"
                )
                raise PluginServiceError(
                    "读取更新日志失败",
                    public_message="读取更新日志失败",
                ) from exc

        logger.warning(f"Plugin {plugin_name} has no changelog file.")
        return {"content": None}, "该插件没有更新日志文件"

    def get_plugin_changelog_from_dashboard_query(
        self,
        plugin_name: str | None,
    ) -> tuple[dict, str]:
        return self.get_plugin_changelog(plugin_name)

    @staticmethod
    def _to_bool(value: object, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)


__all__ = [
    "PLUGIN_UPDATE_CONCURRENCY",
    "PLUGIN_OPERATION_FAILED_MESSAGE",
    "PLUGIN_UPDATE_DISABLED_MESSAGE",
    "PLUGIN_UPDATE_FAILED_MESSAGE",
    "PLUGIN_UPDATE_SOURCE_REQUIRED_MESSAGE",
    "PluginService",
    "PluginServiceError",
    "PluginServiceWarning",
]
