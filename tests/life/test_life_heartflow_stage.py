"""Unit tests for the heart-flow pipeline stage."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.core.message.components import At, AtAll, Reply
from bot.core.pipeline.stage_order import STAGES_ORDER
from bot.life.config import HeartFlowConfig, LifeConfig
from bot.life.heartflow.runtime import TurnRuntime
from bot.life.pipeline import LifeTurnStage

SESSION = "aiocqhttp:GroupMessage:1"


class FakeClock:
    """Clock that advances instead of sleeping."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        """Advance the clock.

        Args:
            delay: Requested delay in seconds.
        """
        await asyncio.sleep(0)
        self.now += max(delay, 0.0)


class FakeEvent:
    """Minimal event implementing what the stage touches."""

    def __init__(
        self,
        text: str = "随便说",
        *,
        private: bool = False,
        button: bool = False,
        components: list | None = None,
    ) -> None:
        self.unified_msg_origin = SESSION
        self.message_str = text
        self.message_obj = SimpleNamespace(timestamp=1000.0, message_id="m1")
        self._private = private
        self._button = button
        self._components = components or []
        self._extra: dict = {}
        self._extras = self._extra
        self._stopped = False
        self.role = "member"
        self.is_wake = False
        self.is_at_or_wake_command = False
        self.plugins_name = None
        self.sent: list = []

    def is_private_chat(self) -> bool:
        return self._private

    def is_button_interaction(self) -> bool:
        return self._button

    def get_messages(self) -> list:
        return self._components

    def get_sender_id(self) -> str:
        return "u1"

    def get_self_id(self) -> str:
        return "9"

    def get_button_interaction(self):
        return None

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    async def send(self, chain) -> None:
        self.sent.append(chain)

    def get_sender_name(self) -> str:
        return "Alice"

    def get_group_id(self) -> str:
        return "1"

    def stop_event(self) -> None:
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped

    def set_extra(self, key: str, value) -> None:
        self._extra[key] = value

    def get_extra(self, key: str, default=None):
        return self._extra.get(key, default)


def _service(scope_all: bool = True, bot_names: list[str] | None = None):
    clock = FakeClock()
    config = HeartFlowConfig(enabled=True, bot_names=bot_names or ["小助手"])
    runtime = TurnRuntime(config, clock=clock, sleeper=clock.sleep)
    life_config = LifeConfig(enabled=True)
    if scope_all:
        life_config.scope.mode = "all"
    return SimpleNamespace(config=life_config, heartflow=runtime), clock


def _stage(service) -> LifeTurnStage:
    stage = LifeTurnStage()
    stage.service = service
    stage.wake_prefixes = ["/"]
    return stage


def test_stages_run_between_ingest_and_wake_check():
    assert STAGES_ORDER.index("LifeReactionStage") == (
        STAGES_ORDER.index("LifeIngestStage") + 1
    )
    assert STAGES_ORDER.index("LifeAcpStage") == (
        STAGES_ORDER.index("LifeReactionStage") + 1
    )
    assert STAGES_ORDER.index("LifeTurnStage") == (
        STAGES_ORDER.index("LifeAcpStage") + 1
    )
    assert STAGES_ORDER.index("LifeTurnStage") < STAGES_ORDER.index("WakingCheckStage")


@pytest.mark.asyncio
async def test_answered_turn_continues_with_the_merged_text():
    service, _clock = _service()
    stage = _stage(service)
    event = FakeEvent("小助手 在吗")

    await stage.process(event)

    assert event.is_stopped() is False
    assert event.get_extra("_life_force_wake") is True
    assert "小助手 在吗" in event.message_str


@pytest.mark.asyncio
async def test_waited_turn_stops_the_event():
    service, _clock = _service()
    stage = _stage(service)
    event = FakeEvent("嗯嗯")

    await stage.process(event)

    assert event.is_stopped() is True
    assert event.get_extra("_life_force_wake") is None


@pytest.mark.asyncio
async def test_private_chat_passes_through():
    service, _clock = _service()
    stage = _stage(service)
    event = FakeEvent("你好", private=True)

    await stage.process(event)

    assert event.is_stopped() is False
    assert event.get_extra("_life_force_wake") is None


@pytest.mark.parametrize(
    "components",
    [[At(qq="123456")], [AtAll()], [Reply(id="m0", sender_id="9")]],
)
@pytest.mark.asyncio
async def test_addressed_messages_pass_through(components):
    service, _clock = _service()
    stage = _stage(service)
    event = FakeEvent("小助手 在吗", components=components)

    await stage.process(event)

    assert event.is_stopped() is False
    assert event.get_extra("_life_force_wake") is None


@pytest.mark.asyncio
async def test_command_passes_through():
    service, _clock = _service()
    stage = _stage(service)
    event = FakeEvent("/help")

    await stage.process(event)

    assert event.is_stopped() is False


@pytest.mark.asyncio
async def test_non_text_message_passes_through():
    service, _clock = _service()
    stage = _stage(service)
    event = FakeEvent("")

    await stage.process(event)

    assert event.is_stopped() is False


@pytest.mark.asyncio
async def test_button_interaction_passes_through():
    service, _clock = _service()
    stage = _stage(service)
    event = FakeEvent("确认", button=True)

    await stage.process(event)

    assert event.is_stopped() is False


@pytest.mark.asyncio
async def test_already_forced_event_is_not_batched_again():
    service, _clock = _service()
    stage = _stage(service)
    event = FakeEvent("接着说")
    event.set_extra("_life_force_wake", True)

    await stage.process(event)

    assert event.is_stopped() is False


@pytest.mark.asyncio
async def test_missing_runtime_is_a_noop():
    stage = _stage(SimpleNamespace(config=LifeConfig(enabled=True), heartflow=None))
    event = FakeEvent("随便说")

    await stage.process(event)

    assert event.is_stopped() is False


@pytest.mark.asyncio
async def test_scope_outside_the_allowlist_passes_through():
    service, _clock = _service(scope_all=False)
    stage = _stage(service)
    event = FakeEvent("小助手 在吗")

    await stage.process(event)

    assert event.is_stopped() is False


def _waking_context() -> SimpleNamespace:
    return SimpleNamespace(
        bot_config={
            "platform_settings": {
                "no_permission_reply": False,
                "friend_message_needs_wake_prefix": False,
                "ignore_bot_self_message": False,
                "ignore_at_all": False,
            },
            "admins_id": [],
            "wake_prefix": ["/"],
            "disable_builtin_commands": False,
        },
        db_helper=None,
        bot_config_id="test",
    )


async def _identity(event, handlers):
    return handlers


@pytest.mark.asyncio
async def test_forced_wake_bypasses_the_wake_check(monkeypatch):
    from bot.core.pipeline.waking_check.stage import WakingCheckStage
    from bot.core.star.session_plugin_manager import SessionPluginManager

    monkeypatch.setattr(
        SessionPluginManager,
        "filter_handlers_by_session",
        staticmethod(_identity),
    )
    stage = WakingCheckStage()
    await stage.initialize(_waking_context())
    event = FakeEvent("接着说")
    event.set_extra("_life_force_wake", True)

    await stage.process(event)

    assert event.is_wake is True
    assert event.is_stopped() is False


@pytest.mark.asyncio
async def test_ambient_group_message_is_dropped_without_the_flag(monkeypatch):
    from bot.core.pipeline.waking_check.stage import WakingCheckStage
    from bot.core.star.session_plugin_manager import SessionPluginManager

    monkeypatch.setattr(
        SessionPluginManager,
        "filter_handlers_by_session",
        staticmethod(_identity),
    )
    stage = WakingCheckStage()
    await stage.initialize(_waking_context())
    event = FakeEvent("接着说")

    await stage.process(event)

    assert event.is_wake is False
    assert event.is_stopped() is True
