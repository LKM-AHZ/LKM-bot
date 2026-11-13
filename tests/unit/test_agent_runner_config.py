import copy
import json
from types import SimpleNamespace

import pytest

from bot.core.config.agent import get_runtime_config_default
from bot.core.config.agent_runner import (
    AGENT_RUNNER_CONFIG_DEFAULTS,
    get_agent_runner_config_default,
    normalize_agent_runner,
)
from bot.core.config.bot_config import BotConfig
from bot.core.config.default import DEFAULT_CONFIG
from bot.core.pipeline.process_stage.stage import AgentRequestSubStage


@pytest.mark.parametrize(
    "runner_type", ["local", "dify", "coze", "dashscope", "deerflow"]
)
def test_agent_runner_defaults_are_isolated_and_normalized(runner_type: str):
    first = get_agent_runner_config_default(runner_type)
    second = get_agent_runner_config_default(runner_type)

    first["test_mutation"] = True

    assert second == AGENT_RUNNER_CONFIG_DEFAULTS[runner_type]
    assert normalize_agent_runner({"runner_type": runner_type, "config": second}) == {
        "runner_type": runner_type,
        "config": second,
    }
    if runner_type != "local":
        assert "persona_id" not in second


def test_switching_runner_type_discards_previous_runner_fields():
    normalized = normalize_agent_runner(
        {
            "runner_type": "dify",
            "config": {
                "provider_id": "legacy-provider",
                "persona_id": "legacy-persona",
                "model": {"provider_id": "chat-model"},
                "dify_api_key": "secret",
                "unexpected": True,
            },
        }
    )

    assert normalized["config"] == {
        **get_agent_runner_config_default("dify"),
        "dify_api_key": "secret",
    }
    assert "provider_id" not in normalized["config"]
    assert "persona_id" not in normalized["config"]
    assert "model" not in normalized["config"]


@pytest.mark.asyncio
async def test_agent_request_normalizes_incomplete_runner_config():
    config = {
        "wake_prefix": [],
        "provider_settings": {
            "wake_prefix": "",
            "streaming_response": True,
            "unsupported_streaming_strategy": "aggregate",
        },
        "agent": {
            "runtime": "dify",
            "config": {"dify_api_key": "saved-key"},
        },
    }
    stage = AgentRequestSubStage()
    context = SimpleNamespace(bot_config=config, agent_runtimes={})

    await stage.initialize(context)

    assert context.bot_config["agent"]["config"] == {
        **get_agent_runner_config_default("dify"),
        "dify_api_key": "saved-key",
    }


@pytest.mark.parametrize(
    "runner_type", ["local", "dify", "coze", "dashscope", "deerflow"]
)
def test_each_runner_configuration_round_trips(tmp_path, runner_type: str):
    config = copy.deepcopy(DEFAULT_CONFIG)
    expected = {
        "runtime": "bot" if runner_type == "local" else runner_type,
        "config": get_runtime_config_default(
            "bot" if runner_type == "local" else runner_type
        ),
    }
    config["agent"] = expected
    config_path = tmp_path / f"{runner_type}.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    loaded = BotConfig(config_path=str(config_path))
    loaded.save_config()
    reloaded = BotConfig(config_path=str(config_path))

    assert reloaded["agent"] == expected


def test_new_agent_config_is_authoritative_and_opaque_on_reload(tmp_path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["config_version"] = 2
    config["provider_settings"]["agent_runner_type"] = "coze"
    config["agent"] = {
        "runtime": "dify",
        "config": {
            **get_agent_runner_config_default("dify"),
            "dify_api_key": "saved-key",
            "variables": {"nested": {"value": 1}},
        },
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    loaded = BotConfig(config_path=str(config_path))

    assert loaded["agent"]["runtime"] == "dify"
    assert loaded["agent"]["config"]["dify_api_key"] == "saved-key"
    assert loaded["agent"]["config"]["variables"] == {"nested": {"value": 1}}
    assert "agent_runner_type" not in loaded["provider_settings"]
