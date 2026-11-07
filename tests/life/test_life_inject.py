"""Unit tests for the combined prompt hook."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.life.memory import inject as inject_module


class FakeInjector:
    def __init__(self, result: bool = True, explode: bool = False) -> None:
        self.result = result
        self.explode = explode
        self.calls = 0

    async def inject(self, event, req) -> bool:
        self.calls += 1
        if self.explode:
            raise RuntimeError("boom")
        req.system_prompt += "<x>"
        return self.result


def _service(memory=None, expression=None, jargon=None):
    return SimpleNamespace(memory=memory, expression=expression, jargon=jargon)


@pytest.mark.asyncio
async def test_returns_false_without_service(monkeypatch):
    monkeypatch.setattr(inject_module, "get_life_service", lambda: None)
    req = SimpleNamespace(prompt="hi", system_prompt="sys")

    assert await inject_module.inject_life_context(None, req) is False
    assert req.system_prompt == "sys"


@pytest.mark.asyncio
async def test_runs_both_injectors(monkeypatch):
    memory = FakeInjector()
    expression = FakeInjector()
    monkeypatch.setattr(
        inject_module,
        "get_life_service",
        lambda: _service(memory, expression),
    )
    req = SimpleNamespace(prompt="hi", system_prompt="sys")

    changed = await inject_module.inject_life_context(None, req)

    assert changed is True
    assert memory.calls == 1
    assert expression.calls == 1
    assert req.system_prompt == "sys<x><x>"


@pytest.mark.asyncio
async def test_runs_the_jargon_injector_too(monkeypatch):
    jargon = FakeInjector()
    monkeypatch.setattr(
        inject_module,
        "get_life_service",
        lambda: _service(None, None, jargon),
    )
    req = SimpleNamespace(prompt="hi", system_prompt="sys")

    changed = await inject_module.inject_life_context(None, req)

    assert changed is True
    assert jargon.calls == 1


@pytest.mark.asyncio
async def test_failure_in_one_injector_does_not_block_the_other(monkeypatch):
    memory = FakeInjector(explode=True)
    expression = FakeInjector()
    monkeypatch.setattr(
        inject_module,
        "get_life_service",
        lambda: _service(memory, expression),
    )
    req = SimpleNamespace(prompt="hi", system_prompt="sys")

    changed = await inject_module.inject_life_context(None, req)

    assert changed is True
    assert expression.calls == 1


@pytest.mark.asyncio
async def test_skips_disabled_injectors(monkeypatch):
    monkeypatch.setattr(
        inject_module,
        "get_life_service",
        lambda: _service(None, FakeInjector(result=False)),
    )
    req = SimpleNamespace(prompt="hi", system_prompt="sys")

    assert await inject_module.inject_life_context(None, req) is False
