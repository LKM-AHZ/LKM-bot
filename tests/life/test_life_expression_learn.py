"""Unit tests for expression learning: heuristics, LLM path and cursor."""

from __future__ import annotations

import json
import time

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import ExpressionConfig
from bot.life.expression.learn import ExpressionLearner
from bot.life.expression.store import ExpressionStore

SESSION = "aiocqhttp:GroupMessage:1"


class FakeSource:
    def __init__(self, messages):
        self.messages = messages

    async def fetch_range(self, session_key, since_ts=0, until_ts=None, limit=200):
        return [row for row in self.messages if row["ts"] > since_ts][:limit]

    async def active_sessions(self, since_ts=0, limit=20):
        return sorted({row["session_key"] for row in self.messages})


def _message(text: str, ts: int, group_id: str | None = "1") -> dict:
    return {
        "session_key": SESSION,
        "plain_text": text,
        "ts": ts,
        "group_id": group_id,
        "sender_name": "Alice",
    }


@pytest_asyncio.fixture
async def store(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    expressions = ExpressionStore(database)
    await expressions.ensure_schema()
    yield expressions
    await database.engine.dispose()


def _learner(store, messages, **overrides):
    config = ExpressionConfig(
        enabled=True,
        min_frequency=2,
        use_llm=False,
        **overrides,
    )
    return ExpressionLearner(
        source=FakeSource(messages),
        target=store,
        config=config,
    )


def test_mine_keeps_repeated_emphatic_phrases(store):
    now = int(time.time())
    messages = [
        _message("这个 bug 又来了 笑死", now),
        _message("笑死 又是它", now + 10),
        _message("今天天气不错", now + 20),
    ]
    learner = _learner(store, messages)

    candidates = learner.mine(messages)
    styles = [row["style"] for row in candidates]

    assert "笑死" in styles
    assert "今天天气不错" not in styles
    emphatic = next(row for row in candidates if row["style"] == "笑死")
    assert emphatic["weight"] == 2.0
    assert emphatic["situation"]


def test_mine_drops_noise_long_text_and_one_offs(store):
    now = int(time.time())
    messages = [
        _message("https://example.com/abc", now),
        _message("/help", now + 1),
        _message("这句话特别特别长而且完全没有语气词所以不应该被学成表达方式", now + 2),
        _message("确实", now + 3),
    ]
    learner = _learner(store, messages)

    candidates = learner.mine(messages)

    assert [row["style"] for row in candidates] == []


def test_mine_requires_frequency_for_plain_phrases(store):
    now = int(time.time())
    messages = [_message("收到", now), _message("收到", now + 1)]
    learner = _learner(store, messages)

    candidates = learner.mine(messages)

    # Not emphatic, but it repeats three times or more elsewhere; two is not enough.
    assert candidates == []


@pytest.mark.asyncio
async def test_learn_session_writes_and_advances_cursor(store):
    now = int(time.time())
    messages = [
        _message("笑死 这个也敢发", now - 20),
        _message("笑死 真的离谱", now - 10),
    ]
    learner = _learner(store, messages)

    result = await learner.learn_session(SESSION)

    assert result["candidates"] >= 1
    assert result["inserted"] >= 1
    assert await store.cursor(SESSION) == now - 10
    rows = await store.list_expressions(SESSION)
    assert rows and rows[0]["source"] == "heuristic"


@pytest.mark.asyncio
async def test_learn_session_is_incremental(store):
    now = int(time.time())
    messages = [
        _message("笑死 又来", now - 20),
        _message("笑死 真的", now - 10),
    ]
    learner = _learner(store, messages)

    await learner.learn_session(SESSION)
    second = await learner.learn_session(SESSION)

    assert second["skipped"] is True


@pytest.mark.asyncio
async def test_llm_path_adds_extra_expressions(store):
    now = int(time.time())
    messages = [_message("这操作真的看不懂", now)]

    async def summarizer(system_prompt: str, user_prompt: str) -> str:
        return json.dumps(
            [{"situation": "有人发迷惑操作", "style": "看不懂"}],
            ensure_ascii=False,
        )

    config = ExpressionConfig(enabled=True, min_frequency=5, use_llm=True)
    learner = ExpressionLearner(
        source=FakeSource(messages),
        target=store,
        config=config,
        summarizer=summarizer,
    )

    result = await learner.learn_session(SESSION)

    rows = await store.list_expressions(SESSION)
    assert result["inserted"] == 1
    assert rows[0]["source"] == "llm"
    assert rows[0]["situation"] == "有人发迷惑操作"


@pytest.mark.asyncio
async def test_llm_invalid_payload_is_ignored(store):
    now = int(time.time())
    messages = [_message("随便说说", now)]

    async def summarizer(system_prompt: str, user_prompt: str) -> str:
        return "这不是 JSON"

    config = ExpressionConfig(enabled=True, min_frequency=5, use_llm=True)
    learner = ExpressionLearner(
        source=FakeSource(messages),
        target=store,
        config=config,
        summarizer=summarizer,
    )

    await learner.learn_session(SESSION)

    assert await store.list_expressions(SESSION) == []
    assert learner.snapshot()["llm_calls"] == 1
