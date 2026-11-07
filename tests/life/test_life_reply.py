"""Unit tests for reply post-processing: Markdown, typos, length, split, quote."""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

import pytest

from bot.core.message.components import Plain, Reply
from bot.life.config import LifeConfig, ReplyConfig
from bot.life.reply.postprocess import ReplyPostProcessor


class FakeResult:
    def __init__(self, text: str = "在吗", llm: bool = True) -> None:
        self.chain = [Plain(text)]
        self._llm = llm

    def is_llm_result(self) -> bool:
        return self._llm

    def derive(self, chain):
        return SimpleNamespace(chain=list(chain))


class FakeEvent:
    def __init__(
        self,
        umo: str = "aiocqhttp:GroupMessage:1",
        group_id: str | None = "1",
        platform: str = "aiocqhttp",
        persona_id: str = "p1",
        message_id: str = "m1",
    ) -> None:
        self.unified_msg_origin = umo
        self._group_id = group_id
        self._platform = platform
        self._persona_id = persona_id
        self.message_obj = SimpleNamespace(message_id=message_id)
        self.sent: list[str] = []

    async def send(self, chain) -> None:
        for comp in chain.chain:
            self.sent.append(getattr(comp, "text", ""))

    def get_group_id(self):
        return self._group_id

    def get_platform_name(self) -> str:
        return self._platform

    def get_extra(self, key: str, default=None):
        return self._persona_id if key == "_trace_persona_id" else default


class FakePersona:
    def __init__(self, profile: dict | None) -> None:
        self.profile = profile
        self.calls = 0

    async def profile_for(self, persona_id: str):
        self.calls += 1
        return self.profile


def _processor(profile: dict | None = None, **overrides) -> ReplyPostProcessor:
    overrides.setdefault("segment_delay_seconds", 0)
    config = ReplyConfig(enabled=True, **overrides)
    life = LifeConfig(enabled=True)
    life.scope.mode = "all"
    return ReplyPostProcessor(
        config,
        life_config=life,
        persona=FakePersona(
            profile
            if profile is not None
            else {
                "allow_markdown": False,
                "reply_max_chars": 30,
                "typo_rate": 0.0,
                "multi_segment_prob": 0.0,
                "quote_rate": 0.0,
            }
        ),
        rng=random.Random(7),
    )


def test_strip_markdown_turns_syntax_into_plain_sentences():
    processor = _processor()
    text = (
        "## 标题\n\n"
        "- 第一点\n"
        "- 第二点\n\n"
        "**重点**和`代码`，还有[链接](https://example.com)。\n\n"
        "```python\nprint(1)\n```\n\n"
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    )

    result = processor.strip_markdown(text)

    for marker in ("##", "**", "`", "](", "|", "- "):
        assert marker not in result
    assert "第一点" in result
    assert "重点" in result
    assert "链接" in result


def test_strip_markdown_is_idempotent():
    processor = _processor()
    text = "今天天气不错，我们去爬山吧。"

    once = processor.strip_markdown(text)

    assert once == text
    assert processor.strip_markdown(once) == once


def test_a_typo_is_at_most_one_pair_swap_or_doubling():
    processor = _processor(rng=random.Random(3))
    text = "今天天气真的挺不错的"

    nudged = processor.nudge_typos(text, 1.0)

    assert nudged != text
    assert abs(len(nudged) - len(text)) <= 1
    assert sorted(nudged) == sorted(text) or len(nudged) == len(text) + 1


def test_zero_rate_never_touches_the_text():
    processor = _processor()

    assert processor.nudge_typos("今天天气真的挺不错的", 0.0) == "今天天气真的挺不错的"


def test_cap_length_cuts_on_a_sentence_boundary():
    processor = _processor()

    capped = processor.cap_length("第一句话。第二句话比较长。第三句话。", 8)

    assert capped == "第一句话。"


def test_cap_length_without_punctuation_hard_cuts():
    processor = _processor()

    assert processor.cap_length("没有标点的一长串文字", 4) == "没有标点"


def test_split_segments_groups_sentences_within_the_part_budget():
    processor = _processor()
    text = "第一句话比较长一点。第二句话也比较长。第三句话同样不短。第四句话。第五句话。第六句话。"

    parts = processor.split_segments(text, 1.0)

    assert len(parts) == 3
    assert "".join(parts) == text


def test_short_replies_are_never_split():
    processor = _processor()

    assert processor.split_segments("在吗", 1.0) == ["在吗"]


@pytest.mark.asyncio
async def test_plain_markdown_is_stripped_before_sending():
    processor = _processor()
    result = FakeResult("**好的**，我这就去。")

    await processor.process(FakeEvent(), result)

    assert result.chain[0].text == "好的，我这就去。"
    assert processor.status()["counters"]["markdown_stripped"] == 1


@pytest.mark.asyncio
async def test_markdown_is_kept_when_the_profile_allows_it():
    processor = _processor(
        {
            "allow_markdown": True,
            "reply_max_chars": 0,
            "typo_rate": 0.0,
            "multi_segment_prob": 0.0,
            "quote_rate": 0.0,
        }
    )
    result = FakeResult("**好的**")

    await processor.process(FakeEvent(), result)

    assert result.chain[0].text == "**好的**"


@pytest.mark.asyncio
async def test_over_long_replies_are_capped():
    processor = _processor(
        {
            "allow_markdown": False,
            "reply_max_chars": 6,
            "typo_rate": 0.0,
            "multi_segment_prob": 0.0,
            "quote_rate": 0.0,
        }
    )
    result = FakeResult("第一句话。第二句话。第三句话。")

    await processor.process(FakeEvent(), result)

    assert result.chain[0].text == "第一句话。"
    assert processor.status()["counters"]["capped"] == 1


@pytest.mark.asyncio
async def test_a_reply_is_split_when_upstream_segmentation_is_off():
    processor = _processor(
        {
            "allow_markdown": False,
            "reply_max_chars": 0,
            "typo_rate": 0.0,
            "multi_segment_prob": 1.0,
            "quote_rate": 0.0,
        }
    )
    event = FakeEvent()
    result = FakeResult("第一句话比较长一点。第二句话也比较长。第三句话同样不短。")

    await processor.process(event, result, upstream_segmented=False)
    await asyncio.sleep(0.01)

    assert len(result.chain) == 1
    assert result.chain[0].text == "第一句话比较长一点。"
    assert event.sent == ["第二句话也比较长。", "第三句话同样不短。"]
    assert processor.status()["counters"]["split"] == 1


@pytest.mark.asyncio
async def test_split_is_skipped_when_upstream_already_splits():
    processor = _processor(
        {
            "allow_markdown": False,
            "reply_max_chars": 0,
            "typo_rate": 0.0,
            "multi_segment_prob": 1.0,
            "quote_rate": 0.0,
        }
    )
    result = FakeResult("第一句话。第二句话。第三句话。")

    await processor.process(FakeEvent(), result, upstream_segmented=True)

    assert len(result.chain) == 1


@pytest.mark.asyncio
async def test_probability_quote_is_inserted_on_a_supported_platform():
    processor = _processor(
        {
            "allow_markdown": False,
            "reply_max_chars": 0,
            "typo_rate": 0.0,
            "multi_segment_prob": 0.0,
            "quote_rate": 1.0,
        }
    )
    result = FakeResult("好的")

    await processor.process(FakeEvent(), result)

    assert isinstance(result.chain[0], Reply)
    assert result.chain[0].id == "m1"
    assert processor.status()["counters"]["quoted"] == 1


@pytest.mark.asyncio
async def test_quote_is_not_added_twice():
    processor = _processor(
        {
            "allow_markdown": False,
            "reply_max_chars": 0,
            "typo_rate": 0.0,
            "multi_segment_prob": 0.0,
            "quote_rate": 1.0,
        }
    )
    result = FakeResult("好的")
    result.chain.insert(0, Reply(id="m0"))

    await processor.process(FakeEvent(), result)

    assert [comp for comp in result.chain if isinstance(comp, Reply)] == result.chain[
        :1
    ]
    assert processor.status()["counters"]["quoted"] == 0


@pytest.mark.asyncio
async def test_recent_ring_records_the_last_replies():
    processor = _processor()
    result = FakeResult("**好的**，我这就去。")

    await processor.process(FakeEvent(), result)

    recent = processor.status()["recent"]
    assert len(recent) == 1
    assert recent[0]["chars"] == len("好的，我这就去。")
    assert recent[0]["segments"] == 1
    assert recent[0]["markdown"] is True
    assert recent[0]["leak"] is False


@pytest.mark.asyncio
async def test_recent_ring_is_bounded():
    processor = _processor()
    for _ in range(60):
        await processor.process(FakeEvent(), FakeResult("好的。"))

    assert len(processor.status()["recent"]) == 50


@pytest.mark.asyncio
async def test_sessions_outside_the_allowlist_are_skipped():
    life = LifeConfig(enabled=True)
    life.scope.mode = "allowlist"
    processor = ReplyPostProcessor(
        ReplyConfig(enabled=True),
        life_config=life,
        persona=FakePersona({"reply_max_chars": 10}),
        rng=random.Random(7),
    )
    result = FakeResult("**很长的一段回复**")

    await processor.process(FakeEvent(umo="other:GroupMessage:9"), result)

    assert result.chain[0].text == "**很长的一段回复**"
    assert processor.status()["counters"]["skipped"] == 1


@pytest.mark.asyncio
async def test_non_llm_results_are_left_alone():
    processor = _processor()
    result = FakeResult("**命令输出**", llm=False)

    await processor.process(FakeEvent(), result)

    assert result.chain[0].text == "**命令输出**"
    assert processor.status()["counters"]["processed"] == 0
