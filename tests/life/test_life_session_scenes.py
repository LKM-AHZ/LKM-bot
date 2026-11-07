"""Unit tests for scene-aware reply policy and per-session pause."""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from bot.core.message.components import At, Plain, Reply
from bot.core.message.message_event_result import MessageChain
from bot.life.config import (
    HeartFlowConfig,
    LifeConfig,
    PauseConfig,
    ReplyConfig,
    ScenePolicyConfig,
    ScenesConfig,
)
from bot.life.heartflow.runtime import TurnRuntime
from bot.life.pipeline import LifeTurnStage, _plan_reply_supplement
from bot.life.reply.postprocess import ReplyPostProcessor
from bot.life.scenes import policy_for, quote_rate, scene_kind

GROUP_UMO = "aiocqhttp:GroupMessage:1"
PRIVATE_UMO = "aiocqhttp:FriendMessage:42"


class _Event:
    def __init__(
        self,
        umo: str = GROUP_UMO,
        text: str = "hello",
        group_id: str | None = "1",
        extra: dict | None = None,
    ) -> None:
        self.unified_msg_origin = umo
        self.message_str = text
        self._group_id = group_id
        self._extra = dict(extra or {})
        self.stopped = False

    def get_group_id(self):
        return self._group_id

    def get_extra(self, key: str, default=None):
        return self._extra.get(key, default)

    def stop_event(self) -> None:
        self.stopped = True

    def is_private_chat(self) -> bool:
        return self._group_id is None

    def is_button_interaction(self) -> bool:
        return False

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_messages(self) -> list:
        return []


class _Result:
    def __init__(self) -> None:
        self.chain = [Plain("你好")]

    def is_llm_result(self) -> bool:
        return True

    def derive(self, chain):
        return MessageChain(chain=list(chain))


class _Persona:
    def __init__(self, quote_rate: float) -> None:
        self._quote_rate = quote_rate

    async def profile_for(self, persona_id: str) -> dict:
        return {"quote_rate": self._quote_rate, "allow_markdown": True}


def test_scene_kind_classifies_sessions() -> None:
    assert scene_kind(GROUP_UMO) == "group"
    assert scene_kind(PRIVATE_UMO) == "private"
    assert scene_kind("webchat:whatever") == "other"
    assert scene_kind("webchat:whatever", group_id="7") == "group"


def test_quote_rate_scales_and_clamps() -> None:
    assert quote_rate(0.5, "never") == 0.0
    assert quote_rate(0.5, "occasionally") == 0.5
    assert quote_rate(0.5, "often") == 1.0
    assert quote_rate(0.25, "often") == 0.5


def test_policy_for_requires_the_switch() -> None:
    scenes = ScenesConfig(enabled=False, group=ScenePolicyConfig(allow_quote=False))
    assert policy_for(scenes, GROUP_UMO) is None

    scenes.enabled = True
    assert policy_for(scenes, GROUP_UMO).allow_quote is False
    assert policy_for(scenes, PRIVATE_UMO).allow_quote is True


def test_pause_defaults_are_off() -> None:
    assert PauseConfig().is_paused(GROUP_UMO) is False


def test_pause_by_chat_type() -> None:
    pause = PauseConfig(enabled=True, group=True)

    assert pause.is_paused(GROUP_UMO) is True
    assert pause.is_paused(PRIVATE_UMO) is False


def test_pause_by_session_rule_and_prefix() -> None:
    pause = PauseConfig(enabled=True, sessions=[GROUP_UMO, "qq:private:*", "987"])

    assert pause.is_paused(GROUP_UMO) is True
    assert pause.is_paused("qq:private:abc") is True
    assert pause.is_paused("other:session:1", group_id="987") is True
    assert pause.is_paused("other:session:1") is False


@pytest.mark.asyncio
async def test_turn_stage_stops_paused_conversations() -> None:
    config = LifeConfig(pause=PauseConfig(enabled=True, group=True))
    stage = LifeTurnStage()
    stage.service = SimpleNamespace(
        config=config, heartflow=TurnRuntime(HeartFlowConfig())
    )
    stage.wake_prefixes = []
    event = _Event()

    await stage.process(event)

    assert event.stopped is True


@pytest.mark.asyncio
async def test_turn_stage_lets_commands_through_when_paused() -> None:
    config = LifeConfig(pause=PauseConfig(enabled=True, group=True))
    stage = LifeTurnStage()
    stage.service = SimpleNamespace(
        config=config, heartflow=TurnRuntime(HeartFlowConfig())
    )
    stage.wake_prefixes = []
    event = _Event(text="/help")

    await stage.process(event)

    assert event.stopped is False


@pytest.mark.asyncio
async def test_turn_stage_respects_scene_reply_switch() -> None:
    config = LifeConfig(
        scenes=ScenesConfig(
            enabled=True,
            group=ScenePolicyConfig(reply_enabled=False),
        )
    )
    stage = LifeTurnStage()
    stage.service = SimpleNamespace(
        config=config, heartflow=TurnRuntime(HeartFlowConfig())
    )
    stage.wake_prefixes = []
    event = _Event()

    await stage.process(event)

    assert event.stopped is True


def _processor(scenes: ScenesConfig | None, base_rate: float = 1.0):
    return ReplyPostProcessor(
        ReplyConfig(
            enabled=True,
            quote_replies=True,
            quote_platforms=["aiocqhttp"],
        ),
        persona=_Persona(base_rate),
        rng=random.Random(0),
        scenes=scenes,
    )


@pytest.mark.asyncio
async def test_postprocess_never_quotes_when_scene_forbids_it() -> None:
    scenes = ScenesConfig(
        enabled=True,
        group=ScenePolicyConfig(allow_quote=False),
    )
    result = _Result()
    event = _Event()
    event.message_obj = SimpleNamespace(message_id="m1")

    await _processor(scenes).process(event, result)

    assert not any(isinstance(comp, Reply) for comp in result.chain)


@pytest.mark.asyncio
async def test_postprocess_often_doubles_the_quote_rate() -> None:
    scenes = ScenesConfig(
        enabled=True,
        group=ScenePolicyConfig(quote_frequency="often"),
    )
    result = _Result()
    event = _Event()
    event.message_obj = SimpleNamespace(message_id="m1")

    await _processor(scenes, base_rate=0.6).process(event, result)

    assert any(isinstance(comp, Reply) for comp in result.chain)

    occasional = _Result()
    await _processor(
        ScenesConfig(
            enabled=True,
            group=ScenePolicyConfig(quote_frequency="occasionally"),
        ),
        base_rate=0.6,
    ).process(_Event(), occasional)
    assert not any(isinstance(comp, Reply) for comp in occasional.chain)


@pytest.mark.asyncio
async def test_postprocess_never_frequency_blocks_quotes() -> None:
    scenes = ScenesConfig(
        enabled=True,
        group=ScenePolicyConfig(quote_frequency="never"),
    )
    result = _Result()
    event = _Event()
    event.message_obj = SimpleNamespace(message_id="m1")

    await _processor(scenes).process(event, result)

    assert not any(isinstance(comp, Reply) for comp in result.chain)


ROWS = [
    {
        "message_id": "m1",
        "sender_id": "u1",
        "sender_name": "小明",
        "plain_text": "hi",
        "components": [],
    }
]


class _Provider:
    def __init__(self, text: str) -> None:
        self.text = text

    async def text_chat(self, prompt: str, **_):
        return SimpleNamespace(completion_text=self.text)


class _Manager:
    def __init__(self, provider) -> None:
        self._provider = provider

    async def get_using_provider_async(self, kind):
        return self._provider


class _Store:
    async def fetch_range(self, session_key: str, limit: int = 24):
        return ROWS


def _supplement_service(scenes: ScenesConfig):
    return SimpleNamespace(
        config=SimpleNamespace(
            reply=ReplyConfig(supplement_enabled=True),
            scenes=scenes,
        ),
        store=_Store(),
        star_context=SimpleNamespace(provider_manager=_Manager(_Provider(
            '{"parts": [{"type": "reply"}, {"type": "at", "msg_id": "m1"}]}'
        ))),
        emoji=None,
    )


@pytest.mark.asyncio
async def test_supplement_honours_scene_mention_switch() -> None:
    allowed = await _plan_reply_supplement(
        _supplement_service(ScenesConfig(enabled=True)),
        _Event(),
        "hi",
    )
    blocked = await _plan_reply_supplement(
        _supplement_service(
            ScenesConfig(enabled=True, group=ScenePolicyConfig(allow_at=False))
        ),
        _Event(),
        "hi",
    )

    assert allowed is not None
    assert [item["kind"] for item in allowed["parts"]] == ["reply", "at"]
    assert blocked is None


@pytest.mark.asyncio
async def test_reaction_stage_skips_paused_sessions() -> None:
    from bot.life.pipeline import LifeReactionStage

    calls: list[str] = []

    class _Reactions:
        async def try_react(self, umo: str, text: str) -> bool:
            calls.append(text)
            return True

    config = LifeConfig(pause=PauseConfig(enabled=True, group=True))
    stage = LifeReactionStage()
    stage.service = SimpleNamespace(
        config=config,
        reactions=_Reactions(),
    )
    event = _Event()

    await stage.process(event)

    assert calls == []
    assert event.stopped is False


@pytest.mark.asyncio
async def test_acp_stage_skips_paused_sessions() -> None:
    from bot.life.pipeline import LifeAcpStage

    calls: list[str] = []

    class _Acp:
        enabled = True
        config = SimpleNamespace(takeover=True)

        async def ask(self, umo: str, text: str) -> str:
            calls.append(text)
            return "answer"

    config = LifeConfig(pause=PauseConfig(enabled=True, group=True))
    stage = LifeAcpStage()
    stage.service = SimpleNamespace(config=config, acp=_Acp())

    await stage.process(_Event())

    assert calls == []
