"""Tests for keyword reactions: matching, cooldowns and the reaction stage."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.life.config import LifeConfig, ReactionConfig, ReactionRule
from bot.life.pipeline import LifeReactionStage
from bot.life.reaction.service import ReactionService


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class FakeSend:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, umo: str, text: str):
        self.calls.append((umo, text))
        return SimpleNamespace(ok=self.ok, error="" if self.ok else "send failed")


def _service(rules: list[ReactionRule], ok: bool = True):
    clock = FakeClock()
    send = FakeSend(ok)
    config = ReactionConfig(enabled=True, rules=rules)
    return ReactionService(config, send, clock=clock), send, clock


def test_match_substring_is_case_insensitive() -> None:
    service, _send, _clock = _service(
        [ReactionRule(keywords=["典"], reply="确实典"), ReactionRule(keywords=["早安"], reply="早")]
    )
    assert service.match("这也太典了") is not None
    assert service.match("这也太典了")[0] == 0
    assert service.match("大家早安")[0] == 1
    assert service.match("   ") is None
    assert service.match("无关内容") is None


def test_match_exact_and_first_rule_wins() -> None:
    service, _send, _clock = _service(
        [
            ReactionRule(keywords=["草"], reply="a", exact=True),
            ReactionRule(keywords=["草"], reply="b"),
        ]
    )
    assert service.match("草")[1].reply == "a"
    assert service.match("草泥马")[1].reply == "b"


def test_rules_without_reply_are_skipped() -> None:
    service, _send, _clock = _service(
        [ReactionRule(keywords=["x"], reply="   "), ReactionRule(keywords=["x"], reply="ok")]
    )
    assert service.match("x")[0] == 1


@pytest.mark.asyncio
async def test_react_sends_once_then_cools_down() -> None:
    service, send, clock = _service(
        [ReactionRule(keywords=["典"], reply="确实典", cooldown_seconds=60)]
    )
    assert await service.try_react("s1", "太典了") is True
    assert send.calls == [("s1", "确实典")]

    clock.now += 30
    assert await service.try_react("s1", "太典了") is False
    assert service.status()["counters"]["cooled"] == 1

    clock.now += 31
    assert await service.try_react("s1", "又典了") is True
    assert len(send.calls) == 2


@pytest.mark.asyncio
async def test_cooldown_is_per_session() -> None:
    service, send, _clock = _service(
        [ReactionRule(keywords=["典"], reply="确实典", cooldown_seconds=60)]
    )
    assert await service.try_react("s1", "典") is True
    assert await service.try_react("s2", "典") is True
    assert len(send.calls) == 2


@pytest.mark.asyncio
async def test_send_failure_does_not_start_the_cooldown() -> None:
    service, send, _clock = _service(
        [ReactionRule(keywords=["典"], reply="确实典", cooldown_seconds=60)], ok=False
    )
    assert await service.try_react("s1", "典") is False
    assert service.status()["counters"]["failed"] == 1
    assert await service.try_react("s1", "典") is False
    assert len(send.calls) == 2


class StageEvent:
    def __init__(self, text: str = "典", umo: str = "aiocqhttp:GroupMessage:1") -> None:
        self.unified_msg_origin = umo
        self.message_str = text
        self._stopped = False

    def get_group_id(self) -> str:
        return "1"

    def stop_event(self) -> None:
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped


def _stage(reactions, *, scope_all: bool = True) -> LifeReactionStage:
    config = LifeConfig(enabled=True)
    if scope_all:
        config.scope.mode = "all"
    stage = LifeReactionStage()
    stage.service = SimpleNamespace(
        config=config,
        reactions=reactions,
    )
    return stage


@pytest.mark.asyncio
async def test_stage_sends_and_stops_on_match() -> None:
    service, send, _clock = _service([ReactionRule(keywords=["典"], reply="确实典")])
    stage = _stage(service)
    event = StageEvent("太典了")

    await stage.process(event)

    assert send.calls == [("aiocqhttp:GroupMessage:1", "确实典")]
    assert event.is_stopped() is True


@pytest.mark.asyncio
async def test_stage_passes_unmatched_and_commands() -> None:
    service, send, _clock = _service([ReactionRule(keywords=["典"], reply="确实典")])
    stage = _stage(service)

    unmatched = StageEvent("今天天气不错")
    await stage.process(unmatched)
    command = StageEvent("/典")
    await stage.process(command)

    assert send.calls == []
    assert unmatched.is_stopped() is False
    assert command.is_stopped() is False


@pytest.mark.asyncio
async def test_stage_respects_scope_and_missing_service() -> None:
    service, send, _clock = _service([ReactionRule(keywords=["典"], reply="确实典")])
    stage = _stage(service, scope_all=False)
    event = StageEvent("典")

    await stage.process(event)

    assert send.calls == []
    assert event.is_stopped() is False

    no_reactions = LifeReactionStage()
    no_reactions.service = SimpleNamespace(config=LifeConfig(enabled=True), reactions=None)
    await no_reactions.process(event)
    assert event.is_stopped() is False
