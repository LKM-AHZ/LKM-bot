"""Shared configuration validation, security checks and persistence."""

from __future__ import annotations

import os
import traceback
from typing import Any

from bot.core import logger
from bot.core.config.agent_runner import normalize_agent_runner
from bot.core.config.bot_config import BotConfig
from bot.core.config.default import (
    CONFIG_METADATA_2,
    DEFAULT_VALUE_MAP,
    get_local_permission_defaults,
)

PROTECTED_2FA_CONFIG_PATHS = (
    ("dashboard", "totp", "enable"),
    ("dashboard", "totp", "secret"),
    ("dashboard", "totp", "recovery_code_hash"),
)
MAX_FILE_BYTES = 500 * 1024 * 1024


def try_cast(value: Any, type_: str):
    if type_ == "int":
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    elif (
        type_ == "float"
        and isinstance(value, str)
        and value.replace(".", "", 1).isdigit()
    ) or (type_ == "float" and isinstance(value, int)):
        return float(value)
    elif type_ == "float":
        try:
            return float(value)
        except (ValueError, TypeError):
            return None


def _expect_type(value, expected_type, path_key, errors, expected_name=None) -> bool:
    if not isinstance(value, expected_type):
        errors.append(
            f"错误的类型 {path_key}: 期望是 {expected_name or expected_type.__name__}, "
            f"得到了 {type(value).__name__}"
        )
        return False
    return True


def _validate_template_list(value, meta, path_key, errors, validate_fn) -> None:
    if not _expect_type(value, list, path_key, errors, "list"):
        return

    templates = meta.get("templates")
    if not isinstance(templates, dict):
        templates = {}

    for idx, item in enumerate(value):
        item_path = f"{path_key}[{idx}]"
        if not _expect_type(item, dict, item_path, errors, "dict"):
            continue

        template_key = item.get("__template_key") or item.get("template")
        if not template_key:
            errors.append(f"缺少模板选择 {item_path}: 需要 __template_key")
            continue

        template_meta = templates.get(template_key)
        if not template_meta:
            errors.append(f"未知模板 {item_path}: {template_key}")
            continue

        validate_fn(
            item,
            template_meta.get("items", {}),
            path=f"{path_key}.templates.{template_key}.",
        )


def sanitize_path_segment(segment: str) -> str:
    cleaned = []
    for ch in segment:
        if (
            ("a" <= ch <= "z")
            or ("A" <= ch <= "Z")
            or ch.isdigit()
            or ch
            in {
                "-",
                "_",
            }
        ):
            cleaned.append(ch)
        else:
            cleaned.append("_")
    result = "".join(cleaned).strip("_")
    return result or "_"


def _config_key_to_folder(key_path: str) -> str:
    parts = [sanitize_path_segment(part) for part in key_path.split(".") if part]
    return "/".join(parts) if parts else "_"


def config_key_to_folder(key_path: str) -> str:
    return _config_key_to_folder(key_path)


def _normalize_rel_path(path: object) -> str | None:
    if not isinstance(path, str):
        return None
    rel = path.replace("\\", "/").lstrip("/")
    if not rel:
        return None
    parts = [part for part in rel.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        return None
    if rel.startswith("../") or "/../" in rel:
        return None
    return "/".join(parts)


def normalize_rel_path(path: str | None) -> str | None:
    return _normalize_rel_path(path)


def _get_schema_item(schema: dict | None, key_path: str) -> dict | None:
    if not isinstance(schema, dict) or not key_path:
        return None
    if key_path in schema:
        return schema.get(key_path)

    parts = key_path.split(".")
    current = schema
    idx = 0
    while idx < len(parts):
        part = parts[idx]
        if part not in current:
            return None
        meta = current.get(part)
        if idx == len(parts) - 1:
            return meta
        if not isinstance(meta, dict) or meta.get("type") != "object":
            if not isinstance(meta, dict) or meta.get("type") != "template_list":
                return None
            if idx + 2 >= len(parts) or parts[idx + 1] != "templates":
                return None
            template_meta = meta.get("templates", {}).get(parts[idx + 2])
            if not isinstance(template_meta, dict):
                return None
            if idx + 2 == len(parts) - 1:
                return template_meta
            current = template_meta.get("items", {})
            idx += 3
            continue
        current = meta.get("items", {})
        idx += 1
    return None


def get_schema_item(schema: dict | None, key_path: str) -> dict | None:
    return _get_schema_item(schema, key_path)


def _sanitize_filename(name: str) -> str:
    cleaned = os.path.basename(name).strip()
    if not cleaned or cleaned in {".", ".."}:
        return ""
    for sep in (os.sep, os.altsep):
        if sep:
            cleaned = cleaned.replace(sep, "_")
    return cleaned


def sanitize_filename(name: str) -> str:
    return _sanitize_filename(name)


def validate_config(
    data,
    schema: dict,
    is_core: bool,
    *,
    runtime: dict | None = None,
) -> tuple[list[str], dict]:
    """Validate configuration values and normalize linked Local permissions.

    Args:
        data: Submitted configuration, normalized in place.
        schema: Configuration metadata used for validation.
        is_core: Whether this is a core configuration rather than a plugin.
        runtime: Startup runtime snapshot for platform-specific defaults.

    Returns:
        Validation errors and the normalized configuration.
    """
    errors = []

    def validate(data: dict, metadata: dict = schema, path="") -> None:
        for key, value in data.items():
            if key not in metadata:
                continue
            meta = metadata[key]
            if (
                is_core
                and path == "provider."
                and key == "key"
                and data.get("type") == "system_one_decision"
            ):
                # System One uses a single key within the shared provider schema.
                meta = dict(meta)
                meta["type"] = "string"
            if "type" not in meta:
                logger.debug(f"配置项 {path}{key} 没有类型定义, 跳过校验")
                continue
            if value is None:
                data[key] = DEFAULT_VALUE_MAP[meta["type"]]
                continue

            if meta["type"] == "template_list":
                _validate_template_list(value, meta, f"{path}{key}", errors, validate)
                continue

            if meta["type"] == "file":
                if not _expect_type(value, list, f"{path}{key}", errors, "list"):
                    continue
                for idx, item in enumerate(value):
                    if not isinstance(item, str):
                        errors.append(
                            f"Invalid type {path}{key}[{idx}]: expected string, got {type(item).__name__}",
                        )
                        continue
                    normalized = _normalize_rel_path(item)
                    if not normalized or not normalized.startswith("files/"):
                        errors.append(
                            f"Invalid file path {path}{key}[{idx}]: {item}",
                        )
                        continue
                    key_path = f"{path}{key}"
                    expected_folder = _config_key_to_folder(key_path)
                    expected_prefix = f"files/{expected_folder}/"
                    if not normalized.startswith(expected_prefix):
                        errors.append(
                            f"Invalid file path {path}{key}[{idx}]: {item}",
                        )
                        continue
                    value[idx] = normalized
                continue

            if meta["type"] == "list" and not isinstance(value, list):
                errors.append(
                    f"错误的类型 {path}{key}: 期望是 list, 得到了 {type(value).__name__}",
                )
            elif (
                meta["type"] == "list"
                and isinstance(value, list)
                and value
                and "items" in meta
                and isinstance(value[0], dict)
            ):
                for item in value:
                    validate(item, meta["items"], path=f"{path}{key}.")
            elif meta["type"] == "object" and isinstance(value, dict):
                validate(value, meta["items"], path=f"{path}{key}.")

            if meta["type"] == "int" and not isinstance(value, int):
                casted = try_cast(value, "int")
                if casted is None:
                    errors.append(
                        f"错误的类型 {path}{key}: 期望是 int, 得到了 {type(value).__name__}",
                    )
                data[key] = casted
            elif meta["type"] == "float" and not isinstance(value, float):
                casted = try_cast(value, "float")
                if casted is None:
                    errors.append(
                        f"错误的类型 {path}{key}: 期望是 float, 得到了 {type(value).__name__}",
                    )
                data[key] = casted
            elif meta["type"] == "bool" and not isinstance(value, bool):
                errors.append(
                    f"错误的类型 {path}{key}: 期望是 bool, 得到了 {type(value).__name__}",
                )
            elif meta["type"] in ["string", "text"] and not isinstance(value, str):
                errors.append(
                    f"错误的类型 {path}{key}: 期望是 string, 得到了 {type(value).__name__}",
                )
            elif meta["type"] == "list" and not isinstance(value, list):
                errors.append(
                    f"错误的类型 {path}{key}: 期望是 list, 得到了 {type(value).__name__}",
                )
            elif meta["type"] == "object" and not isinstance(value, dict):
                errors.append(
                    f"错误的类型 {path}{key}: 期望是 dict, 得到了 {type(value).__name__}",
                )

    if is_core:
        meta_all = {
            **schema["platform_group"]["metadata"],
            **schema["provider_group"]["metadata"],
            **schema["misc_config_group"]["metadata"],
        }
        validate(data, meta_all)
        provider_settings = data.get("provider_settings", {})
        defaults = get_local_permission_defaults(runtime.get("os") if runtime else None)
        permissions = (
            provider_settings.get("computer_use_local_permissions", {})
            if isinstance(provider_settings, dict)
            else {}
        )
        if not isinstance(permissions, dict):
            errors.append("Local computer permissions must be an object.")
        else:
            for role in ("member", "admin"):
                if role not in permissions:
                    continue
                policy = permissions[role]
                if not isinstance(policy, dict):
                    errors.append(
                        f"Local computer permissions for {role} must be an object."
                    )
                    continue
                for key in ("allow_execution", "allow_network"):
                    if key in policy and not isinstance(policy[key], bool):
                        errors.append(
                            f"Local permission {role}.{key} must be a boolean."
                        )
                scope = policy.get(
                    "filesystem_scope", defaults[role]["filesystem_scope"]
                )
                if scope not in ("none", "workspace", "host"):
                    errors.append(
                        f"Invalid local filesystem scope for {role}: {scope}."
                    )
                if scope == "none":
                    policy["allow_execution"] = False
                    policy["allow_network"] = False
                elif (
                    policy.get("allow_execution", defaults[role]["allow_execution"])
                    is False
                ):
                    policy["allow_network"] = False
                if (
                    runtime is not None
                    and runtime["os"] == "windows"
                    and scope == "host"
                    and policy.get("allow_execution", defaults[role]["allow_execution"])
                    and not policy.get("allow_network", defaults[role]["allow_network"])
                ):
                    errors.append(
                        f"Local permission {role}: Windows Local isolation supports workspace scope only; "
                        "host access with networking disabled is unavailable."
                    )

    else:
        validate(data, schema)

    return errors, data


def _log_computer_config_changes(
    old_config: dict,
    new_config: dict,
    *,
    log_info=None,
) -> None:
    log_info = log_info or logger.info
    old_ps = old_config.get("provider_settings", {})
    new_ps = new_config.get("provider_settings", {})

    old_runtime = old_ps.get("computer_use_runtime", "none")
    new_runtime = new_ps.get("computer_use_runtime", "none")
    if old_runtime != new_runtime:
        log_info(
            "[Computer] Config changed: computer_use_runtime %s -> %s",
            old_runtime,
            new_runtime,
        )

    old_permissions = old_ps.get("computer_use_local_permissions", {})
    new_permissions = new_ps.get("computer_use_local_permissions", {})
    for role in ("member", "admin"):
        old_role = old_permissions.get(role, {})
        new_role = new_permissions.get(role, {})
        for key in ("allow_execution", "allow_network", "filesystem_scope"):
            old_value = old_role.get(key)
            new_value = new_role.get(key)
            if old_value != new_value:
                log_info(
                    "[Computer] Config changed: local_permissions.%s.%s %s -> %s",
                    role,
                    key,
                    old_value,
                    new_value,
                )

    old_sandbox = old_ps.get("sandbox", {})
    new_sandbox = new_ps.get("sandbox", {})
    all_keys = set(old_sandbox.keys()) | set(new_sandbox.keys())
    for key in sorted(all_keys):
        old_val = old_sandbox.get(key)
        new_val = new_sandbox.get(key)
        if old_val == new_val:
            continue
        if "token" in key or "secret" in key:
            old_display = "***" if old_val else "(empty)"
            new_display = "***" if new_val else "(empty)"
        else:
            old_display = old_val
            new_display = new_val
        log_info(
            "[Computer] Config changed: sandbox.%s %s -> %s",
            key,
            old_display,
            new_display,
        )


def _get_nested_value(data: dict, path: tuple[str, ...]) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _set_nested_value(data: dict, path: tuple[str, ...], value: Any) -> None:
    current = data
    for key in path[:-1]:
        next_value = current.get(key)
        if not isinstance(next_value, dict):
            next_value = {}
            current[key] = next_value
        current = next_value
    current[path[-1]] = value


def _protected_2fa_config_changed(old_config: dict, new_config: dict) -> bool:
    return any(
        _get_nested_value(old_config, path) != _get_nested_value(new_config, path)
        for path in PROTECTED_2FA_CONFIG_PATHS
    )


async def _validate_neo_connectivity(post_config: dict) -> str | None:
    ps = post_config.get("provider_settings", {})
    runtime = ps.get("computer_use_runtime", "none")
    sandbox = ps.get("sandbox", {})
    booter = sandbox.get("booter", "")

    if runtime != "sandbox" or booter != "shipyard_neo":
        return None

    endpoint = sandbox.get("shipyard_neo_endpoint", "").rstrip("/")
    if not endpoint:
        return "⚠️ Shipyard Neo endpoint 未设置"

    access_token = sandbox.get("shipyard_neo_access_token", "")
    if not access_token:
        from bot.core.computer.computer_client import _discover_bay_credentials

        access_token = _discover_bay_credentials(endpoint)

    if not access_token:
        return (
            "⚠️ 未找到 Bay API Key。请填写访问令牌，"
            "或确保 Bay 的 credentials.json 可被自动发现。"
        )

    import aiohttp

    health_url = f"{endpoint}/health"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                health_url,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return (
                        f"⚠️ Bay 健康检查失败 (HTTP {resp.status})，"
                        f"请确认 Bay 正在运行: {endpoint}"
                    )
    except Exception:
        return f"⚠️ 无法连接 Bay ({endpoint})，请确认 Bay 已启动。"

    return None


def save_config(
    post_config: dict,
    config: BotConfig,
    is_core: bool = False,
    *,
    runtime: dict | None = None,
) -> None:
    """Validate and persist a dashboard configuration update.

    Args:
        post_config: Submitted configuration to validate and save.
        config: Existing configuration and persistence target.
        is_core: Whether this is a core configuration rather than a plugin.
        runtime: Startup runtime snapshot supplied by the profile service.

    Raises:
        ValueError: If configuration validation fails.
    """
    if is_core:
        post_config["agent_runner"] = normalize_agent_runner(
            post_config.get("agent_runner")
        )

    try:
        if is_core:
            errors, post_config = validate_config(
                post_config,
                CONFIG_METADATA_2,
                is_core,
                runtime=runtime,
            )
        else:
            errors, post_config = validate_config(
                post_config,
                getattr(config, "schema", {}),
                is_core,
            )
    except BaseException as exc:
        logger.error(traceback.format_exc())
        logger.warning(f"验证配置时出现异常: {exc}")
        raise ValueError(f"验证配置时出现异常: {exc}")
    if errors:
        raise ValueError(f"格式校验未通过: {errors}")

    if is_core:
        _log_computer_config_changes(dict(config), post_config)
    config.save_config(post_config)
