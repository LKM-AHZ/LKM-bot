"""Unit tests for super creative reply mode (MaiBot 1.4 experimental)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.core.message.components import Plain
from bot.core.message.message_event_result import MessageChain
from bot.life.config import CreativeConfig, ReplyConfig
from bot.life.reply.creative import (
    CLASSICAL_TRANSLATION_INSTRUCTION,
    OUTPUT_LANGUAGE,
    TRANSLATION_INSTRUCTION,
    creative_instruction,
    translate_reply,
)
from bot.life.reply.postprocess import ReplyPostProcessor


class _FakeProvider:
    def __init__(self, text: str | None, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.prompts: list[str] = []

    async def text_chat(self, prompt: str, **_):
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(completion_text=self.text)


class _FakeEvent:
    def __init__(self, extra: dict | None = None) -> None:
        self.unified_msg_origin = "aiocqhttp:GroupMessage:1"
        self._extra = dict(extra or {})
        self.sent: list = []

    def get_extra(self, key: str, default=None):
        return self._extra.get(key, default)

    def get_group_id(self):
        return "1"

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    async def send(self, chain) -> None:
        self.sent.append(chain)


class _FakePersona:
    async def profile_for(self, persona_id: str) -> dict:
        return {}


class _FakeResult:
    def __init__(self, text: str = "日本語の返事") -> None:
        self.chain = [Plain(text)]

    def is_llm_result(self) -> bool:
        return True

    def derive(self, chain):
        return MessageChain(chain=list(chain))


def _processor(translator) -> ReplyPostProcessor:
    return ReplyPostProcessor(
        ReplyConfig(enabled=True),
        persona=_FakePersona(),
        creative=CreativeConfig(enabled=True),
        translator=translator,
    )


def test_creative_config_defaults_are_off() -> None:
    config = CreativeConfig()

    assert config.enabled is False
    assert config.language == "ja-JP"
    assert config.translate_timeout_seconds == 20.0


def test_every_language_has_an_instruction() -> None:
    for language, instruction in OUTPUT_LANGUAGE.items():
        assert creative_instruction(language) == instruction
    assert creative_instruction("unknown") == ""


def test_translation_instructions_differ_for_classical_chinese() -> None:
    assert CLASSICAL_TRANSLATION_INSTRUCTION != TRANSLATION_INSTRUCTION
    assert "文言文" in CLASSICAL_TRANSLATION_INSTRUCTION
    assert "文言文" not in TRANSLATION_INSTRUCTION


@pytest.mark.asyncio
async def test_translate_reply_returns_the_translation() -> None:
    provider = _FakeProvider("你好呀")

    translated = await translate_reply(
        provider, "こんにちは", language="ja-JP", timeout_seconds=5
    )

    assert translated == "你好呀"
    assert TRANSLATION_INSTRUCTION in provider.prompts[0]
    assert "こんにちは" in provider.prompts[0]


@pytest.mark.asyncio
async def test_translate_reply_uses_the_classical_instruction() -> None:
    provider = _FakeProvider("你好")

    await translate_reply(provider, "善哉", language="zh-classical")

    assert CLASSICAL_TRANSLATION_INSTRUCTION in provider.prompts[0]


@pytest.mark.asyncio
async def test_translate_reply_soft_fails() -> None:
    assert await translate_reply(None, "こんにちは", language="ja-JP") is None
    assert await translate_reply(_FakeProvider(""), "x", language="ja-JP") is None
    assert (
        await translate_reply(
            _FakeProvider(None, error=RuntimeError("boom")), "x", language="ja-JP"
        )
        is None
    )


@pytest.mark.asyncio
async def test_postprocess_translates_before_other_rewrites() -> None:
    seen: list[str] = []

    async def translator(text: str) -> str | None:
        seen.append(text)
        return "你好呀"

    processor = _processor(translator)
    result = _FakeResult("こんにちは")

    await processor.process(_FakeEvent(), result)

    assert seen == ["こんにちは"]
    assert [comp.text for comp in result.chain if isinstance(comp, Plain)] == ["你好呀"]
    assert processor._counters["translated"] == 1


@pytest.mark.asyncio
async def test_postprocess_keeps_original_when_translation_fails() -> None:
    async def translator(text: str) -> str | None:
        return None

    processor = _processor(translator)
    result = _FakeResult("こんにちは")

    await processor.process(_FakeEvent(), result)

    assert [comp.text for comp in result.chain if isinstance(comp, Plain)] == [
        "こんにちは"
    ]
    assert processor._counters["translated"] == 0


@pytest.mark.asyncio
async def test_postprocess_without_translator_is_unchanged() -> None:
    processor = ReplyPostProcessor(
        ReplyConfig(enabled=True),
        persona=_FakePersona(),
    )
    result = _FakeResult("在吗")

    await processor.process(_FakeEvent(), result)

    assert [comp.text for comp in result.chain if isinstance(comp, Plain)] == ["在吗"]


@pytest.mark.asyncio
async def test_inject_appends_the_language_instruction_once(monkeypatch) -> None:
    import bot.life.memory.inject as inject_module

    service = SimpleNamespace(
        config=SimpleNamespace(creative=CreativeConfig(enabled=True, language="ru-RU")),
        reply=object(),
    )
    monkeypatch.setattr(inject_module, "get_life_service", lambda: service)
    request = SimpleNamespace(system_prompt="SYS")

    await inject_module.inject_life_context(_FakeEvent(), request)
    first = request.system_prompt
    await inject_module.inject_life_context(_FakeEvent(), request)

    assert OUTPUT_LANGUAGE["ru-RU"] in first
    assert request.system_prompt == first


@pytest.mark.asyncio
async def test_inject_skips_instruction_without_the_reply_layer(monkeypatch) -> None:
    import bot.life.memory.inject as inject_module

    service = SimpleNamespace(
        config=SimpleNamespace(creative=CreativeConfig(enabled=True)),
        reply=None,
    )
    monkeypatch.setattr(inject_module, "get_life_service", lambda: service)
    request = SimpleNamespace(system_prompt="SYS")

    await inject_module.inject_life_context(_FakeEvent(), request)

    assert request.system_prompt == "SYS"


@pytest.mark.asyncio
async def test_inject_skips_instruction_when_disabled(monkeypatch) -> None:
    import bot.life.memory.inject as inject_module

    service = SimpleNamespace(
        config=SimpleNamespace(creative=CreativeConfig(enabled=False))
    )
    monkeypatch.setattr(inject_module, "get_life_service", lambda: service)
    request = SimpleNamespace(system_prompt="SYS")

    await inject_module.inject_life_context(_FakeEvent(), request)

    assert request.system_prompt == "SYS"
