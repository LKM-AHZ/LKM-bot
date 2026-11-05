"""Unit tests for Life message normalization."""

from __future__ import annotations

import time
from types import SimpleNamespace

from bot.life.config import IngestConfig
from bot.life.ingest import normalize_event


class FakeComponent:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def toDict(self) -> dict:
        return self._payload


class FakeEvent:
    unified_msg_origin = "aiocqhttp:GroupMessage:123"
    is_at_or_wake_command = True
    is_wake = True
    created_at = time.time()

    def __init__(
        self,
        message_id: str = "m1",
        components: list | None = None,
        text: str = "hello",
    ) -> None:
        self.message_obj = SimpleNamespace(message_id=message_id, timestamp=1700000000)
        self._components = components or [
            FakeComponent({"type": "at", "data": {"qq": "all"}}),
            FakeComponent({"type": "text", "data": {"text": text}}),
        ]
        self._text = text

    def get_messages(self) -> list:
        return self._components

    def get_message_str(self) -> str:
        return self._text

    def get_message_type(self):
        return SimpleNamespace(value="GroupMessage")

    def get_platform_id(self) -> str:
        return "aiocqhttp"

    def get_group_id(self) -> str:
        return "123"

    def get_sender_id(self) -> str:
        return "u1"

    def get_sender_name(self) -> str:
        return "Alice"


def test_normalize_basic_fields():
    message = normalize_event(FakeEvent(), IngestConfig())

    assert message.platform_id == "aiocqhttp"
    assert message.session_key == "aiocqhttp:GroupMessage:123"
    assert message.message_id == "m1"
    assert message.ts == 1700000000
    assert message.group_id == "123"
    assert message.plain_text == "hello"
    assert message.flags["at"] is True
    assert message.flags["at_all"] is True
    assert message.flags["is_group"] is True
    assert len(message.components) == 2


def test_normalize_falls_back_to_synthetic_message_id():
    event = FakeEvent(message_id="")
    message = normalize_event(event, IngestConfig())

    assert message.message_id.startswith("auto:aiocqhttp:GroupMessage:123:")


def test_base64_is_folded_and_long_text_truncated():
    payload = {
        "type": "image",
        "data": {"file": "base64://" + "a" * 200, "url": "b" * 50},
    }
    event = FakeEvent(components=[FakeComponent(payload)])
    ingest = IngestConfig(max_component_chars=10)

    message = normalize_event(event, ingest)

    image = message.components[0]
    assert image["data"]["file"] == "base64://<200 chars>"
    assert image["data"]["url"].endswith("<50 chars>")


def test_components_can_be_type_only():
    event = FakeEvent()
    ingest = IngestConfig(store_components=False)

    message = normalize_event(event, ingest)

    assert message.components[0] == {"type": "at"}
    assert message.components[0].get("data") is None


def test_reply_component_sets_reply_to():
    event = FakeEvent(
        components=[
            FakeComponent({"type": "reply", "data": {"id": "m0"}}),
            FakeComponent({"type": "text", "data": {"text": "hi"}}),
        ]
    )

    message = normalize_event(event, IngestConfig())

    assert message.reply_to == "m0"
    assert message.flags["reply"] is True
