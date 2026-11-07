"""Unit tests for plan-driven reply supplements (S5e)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from bot.core.message.components import At, Image, Plain, Reply
from bot.core.message.message_event_result import MessageChain
from bot.life.config import ReplyConfig
from bot.life.reply.supplement import (
    AtNode,
    EmojiNode,
    ReplyNode,
    ReplySupplementPlan,
    ResolvedSupplement,
    SupplementPart,
    SupplementResources,
    compose_supplement,
    generate_reply_supplement,
    message_from_row,
    parse_supplement_plan,
    resolve_supplement,
)

ROWS = [
    {
        "message_id": "m1",
        "sender_id": "u1",
        "sender_name": "小明",
        "plain_text": "看这个图",
        "components": [
            {"type": "image", "data": {"path": "/tmp/a.png"}},
            {"type": "image", "data": {"path": "/tmp/s1.gif", "sub_type": "emoji"}},
        ],
    },
    {
        "message_id": "m2",
        "sender_id": "u2",
        "sender_name": "小红",
        "plain_text": "哈哈",
        "components": [
            {"type": "image", "data": {"path": "/tmp/b.png"}},
            {"type": "image", "data": {"path": "/tmp/c.png"}},
            {"type": "image", "data": {"path": "/tmp/s2.gif", "sub_type": "emoji"}},
        ],
    },
]


def _body(text: str = "你好") -> MessageChain:
    return MessageChain(chain=[Plain(text)])


def _resources(**kwargs) -> SupplementResources:
    return SupplementResources.from_rows(ROWS, **kwargs)


def test_message_from_row_splits_pictures_and_stickers() -> None:
    message = message_from_row(ROWS[0])

    assert message.pictures == ["/tmp/a.png"]
    assert message.emojis == ["/tmp/s1.gif"]
    assert message.sender_name == "小明"


def test_parse_plan_accepts_fenced_json() -> None:
    payload = {
        "msg_id": "m1",
        "parts": [{"type": "at", "msg_id": "m1"}, {"type": "reply"}],
    }

    plan = parse_supplement_plan(
        "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    )

    assert plan.msg_id == "m1"
    assert isinstance(plan.parts[0], AtNode)
    assert isinstance(plan.parts[1], ReplyNode)


def test_parse_plan_rejects_broken_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_supplement_plan("not json")


def test_plan_requires_exactly_one_reply_placeholder() -> None:
    with pytest.raises(ValueError, match="exactly one reply"):
        ReplySupplementPlan(parts=[{"type": "at", "msg_id": "m1"}])
    with pytest.raises(ValueError, match="exactly one reply"):
        ReplySupplementPlan(
            parts=[{"type": "reply"}, {"type": "reply"}],
        )


def test_emoji_node_rejects_mixed_or_empty_references() -> None:
    with pytest.raises(ValueError, match="must not carry source"):
        EmojiNode(type="emoji", source="m1", emotion="开心")
    with pytest.raises(ValueError, match="requires a message source"):
        EmojiNode(type="emoji")
    with pytest.raises(ValueError, match="must not be blank"):
        EmojiNode(type="emoji", emotion="  ")


@pytest.mark.asyncio
async def test_resolve_plan_resolves_mentions_pictures_and_stickers() -> None:
    plan = ReplySupplementPlan(
        msg_id="m2",
        parts=[
            {"type": "at", "msg_id": "m1"},
            {"type": "reply"},
            {"type": "pic", "source": "m2", "index": 1},
            {"type": "emoji", "source": "m1", "index": 0},
        ],
    )

    resolved = await resolve_supplement(plan, _resources())

    assert resolved.quote_id == "m2"
    assert [part.kind for part in resolved.parts] == ["at", "reply", "pic", "emoji"]
    assert resolved.parts[2].payload == "/tmp/c.png"
    assert resolved.parts[3].payload == "/tmp/s1.gif"


@pytest.mark.asyncio
async def test_resolve_plan_rejects_unknown_reference_and_out_of_range() -> None:
    plan = ReplySupplementPlan(
        parts=[{"type": "reply"}, {"type": "pic", "source": "m1", "index": 5}],
    )

    with pytest.raises(ValueError, match="out of range"):
        await resolve_supplement(plan, _resources())

    bad = ReplySupplementPlan(
        parts=[{"type": "reply"}, {"type": "at", "msg_id": "missing"}],
    )
    with pytest.raises(ValueError, match="unknown or ambiguous"):
        await resolve_supplement(bad, _resources())


@pytest.mark.asyncio
async def test_resolve_plan_honours_scene_switches() -> None:
    plan = ReplySupplementPlan(
        msg_id="m1",
        parts=[{"type": "reply"}, {"type": "at", "msg_id": "m1"}],
    )

    with pytest.raises(ValueError, match="mentions are disabled"):
        await resolve_supplement(plan, _resources(allow_at=False))
    with pytest.raises(ValueError, match="quoting is disabled"):
        await resolve_supplement(
            ReplySupplementPlan(msg_id="m1", parts=[{"type": "reply"}]),
            _resources(allow_quote=False),
        )


@pytest.mark.asyncio
async def test_semantic_sticker_is_picked_and_missing_pick_is_dropped() -> None:
    plan = ReplySupplementPlan(
        parts=[{"type": "reply"}, {"type": "emoji", "emotion": "开心"}],
    )

    async def pick(session_key: str, query: str):
        assert session_key == "session"
        assert "开心" in query
        return {"file_path": "/tmp/picked.gif"}

    resolved = await resolve_supplement(
        plan, _resources(session_key="session"), pick_emoji=pick
    )
    assert [part.kind for part in resolved.parts] == ["reply", "emoji"]
    assert resolved.parts[1].payload == "/tmp/picked.gif"

    async def miss(session_key: str, query: str):
        return None

    dropped = await resolve_supplement(
        plan, _resources(session_key="session"), pick_emoji=miss
    )
    assert [part.kind for part in dropped.parts] == ["reply"]


def test_compose_merges_mentions_and_sends_attachments_separately() -> None:
    resolved = ResolvedSupplement(
        quote_id="m2",
        parts=[
            SimpleNamespace(kind="at", payload=At(qq="u1", name="小明")),
            SimpleNamespace(kind="reply"),
            SimpleNamespace(kind="pic", payload="/tmp/a.png"),
            SimpleNamespace(kind="emoji", payload="/tmp/s1.gif"),
        ],
    )

    chains = compose_supplement(resolved, _body())

    assert len(chains) == 3
    assert isinstance(chains[0].chain[0], Reply)
    assert isinstance(chains[0].chain[1], At)
    assert any(isinstance(comp, Plain) for comp in chains[0].chain)
    assert isinstance(chains[1].chain[0], Image)
    assert isinstance(chains[2].chain[0], Image)


def test_compose_attaches_trailing_mention_to_the_body() -> None:
    resolved = ResolvedSupplement(
        parts=[
            SimpleNamespace(kind="reply"),
            SimpleNamespace(kind="at", payload=At(qq="u2", name="小红")),
        ],
    )

    chains = compose_supplement(resolved, _body())

    assert len(chains) == 1
    assert any(isinstance(comp, At) for comp in chains[0].chain)


def test_compose_requires_the_reply_placeholder() -> None:
    with pytest.raises(ValueError, match="no reply placeholder"):
        compose_supplement(ResolvedSupplement(parts=[]), _body())


def test_resolved_supplement_round_trips_through_extra() -> None:
    resolved = ResolvedSupplement(
        quote_id="m2",
        parts=[
            SupplementPart("at", At(qq="u1", name="小明")),
            SupplementPart("reply"),
            SupplementPart("pic", "/tmp/a.png"),
        ],
    )

    restored = ResolvedSupplement.from_extra(resolved.to_extra())

    assert restored is not None
    assert restored.quote_id == "m2"
    assert [part.kind for part in restored.parts] == ["at", "reply", "pic"]
    assert isinstance(restored.parts[0].payload, At)


class _FakeProvider:
    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.calls: list[str] = []

    async def text_chat(self, prompt: str, system_prompt: str = "", **_: object):
        self.calls.append(prompt)
        return SimpleNamespace(completion_text=self._replies.pop(0))


@pytest.mark.asyncio
async def test_generate_supplement_returns_plan_on_first_try() -> None:
    plan = json.dumps(
        {"parts": [{"type": "reply"}, {"type": "at", "msg_id": "m1"}]},
        ensure_ascii=False,
    )
    provider = _FakeProvider(["```json\n" + plan + "\n```"])

    result = await generate_reply_supplement(provider, _resources())

    assert result is not None
    assert len(result.parts) == 2
    assert len(provider.calls) == 1
    assert "m1" in provider.calls[0]


@pytest.mark.asyncio
async def test_generate_supplement_retries_once_with_the_error() -> None:
    valid = json.dumps(
        {"parts": [{"type": "reply"}]},
        ensure_ascii=False,
    )
    provider = _FakeProvider(["oops", valid])

    result = await generate_reply_supplement(provider, _resources())

    assert result is not None
    assert len(provider.calls) == 2
    assert "上次输出校验失败" in provider.calls[1]


@pytest.mark.asyncio
async def test_generate_supplement_gives_up_after_the_retry() -> None:
    provider = _FakeProvider(["oops", "still oops"])

    assert await generate_reply_supplement(provider, _resources()) is None


@pytest.mark.asyncio
async def test_generate_supplement_without_provider_is_none() -> None:
    assert await generate_reply_supplement(None, _resources()) is None


def test_reply_config_exposes_supplement_defaults() -> None:
    config = ReplyConfig()

    assert config.supplement_enabled is False
    assert config.supplement_max_parts == 8
    assert config.supplement_allow_at is True
    assert config.supplement_retry_once is True


@pytest.mark.asyncio
async def test_compose_keeps_body_metadata() -> None:
    body = MessageChain(chain=[Plain("hi")], use_t2i_=True, use_markdown_=False)
    resolved = ResolvedSupplement(parts=[SimpleNamespace(kind="reply")])

    chains = compose_supplement(resolved, body)

    assert chains[0].use_t2i_ is True
    assert chains[0].use_markdown_ is False


class _FakeStore:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def fetch_range(self, session_key: str, limit: int = 24):
        assert session_key
        return self._rows


class _FakeProviderManager:
    def __init__(self, provider) -> None:
        self._provider = provider

    async def get_using_provider_async(self, kind):
        return self._provider


class _FakeService:
    def __init__(self, provider, rows: list[dict]) -> None:
        self.config = SimpleNamespace(
            reply=ReplyConfig(
                supplement_enabled=True,
                segment_delay_seconds=0.0,
            )
        )
        self.store = _FakeStore(rows)
        self.star_context = SimpleNamespace(
            provider_manager=_FakeProviderManager(provider)
        )
        self.emoji = None


class _FakePipelineEvent:
    def __init__(self, extra: dict | None = None) -> None:
        self.unified_msg_origin = "aiocqhttp:GroupMessage:1"
        self._extra = dict(extra or {})
        self.sent: list = []

    def get_extra(self, key: str, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key: str, value) -> None:
        self._extra[key] = value

    def get_group_id(self):
        return "1"

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    async def send(self, chain) -> None:
        self.sent.append(chain)


class _FakeProfilePersona:
    async def profile_for(self, persona_id: str) -> dict:
        return {}


@pytest.mark.asyncio
async def test_plan_reply_supplement_builds_payload() -> None:
    from bot.life.pipeline import _plan_reply_supplement

    plan_json = json.dumps(
        {
            "msg_id": "m2",
            "parts": [{"type": "reply"}, {"type": "pic", "source": "m1", "index": 0}],
        },
        ensure_ascii=False,
    )
    service = _FakeService(_FakeProvider([plan_json]), ROWS)

    payload = await _plan_reply_supplement(service, _FakePipelineEvent(), "看看")

    assert payload is not None
    assert payload["quote_id"] == "m2"
    assert [item["kind"] for item in payload["parts"]] == ["reply", "pic"]
    assert payload["parts"][1]["path"] == "/tmp/a.png"


@pytest.mark.asyncio
async def test_plan_reply_supplement_returns_none_without_provider() -> None:
    from bot.life.pipeline import _plan_reply_supplement

    service = _FakeService(None, ROWS)

    assert await _plan_reply_supplement(service, _FakePipelineEvent(), "看看") is None


@pytest.mark.asyncio
async def test_postprocess_sends_supplement_messages_after_the_body() -> None:
    from bot.life.reply.postprocess import ReplyPostProcessor

    processor = ReplyPostProcessor(
        ReplyConfig(
            enabled=True,
            supplement_enabled=True,
            segment_delay_seconds=0.0,
        ),
        persona=_FakeProfilePersona(),
    )
    event = _FakePipelineEvent(
        {
            "_life_reply_supplement": {
                "quote_id": None,
                "parts": [{"kind": "reply"}, {"kind": "pic", "path": "/tmp/a.png"}],
            }
        }
    )
    result = SimpleNamespace(
        chain=[Plain("你好")],
        is_llm_result=lambda: True,
        derive=lambda chain: MessageChain(chain=list(chain)),
    )

    await processor.process(event, result)
    await asyncio.sleep(0.05)

    assert [comp.text for comp in result.chain if isinstance(comp, Plain)] == ["你好"]
    assert len(event.sent) == 1
    assert isinstance(event.sent[0].chain[0], Image)
