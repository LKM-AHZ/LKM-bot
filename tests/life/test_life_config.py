"""Unit tests for the Life layer configuration."""

from __future__ import annotations

from bot.life.config import (
    LifeConfig,
    load_life_config,
    save_life_config,
)


def test_defaults_are_disabled():
    config = LifeConfig()
    assert config.enabled is False
    assert config.scope.mode == "allowlist"
    assert config.session_enabled("aiocqhttp:GroupMessage:1") is False


def test_allowlist_scope():
    config = LifeConfig(enabled=True)
    config.scope.enabled_umos = ["aiocqhttp:GroupMessage:1"]
    config.scope.enabled_group_ids = ["2"]

    assert config.session_enabled("aiocqhttp:GroupMessage:1")
    assert config.session_enabled("aiocqhttp:GroupMessage:9", group_id="2")
    assert not config.session_enabled("aiocqhttp:GroupMessage:9", group_id="3")


def test_disabled_scope_wins_over_allowlist():
    config = LifeConfig(enabled=True)
    config.scope.enabled_umos = ["aiocqhttp:GroupMessage:1"]
    config.scope.disabled_umos = ["aiocqhttp:GroupMessage:1"]

    assert not config.session_enabled("aiocqhttp:GroupMessage:1")


def test_all_mode_covers_every_session():
    config = LifeConfig(enabled=True)
    config.scope.mode = "all"

    assert config.session_enabled("aiocqhttp:FriendMessage:any")


def test_config_roundtrip(tmp_path):
    path = tmp_path / "life" / "config.json"
    original = LifeConfig(enabled=True)
    original.scope.enabled_umos = ["aiocqhttp:GroupMessage:1"]
    original.ingest.retention_days = 7

    saved = save_life_config(original, path)
    loaded = load_life_config(saved)

    assert loaded.enabled is True
    assert loaded.scope.enabled_umos == ["aiocqhttp:GroupMessage:1"]
    assert loaded.ingest.retention_days == 7


def test_invalid_config_falls_back_to_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"enabled": "not-a-bool", "unknown": 1}', encoding="utf-8")

    config = load_life_config(path)

    assert config.enabled is False


def test_heartflow_defaults_are_disabled():
    config = LifeConfig()

    assert config.heartflow.enabled is False
    assert config.heartflow.debounce.enabled is True
    assert config.heartflow.debounce.hard_cap_seconds == 8.0
    assert config.heartflow.gate.base_threshold == 0.55
    assert config.heartflow.gate.weights["at_bot"] > 0


def test_heartflow_roundtrip(tmp_path):
    path = tmp_path / "life" / "config.json"
    original = LifeConfig(enabled=True)
    original.heartflow.enabled = True
    original.heartflow.debounce.natural_seconds = 3.0
    original.heartflow.gate.base_threshold = 0.6
    original.heartflow.gate.weights["question"] = 0.9

    saved = save_life_config(original, path)
    loaded = load_life_config(saved)

    assert loaded.heartflow.enabled is True
    assert loaded.heartflow.debounce.natural_seconds == 3.0
    assert loaded.heartflow.gate.base_threshold == 0.6
    assert loaded.heartflow.gate.weights["question"] == 0.9
