"""Tests for planner monitoring: decisions, deliveries and reasoning display."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from bot.life.config import LifeConfig, MonitorConfig
from bot.life.monitor.recorder import PlannerMonitor
from bot.life.pipeline import LifeTurnStage
from bot.life.reply.postprocess import postprocess_life_reply


def _monitor(**kwargs) -> PlannerMonitor:
    return PlannerMonitor(MonitorConfig(enabled=True, **kwargs))


def _event(umo: str = "aiocqhttp:GroupMessage:1", reasoning: str = ""):
    return SimpleNamespace(
        unified_msg_origin=umo,
        get_extra=lambda key, default=None: (
            reasoning if key == "_llm_reasoning_content" else default
        ),
    )


def _result(text: str = "在的", parts: int = 1):
    return SimpleNamespace(
        chain=[SimpleNamespace(text="x") for _ in range(parts)],
        get_plain_text=lambda: text,
    )


def test_record_decision_counts_reply_and_wait() -> None:
    monitor = _monitor()
    monitor.record_decision(
        session_id="s1",
        action="continue",
        reply=True,
        likelihood=0.81,
        threshold=0.55,
        reason="reply",
        now=100.0,
    )
    monitor.record_decision(
        session_id="s1",
        action="hold",
        reply=False,
        likelihood=0.2,
        threshold=0.55,
        reason="below_threshold",
        now=101.0,
    )

    status = monitor.status()
    assert status["counters"]["decisions"] == 2
    assert status["counters"]["replies"] == 1
    assert status["counters"]["waits"] == 1
    assert status["last"]["reason"] == "below_threshold"
    record = monitor.recent("s1", limit=1)[0]
    assert record["likelihood"] == 0.2
    assert record["threshold"] == 0.55
    assert record["created_at"] == 101.0


def test_ring_drops_the_oldest() -> None:
    monitor = _monitor(recent_size=2)
    for index in range(3):
        monitor.record_decision(
            session_id="s1",
            action="hold",
            reply=False,
            likelihood=0.0,
            threshold=0.5,
            reason=f"r{index}",
            now=100.0 + index,
        )
    assert [item["reason"] for item in monitor.recent()] == ["r2", "r1"]
    assert monitor.status()["counters"]["dropped"] == 1


def test_recent_filters_by_session_and_limit() -> None:
    monitor = _monitor()
    for umo in ("s1", "s2"):
        monitor.record_decision(
            session_id=umo,
            action="hold",
            reply=False,
            likelihood=0.0,
            threshold=0.5,
            reason=umo,
        )
    assert [item["session_id"] for item in monitor.recent("s1")] == ["s1"]
    assert [item["session_id"] for item in monitor.recent()] == ["s2", "s1"]


def test_record_delivery_captures_chars_and_reasoning() -> None:
    monitor = _monitor(reasoning_max_chars=5)
    monitor.record_delivery(_event(reasoning="这是一段很长的推理"), _result("在的", parts=2))

    record = monitor.recent()[0]
    assert record["kind"] == "delivery"
    assert record["chars"] == 2
    assert record["parts"] == 2
    assert record["reasoning"] == "这是一段很"
    assert monitor.status()["counters"]["deliveries"] == 1


def test_reasoning_capture_can_be_disabled() -> None:
    monitor = _monitor(capture_reasoning=False)
    monitor.record_delivery(_event(reasoning="推理"), _result())
    assert monitor.recent()[0]["reasoning"] == ""
    assert monitor.status()["capture_reasoning"] is False


class _FakeMonitor:
    def __init__(self) -> None:
        self.decisions: list[dict] = []
        self.deliveries: list[tuple] = []

    def record_decision(self, **kwargs) -> None:
        self.decisions.append(kwargs)

    def record_delivery(self, event, result) -> None:
        self.deliveries.append((event, result))


def _stage(service) -> LifeTurnStage:
    stage = LifeTurnStage()
    stage.service = service
    stage.wake_prefixes = ["/"]
    return stage


class _StageEvent:
    def __init__(self, text: str = "随便说") -> None:
        self.unified_msg_origin = "aiocqhttp:GroupMessage:1"
        self.message_str = text
        self.message_obj = SimpleNamespace(timestamp=time.time(), message_id="m1")
        self._extra: dict = {}
        self._stopped = False

    def is_private_chat(self) -> bool:
        return False

    def is_button_interaction(self) -> bool:
        return False

    def get_messages(self) -> list:
        return []

    def get_sender_id(self) -> str:
        return "u1"

    def get_sender_name(self) -> str:
        return "Alice"

    def get_group_id(self) -> str:
        return "1"

    def stop_event(self) -> None:
        self._stopped = True

    def set_extra(self, key: str, value) -> None:
        self._extra[key] = value

    def get_extra(self, key: str, default=None):
        return self._extra.get(key, default)


class _InstantClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(0)
        self.now += max(delay, 0.0)


@pytest.mark.asyncio
async def test_turn_stage_records_the_gate_decision() -> None:
    from bot.life.config import HeartFlowConfig
    from bot.life.heartflow.runtime import TurnRuntime

    clock = _InstantClock()
    runtime = TurnRuntime(
        HeartFlowConfig(enabled=True, bot_names=["小助手"]),
        clock=clock,
        sleeper=clock.sleep,
    )
    monitor = _FakeMonitor()
    life_config = LifeConfig(enabled=True)
    life_config.scope.mode = "all"
    service = SimpleNamespace(
        config=life_config,
        heartflow=runtime,
        monitor=monitor,
    )
    stage = _stage(service)

    await stage.process(_StageEvent("小助手 在吗"))

    assert len(monitor.decisions) == 1
    decision = monitor.decisions[0]
    assert decision["session_id"] == "aiocqhttp:GroupMessage:1"
    assert decision["action"] == "continue"
    assert decision["reply"] is True
    assert decision["likelihood"] > 0.0


@pytest.mark.asyncio
async def test_postprocess_records_delivery_even_without_reply_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bot.life.service as life_service_module

    monitor = _FakeMonitor()
    service = SimpleNamespace(reply=None, monitor=monitor)
    monkeypatch.setattr(life_service_module, "get_life_service", lambda: service)

    await postprocess_life_reply(_event(), _result("在的"), upstream_segmented=False)

    assert len(monitor.deliveries) == 1


@pytest.mark.asyncio
async def test_monitor_endpoint_returns_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bot.dashboard.api import life as life_api
    import bot.life.service as life_service_module

    monitor = _monitor()
    monitor.record_decision(
        session_id="s1",
        action="hold",
        reply=False,
        likelihood=0.2,
        threshold=0.55,
        reason="below_threshold",
    )
    monkeypatch.setattr(
        life_service_module,
        "get_life_service",
        lambda: SimpleNamespace(monitor=monitor),
    )

    payload = await life_api.life_monitor(session="", limit=10, _auth=None)

    assert payload["status"] == "ok"
    assert payload["data"]["enabled"] is True
    assert payload["data"]["records"][0]["kind"] == "decision"
    assert payload["data"]["status"]["counters"]["decisions"] == 1


@pytest.mark.asyncio
async def test_monitor_endpoint_without_service(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.dashboard.api import life as life_api
    import bot.life.service as life_service_module

    monkeypatch.setattr(life_service_module, "get_life_service", lambda: None)
    payload = await life_api.life_monitor(session="", limit=10, _auth=None)
    assert payload["data"] == {"enabled": False, "records": []}
