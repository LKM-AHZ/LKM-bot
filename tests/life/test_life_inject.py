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


def _service(
    memory=None,
    expression=None,
    jargon=None,
    focus=None,
    attention=None,
    persona=None,
    subagent=None,
):
    return SimpleNamespace(
        memory=memory,
        expression=expression,
        jargon=jargon,
        focus=focus,
        attention=attention,
        persona=persona,
        subagent=subagent,
    )


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


@pytest.mark.asyncio
async def test_runs_the_focus_injector_last(monkeypatch):
    focus = FakeInjector()
    monkeypatch.setattr(
        inject_module,
        "get_life_service",
        lambda: _service(jargon=FakeInjector(), focus=focus),
    )
    req = SimpleNamespace(prompt="hi", system_prompt="sys")

    changed = await inject_module.inject_life_context(None, req)

    assert changed is True
    assert focus.calls == 1
    assert req.system_prompt == "sys<x><x>"


@pytest.mark.asyncio
async def test_persona_runs_before_every_other_block(monkeypatch):
    order: list[str] = []

    class Ordered(FakeInjector):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

        async def inject(self, event, req) -> bool:
            order.append(self.name)
            return await super().inject(event, req)

    monkeypatch.setattr(
        inject_module,
        "get_life_service",
        lambda: _service(
            memory=Ordered("memory"),
            expression=Ordered("expression"),
            jargon=Ordered("jargon"),
            focus=Ordered("focus"),
            attention=Ordered("attention"),
            persona=Ordered("persona"),
            subagent=Ordered("subagent"),
        ),
    )
    req = SimpleNamespace(prompt="hi", system_prompt="sys")

    assert await inject_module.inject_life_context(None, req) is True

    assert order == list(inject_module.INJECTOR_ORDER)
    assert order[0] == "persona"


@pytest.mark.asyncio
async def test_missing_persona_attribute_is_tolerated(monkeypatch):
    memory = FakeInjector()
    monkeypatch.setattr(
        inject_module,
        "get_life_service",
        lambda: SimpleNamespace(memory=memory),
    )
    req = SimpleNamespace(prompt="hi", system_prompt="sys")

    assert await inject_module.inject_life_context(None, req) is True
    assert memory.calls == 1


@pytest.mark.asyncio
async def test_runs_the_attention_injector_last(monkeypatch):
    attention = FakeInjector()
    monkeypatch.setattr(
        inject_module,
        "get_life_service",
        lambda: _service(focus=FakeInjector(), attention=attention),
    )
    req = SimpleNamespace(prompt="hi", system_prompt="sys")

    changed = await inject_module.inject_life_context(None, req)

    assert changed is True
    assert attention.calls == 1
    assert req.system_prompt == "sys<x><x>"


class FakeTrace:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, action: str, **fields) -> None:
        self.records.append((action, fields))


@pytest.mark.asyncio
async def test_records_the_request_snapshot_on_the_trace(monkeypatch):
    monkeypatch.setattr(inject_module, "get_life_service", lambda: _service())
    trace = FakeTrace()
    event = SimpleNamespace(
        trace=trace,
        unified_msg_origin="aiocqhttp:GroupMessage:1",
    )
    req = SimpleNamespace(
        prompt="hi",
        system_prompt="sys",
        model="m1",
        func_tool=SimpleNamespace(names=lambda: ["search"]),
    )

    await inject_module.inject_life_context(event, req)

    action, fields = trace.records[0]
    assert action == "life_request"
    assert fields["prompt"] == "hi"
    assert fields["system_prompt"] == "sys"
    assert fields["tools"] == ["search"]
