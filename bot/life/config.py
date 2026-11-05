"""Configuration for the Life layer.

The Life layer is the sidecar subsystem used to migrate MaiBot capabilities. It
stays disabled by default and keeps its own ``data/life/config.json`` instead of
writing into ``cmd_config.json``, so the upstream configuration schema is not
polluted.

Gray release: ``enabled`` is the master switch and ``scope.mode`` controls the
rollout. The default ``allowlist`` mode only takes over sessions explicitly
listed in ``enabled_umos`` or ``enabled_group_ids``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from bot import logger
from bot.core.utils.bot_path import get_bot_data_path, get_bot_root

LIFE_DIR_NAME = "life"
LIFE_CONFIG_FILE_NAME = "config.json"


class IngestConfig(BaseModel):
    """Message archive settings."""

    enabled: bool = True
    """Whether incoming messages are archived; disabled means a no-op stage."""

    store_components: bool = True
    """Whether full message components are stored; otherwise text only."""

    max_text_chars: int = 4000
    """Maximum stored plain-text length per message."""

    max_component_chars: int = 512
    """Maximum stored component field length; base64 is folded to a marker."""

    queue_size: int = 10000
    """In-memory queue capacity; overflow drops the oldest batch and counts."""

    batch_size: int = 50
    """Maximum rows written per flush."""

    retention_days: int = 0
    """Retention in days; 0 keeps messages forever."""


class ScopeConfig(BaseModel):
    """Gray-release scope."""

    mode: Literal["allowlist", "all"] = "allowlist"
    """``allowlist`` only takes over listed sessions; ``all`` takes over all."""

    enabled_umos: list[str] = Field(default_factory=list)
    """Sessions enabled by unified_msg_origin."""

    enabled_group_ids: list[str] = Field(default_factory=list)
    """Group ids enabled for the rollout."""

    disabled_umos: list[str] = Field(default_factory=list)
    """Sessions explicitly excluded; takes precedence over enabled lists."""


class SenderConfig(BaseModel):
    """Proactive sending settings."""

    enabled: bool = True
    timeout_seconds: float = 30.0


class LifeConfig(BaseModel):
    """Root Life layer configuration."""

    enabled: bool = False

    data_dir: str = ""
    """Data directory; empty means ``data/life``. Relative paths use BOT_ROOT."""

    ingest: IngestConfig = Field(default_factory=IngestConfig)
    scope: ScopeConfig = Field(default_factory=ScopeConfig)
    sender: SenderConfig = Field(default_factory=SenderConfig)

    def resolved_data_dir(self, override: str | Path | None = None) -> Path:
        """Return the resolved Life data directory.

        Args:
            override: Explicit directory from the caller; highest priority.

        Returns:
            Absolute path of the data directory.
        """
        if override is not None:
            path = Path(override)
        elif self.data_dir:
            path = Path(self.data_dir)
        else:
            return Path(get_bot_data_path()) / LIFE_DIR_NAME
        if not path.is_absolute():
            path = Path(get_bot_root()) / path
        return path

    def session_enabled(self, umo: str, group_id: str | None = None) -> bool:
        """Return whether a session is taken over by the Life layer.

        Args:
            umo: unified_msg_origin of the session.
            group_id: Group id, or None for private sessions.

        Returns:
            True when the session is enabled.
        """
        if not self.enabled:
            return False
        if umo in self.scope.disabled_umos:
            return False
        if self.scope.mode == "all":
            return True
        if umo in self.scope.enabled_umos:
            return True
        return bool(group_id and group_id in self.scope.enabled_group_ids)


def default_config_path() -> Path:
    """Return the default configuration file path."""
    return Path(get_bot_data_path()) / LIFE_DIR_NAME / LIFE_CONFIG_FILE_NAME


def load_life_config(path: str | Path | None = None) -> LifeConfig:
    """Load the configuration, falling back to defaults on errors.

    Args:
        path: Configuration file path; defaults to ``data/life/config.json``.

    Returns:
        Loaded configuration, or defaults when the file is missing or invalid.
    """
    config_path = Path(path) if path is not None else default_config_path()
    if not config_path.exists():
        return LifeConfig()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error(f"[life] failed to read config, using defaults: {exc}")
        return LifeConfig()
    try:
        return LifeConfig.model_validate(raw)
    except ValidationError as exc:
        logger.error(f"[life] invalid config, using defaults: {exc}")
        return LifeConfig()


def save_life_config(config: LifeConfig, path: str | Path | None = None) -> Path:
    """Atomically write the configuration.

    Args:
        config: Configuration to persist.
        path: Target path; defaults to ``data/life/config.json``.

    Returns:
        The written path.

    Raises:
        OSError: When the file cannot be written or replaced.
    """
    config_path = Path(path) if path is not None else default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2)
    handle, tmp_name = tempfile.mkstemp(
        dir=str(config_path.parent),
        prefix=".life-config-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(payload)
            file.write("\n")
        os.replace(tmp_name, config_path)
    except BaseException:
        tmp_path = Path(tmp_name)
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    return config_path
