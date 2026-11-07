"""Unit tests for prompt assembly: dedupe, per-block caps and the shared budget."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.life.config import PromptConfig
from bot.life.memory import inject as inject_module
from bot.life.prompt import PromptAssembler


def _block(name: str, body: str) -> str:
    return f"<life_{name}>{body}</life_{name}>"


@pytest.fixture
def assembler() -> PromptAssembler:
    return PromptAssembler(PromptConfig())


def test_prompt_without_blocks_is_untouched(assembler):
    prompt = "You are a helpful assistant."

    assert assembler.assemble(prompt) == prompt
    assert assembler.status()["counters"]["assemblies"] == 0


def test_disabled_assembler_is_a_noop():
    prompt = _block("memory", "- 用户喜欢爬山")

    assert PromptAssembler(PromptConfig(enabled=False)).assemble(prompt) == prompt


def test_blocks_keep_their_position_and_the_base_text(assembler):
    prompt = (
        _block("persona", "说话短。")
        + "\n\n"
        + _block("memory", "- 用户喜欢爬山")
        + "\n\n基础系统提示"
    )

    result = assembler.assemble(prompt)

    assert result.startswith("<life_persona>")
    assert result.endswith("基础系统提示")
    assert result.index("<life_persona>") < result.index("<life_memory>")


def test_an_identical_line_is_dropped_from_the_later_block(assembler):
    shared = "- 用户上周说周末去南山爬山"
    prompt = (
        _block("memory", shared) + "\n" + _block("focus", shared + "\n- 用户问了地铁")
    )

    result = assembler.assemble(prompt).count(shared)

    assert result == 1
    assert assembler.status()["counters"]["deduped_lines"] == 1


def test_short_lines_are_not_deduped(assembler):
    prompt = _block("memory", "- 晴") + "\n" + _block("focus", "- 晴")

    result = assembler.assemble(prompt)

    assert result.count("- 晴") == 2


def test_a_block_is_cut_at_a_line_boundary():
    assembler = PromptAssembler(PromptConfig(max_block_chars=20))
    body = "\n".join(
        ["- 第一行内容比较长一点点", "- 第二行内容也比较长一点", "- 第三行"]
    )

    result = assembler.assemble(_block("memory", body))

    assert "<life_memory>" in result and "</life_memory>" in result
    assert "- 第一行内容比较长一点点" in result
    assert "- 第二行内容也比较长一点" not in result
    assert assembler.status()["counters"]["trimmed_blocks"] == 1


def test_over_budget_drops_the_least_important_block_first():
    assembler = PromptAssembler(PromptConfig(max_total_chars=200))
    prompt = "\n\n".join(
        [
            _block("persona", "人设" * 50),
            _block("memory", "记忆" * 50),
            _block("attention", "回钩" * 50),
        ]
    )

    result = assembler.assemble(prompt)

    assert "<life_attention>" not in result
    assert "<life_memory>" in result
    assert "<life_persona>" in result
    assert assembler.status()["last"]["dropped"] == ["attention"]


def test_persona_is_never_dropped_but_is_truncated():
    assembler = PromptAssembler(PromptConfig(max_total_chars=120))
    prompt = _block("persona", "\n".join(["人设" * 20] * 4))

    result = assembler.assemble(prompt)

    assert "<life_persona>" in result
    body = result.split("<life_persona>")[1].split("</life_persona>")[0]
    assert len(body) <= 120


def test_status_records_the_last_assembly(assembler):
    prompt = _block("persona", "说话短。") + "\n\n" + _block("memory", "- 用户喜欢爬山")

    assembler.assemble(prompt, umo="aiocqhttp:GroupMessage:1")

    last = assembler.status()["last"]
    assert last["umo"] == "aiocqhttp:GroupMessage:1"
    assert last["kept"] == ["persona", "memory"]
    assert last["dropped"] == []
    assert sum(item["chars"] for item in last["blocks"]) == last["total_chars"]


def test_unknown_blocks_are_dropped_before_known_ones():
    assembler = PromptAssembler(PromptConfig(max_total_chars=120))
    prompt = _block("persona", "人设") + "\n\n" + _block("custom", "自定义" * 40)

    result = assembler.assemble(prompt)

    assert "<life_custom>" not in result
    assert assembler.status()["last"]["dropped"] == ["custom"]


@pytest.mark.asyncio
async def test_the_hook_assembles_after_every_injector(monkeypatch):
    class BlockInjector:
        def __init__(self, name: str) -> None:
            self.name = name

        async def inject(self, event, req) -> bool:
            req.system_prompt += "\n\n" + _block(self.name, "- 重复的一行内容在这里")
            return True

    service = SimpleNamespace(
        persona=BlockInjector("persona"),
        memory=BlockInjector("memory"),
        prompt=PromptAssembler(PromptConfig(max_total_chars=200)),
    )
    monkeypatch.setattr(inject_module, "get_life_service", lambda: service)
    req = SimpleNamespace(prompt="hi", system_prompt="sys")
    event = SimpleNamespace(unified_msg_origin="aiocqhttp:GroupMessage:1")

    assert await inject_module.inject_life_context(event, req) is True

    assert req.system_prompt.count("- 重复的一行内容在这里") == 1
    assert service.prompt.status()["counters"]["deduped_lines"] == 1
