"""Unit tests for jargon learning: definition patterns, LLM path and cursor."""

from __future__ import annotations

import json
import time

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import JargonConfig
from bot.life.jargon.learn import JargonLearner
from bot.life.jargon.store import JargonStore

SESSION = "aiocqhttp:GroupMessage:1"


class FakeSource:
    def __init__(self, messages):
        self.messages = messages

    async def fetch_range(self, session_key, since_ts=0, until_ts=None, limit=200):
        return [row for row in self.messages if row["ts"] > since_ts][:limit]

    async def active_sessions(self, since_ts=0, limit=20):
        return sorted({row["session_key"] for row in self.messages})


def _message(text: str, ts: int) -> dict:
    return {
        "session_key": SESSION,
        "plain_text": text,
        "ts": ts,
        "group_id": "1",
        "sender_name": "Alice",
    }


@pytest_asyncio.fixture
async def store(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    jargon = JargonStore(database, JargonConfig(enabled=True))
    await jargon.ensure_schema()
    yield jargon
    await database.engine.dispose()


def _learner(store, messages, **overrides):
    config = JargonConfig(enabled=True, use_llm=False, **overrides)
    return JargonLearner(source=FakeSource(messages), target=store, config=config)


def test_mine_extracts_definition_patterns(store):
    now = int(time.time())
    messages = [
        _message("绷不住 就是想笑但憋着", now),
        _message("「典」就是很典型的例子", now + 1),
        _message("人工智能，简称 AI", now + 2),
    ]
    learner = _learner(store, messages)

    terms = {row["term"]: row["meaning"] for row in learner.mine(messages)}

    assert terms["绷不住"] == "想笑但憋着"
    assert terms["典"] == "很典型的例子"
    assert terms["AI"] == "人工智能"


def test_mine_drops_stopwords_digits_and_self_reference(store):
    now = int(time.time())
    messages = [
        _message("这个就是懒", now),
        _message("123 就是数字", now + 1),
        _message("绷不住 就是绷不住的意思", now + 2),
    ]
    learner = _learner(store, messages)

    assert learner.mine(messages) == []


def test_mine_respects_term_length_bounds(store):
    now = int(time.time())
    messages = [_message("这是一个特别长的词组 就是太长了", now + 1)]
    learner = _learner(store, messages, max_term_chars=4)

    assert learner.mine(messages) == []


@pytest.mark.asyncio
async def test_learn_session_writes_and_advances_cursor(store):
    now = int(time.time())
    messages = [_message("绷不住 就是想笑但憋着", now - 10)]
    learner = _learner(store, messages)

    result = await learner.learn_session(SESSION)

    assert result["inserted"] == 1
    assert await store.cursor(SESSION) == now - 10
    row = await store.get_by_term(SESSION, "绷不住")
    assert row["source"] == "heuristic"
    assert row["status"] == "active"


@pytest.mark.asyncio
async def test_learn_session_is_incremental(store):
    now = int(time.time())
    messages = [_message("绷不住 就是想笑但憋着", now - 10)]
    learner = _learner(store, messages)

    await learner.learn_session(SESSION)
    second = await learner.learn_session(SESSION)

    assert second["skipped"] is True


@pytest.mark.asyncio
async def test_llm_path_adds_terms(store):
    now = int(time.time())
    messages = [_message("今天又加班到十一点", now)]

    async def summarizer(system_prompt: str, user_prompt: str) -> str:
        return json.dumps(
            [{"term": "加班", "meaning": "在本群里特指自愿留下来改 bug"}],
            ensure_ascii=False,
        )

    learner = JargonLearner(
        source=FakeSource(messages),
        target=store,
        config=JargonConfig(enabled=True, use_llm=True),
        summarizer=summarizer,
    )

    await learner.learn_session(SESSION)

    row = await store.get_by_term(SESSION, "加班")
    assert row is not None
    assert row["source"] == "llm"
    assert "自愿" in row["meaning"]


@pytest.mark.asyncio
async def test_llm_invalid_payload_is_ignored(store):
    now = int(time.time())
    messages = [_message("随便说说", now)]

    async def summarizer(system_prompt: str, user_prompt: str) -> str:
        return "不是 JSON"

    learner = JargonLearner(
        source=FakeSource(messages),
        target=store,
        config=JargonConfig(enabled=True, use_llm=True),
        summarizer=summarizer,
    )

    await learner.learn_session(SESSION)

    assert await store.list_terms(SESSION) == []
