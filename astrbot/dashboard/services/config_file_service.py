"""ConfigFileService operations for the Dashboard configuration API."""

from __future__ import annotations

import os
from pathlib import Path

from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.core.star.star import star_registry
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
from astrbot.core.utils.upload import UploadTooLargeError
from astrbot.dashboard.services import config_service


class ConfigFileService:
    def __init__(self, core_lifecycle: AstrBotCoreLifecycle) -> None:
        self.core_lifecycle = core_lifecycle

    def get_plugin_metadata_by_name(self, plugin_name: str):
        for plugin_md in star_registry:
            if plugin_md.name == plugin_name:
                return plugin_md
        return None

    def resolve_config_file_scope(
        self,
        *,
        scope: str | None,
        name: str | None,
        key_path: str | None,
    ):
        scope = scope or "plugin"
        if scope != "plugin":
            raise ValueError(f"Unsupported scope: {scope}")
        if not name or not key_path:
            raise ValueError("Missing name or key parameter")

        metadata = self.get_plugin_metadata_by_name(name)
        if not metadata or not metadata.config:
            raise ValueError(f"Plugin {name} not found or has no config")

        return scope, name, key_path, metadata, metadata.config

    async def save_plugin_configs(
        self,
        post_configs: dict,
        plugin_name: str,
    ) -> None:
        metadata = self.get_plugin_metadata_by_name(plugin_name)
        if not metadata:
            raise ValueError(f"插件 {plugin_name} 不存在")
        if not metadata.config:
            raise ValueError(f"插件 {plugin_name} 没有注册配置")

        errors, post_configs = config_service.validate_config(
            post_configs,
            getattr(metadata.config, "schema", {}),
            is_core=False,
        )
        if errors:
            raise ValueError(f"格式校验未通过: {errors}")
        metadata.config.save_config(post_configs)
        await self.core_lifecycle.plugin_manager.reload(plugin_name)

    async def save_plugin_configs_from_dashboard_payload(
        self,
        payload: object,
        *,
        plugin_name: str,
    ) -> str:
        post_configs = payload if isinstance(payload, dict) else {}
        await self.save_plugin_configs(post_configs, plugin_name)
        return f"保存插件 {plugin_name} 成功~ 机器人正在热重载插件。"

    async def upload_config_file(
        self,
        *,
        scope: str | None,
        name: str | None,
        key_path: str | None,
        files,
    ) -> dict:
        _, name, key_path, _, config = self.resolve_config_file_scope(
            scope=scope,
            name=name,
            key_path=key_path,
        )
        meta = config_service._get_schema_item(
            getattr(config, "schema", None), key_path
        )
        if not meta or meta.get("type") != "file":
            raise ValueError("Config item not found or not file type")
        if not files:
            raise ValueError("No files uploaded")

        allowed_exts = self._allowed_file_extensions(meta)
        plugin_root_path = self._plugin_root_path(name)
        plugin_root_path.mkdir(parents=True, exist_ok=True)

        uploaded: list[str] = []
        errors: list[str] = []
        folder = config_service._config_key_to_folder(key_path)

        for file in files.values():
            filename = config_service._sanitize_filename(file.filename or "")
            if not filename:
                errors.append("Invalid filename")
                continue

            file_size = getattr(file, "content_length", None)
            if isinstance(file_size, int) and file_size > config_service.MAX_FILE_BYTES:
                errors.append(f"File too large: {filename}")
                continue

            ext = os.path.splitext(filename)[1].lstrip(".").lower()
            if allowed_exts and ext not in allowed_exts:
                errors.append(f"Unsupported file type: {filename}")
                continue

            rel_path = f"files/{folder}/{filename}"
            save_path = self._safe_plugin_path(plugin_root_path, rel_path)
            if save_path is None:
                errors.append(f"Invalid path: {filename}")
                continue

            save_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                await file.save(str(save_path), max_bytes=config_service.MAX_FILE_BYTES)
            except UploadTooLargeError:
                errors.append(f"File too large: {filename}")
                continue
            uploaded.append(rel_path)

        if not uploaded:
            raise ValueError(
                "Upload failed: " + ", ".join(errors) if errors else "Upload failed"
            )

        return {"uploaded": uploaded, "errors": errors}

    async def upload_config_file_from_dashboard_request(self, args, files) -> dict:
        return await self.upload_config_file(
            scope=args.get("scope"),
            name=args.get("name"),
            key_path=args.get("key"),
            files=files,
        )

    def delete_config_file(
        self,
        *,
        scope: str | None,
        name: str | None,
        rel_path: str | None,
    ) -> None:
        if not name:
            raise ValueError("Missing name parameter")
        if (scope or "plugin") != "plugin":
            raise ValueError(f"Unsupported scope: {scope}")

        rel_path = config_service._normalize_rel_path(rel_path)
        if not rel_path or not rel_path.startswith("files/"):
            raise ValueError("Invalid path parameter")

        metadata = self.get_plugin_metadata_by_name(name)
        if not metadata:
            raise ValueError(f"Plugin {name} not found")

        plugin_root_path = self._plugin_root_path(name)
        target_path = self._safe_plugin_path(plugin_root_path, rel_path)
        if target_path is None:
            raise ValueError("Invalid path parameter")
        if target_path.is_file():
            target_path.unlink()

    def delete_config_file_from_dashboard_payload(
        self,
        *,
        scope: str | None,
        name: str | None,
        payload: object,
    ) -> str:
        data = payload if isinstance(payload, dict) else {}
        self.delete_config_file(
            scope=scope,
            name=name,
            rel_path=data.get("path"),
        )
        return "Deleted"

    def delete_config_file_from_dashboard_request(
        self,
        args,
        payload: object,
    ) -> str:
        return self.delete_config_file_from_dashboard_payload(
            scope=args.get("scope") or "plugin",
            name=args.get("name"),
            payload=payload,
        )

    def list_config_files(
        self,
        *,
        scope: str | None,
        name: str | None,
        key_path: str | None,
    ) -> dict:
        _, name, key_path, _, config = self.resolve_config_file_scope(
            scope=scope,
            name=name,
            key_path=key_path,
        )
        meta = config_service._get_schema_item(
            getattr(config, "schema", None), key_path
        )
        if not meta or meta.get("type") != "file":
            raise ValueError("Config item not found or not file type")

        plugin_root_path = self._plugin_root_path(name)
        target_dir = self._safe_plugin_path(
            plugin_root_path,
            f"files/{config_service._config_key_to_folder(key_path)}",
        )
        if target_dir is None:
            raise ValueError("Invalid path parameter")
        if not target_dir.exists() or not target_dir.is_dir():
            return {"files": []}

        files: list[str] = []
        for path in target_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                rel_path = path.relative_to(plugin_root_path).as_posix()
            except ValueError:
                continue
            if rel_path.startswith("files/"):
                files.append(rel_path)
        return {"files": files}

    def list_config_files_from_dashboard_args(self, args) -> dict:
        return self.list_config_files(
            scope=args.get("scope"),
            name=args.get("name"),
            key_path=args.get("key"),
        )

    @staticmethod
    def _allowed_file_extensions(meta: dict) -> list[str]:
        file_types = meta.get("file_types")
        if not isinstance(file_types, list):
            return []
        return [str(ext).lstrip(".").lower() for ext in file_types if str(ext).strip()]

    @staticmethod
    def _plugin_root_path(name: str) -> Path:
        storage_root_path = Path(get_astrbot_plugin_data_path()).resolve(strict=False)
        plugin_root_path = (storage_root_path / name).resolve(strict=False)
        try:
            plugin_root_path.relative_to(storage_root_path)
        except ValueError as exc:
            raise ValueError("Invalid name parameter") from exc
        return plugin_root_path

    @staticmethod
    def _safe_plugin_path(plugin_root_path: Path, rel_path: str) -> Path | None:
        target_path = (plugin_root_path / rel_path).resolve(strict=False)
        try:
            target_path.relative_to(plugin_root_path)
        except ValueError:
            return None
        return target_path
