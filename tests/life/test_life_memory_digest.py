"""Unit tests for the digest write chain and the MaiBot memory importer."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import MemoryDigestConfig
from bot.life.memory.digest import DigestBuilder
from bot.life.memory.importer import MemoryImporter
from bot.life.memory.store import MemoryStore

SESSION = "aiocqhttp:GroupMessage:1"


class FakeSource:
    def __init__(self, messages):
        self.messages = messages

    async def fetch_range(self, session_key, since_ts=0, until_ts=None, limit=200):
        return [row for row in self.messages if row["ts"] > since_ts][:limit]

    async def active_sessions(self, since_ts=0, limit=20):
        return sorted({row["session_key"] for row in self.messages})


def _messages(count: int, start: int, gap: int = 60) -> list[dict]:
    return [
        {
            "id": index + 1,
            "session_key": SESSION,
            "ts": start + index * gap,
            "sender_name": "Alice" if index % 2 == 0 else "Bob",
            "plain_text": f"第 {index} 条消息，讨论迁移计划",
        }
        for index in range(count)
    ]


@pytest_asyncio.fixture
async def store(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    memory = MemoryStore(database)
    await memory.ensure_schema()
    yield memory
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_digest_builds_summaries_and_advances_cursor(store):
    now = int(time.time())
    messages = _messages(18, start=now - 7200, gap=60)
    builder = DigestBuilder(
        source=FakeSource(messages),
        target=store,
        config=MemoryDigestConfig(
            min_messages=5,
            max_messages=60,
            gap_minutes=10,
            use_llm=False,
        ),
    )

    result = await builder.digest_session(SESSION)

    assert result["summaries"] >= 1
    assert await store.cursor(SESSION) == messages[-1]["ts"]
    stats = await store.stats()
    assert stats["by_kind"].get("summary", 0) >= 1


@pytest.mark.asyncio
async def test_digest_skips_when_too_few_messages(store):
    now = int(time.time())
    builder = DigestBuilder(
        source=FakeSource(_messages(2, start=now - 600)),
        target=store,
        config=MemoryDigestConfig(min_messages=8, use_llm=False),
    )

    result = await builder.digest_session(SESSION)

    assert result["skipped"] is True
    assert await store.cursor(SESSION) == 0


@pytest.mark.asyncio
async def test_digest_is_idempotent_on_second_round(store):
    now = int(time.time())
    builder = DigestBuilder(
        source=FakeSource(_messages(12, start=now - 5400)),
        target=store,
        config=MemoryDigestConfig(
            min_messages=4,
            max_messages=60,
            gap_minutes=10,
            use_llm=False,
        ),
    )

    first = await builder.digest_session(SESSION)
    second = await builder.digest_session(SESSION)

    assert first["summaries"] >= 1
    assert second.get("skipped") is True


@pytest.mark.asyncio
async def test_digest_with_summarizer_produces_episode(store):
    now = int(time.time())
    messages = _messages(30, start=now - 14400, gap=60)
    prompts: list[str] = []

    async def summarizer(system_prompt: str, user_prompt: str) -> str:
        prompts.append(user_prompt)
        return f"摘要 {len(prompts)}"

    builder = DigestBuilder(
        source=FakeSource(messages),
        target=store,
        config=MemoryDigestConfig(
            min_messages=4,
            max_messages=40,
            gap_minutes=5,
            use_llm=True,
        ),
        summarizer=summarizer,
    )

    result = await builder.digest_session(SESSION)

    assert result["summaries"] >= 2
    assert result["episodes"] >= 1
    assert prompts


@pytest.mark.asyncio
async def test_importer_reads_jsonl_and_dry_run(store, tmp_path):
    path = tmp_path / "memory.jsonl"
    rows = [
        {"content": "用户喜欢火锅", "ts": 1000, "person": "alice"},
        {"text": "用户在研究记忆检索", "time": 2000},
        {"content": ""},
    ]
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )
    importer = MemoryImporter(store)

    dry = await importer.import_path(path, SESSION, dry_run=True)
    real = await importer.import_path(path, SESSION, dry_run=False)

    assert dry["rows_seen"] == 3
    assert dry["skipped"] == 1
    assert real["imported"] == 2
    assert (await store.stats())["total"] == 2


@pytest.mark.asyncio
async def test_importer_reads_sqlite_tables(store, tmp_path):
    path = tmp_path / "maibot.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE memories (content TEXT, created_at INTEGER)")
    connection.execute("INSERT INTO memories VALUES ('数据库里的记忆', 3000)")
    connection.execute("CREATE TABLE unrelated (name TEXT)")
    connection.execute("INSERT INTO unrelated VALUES ('skip me')")
    connection.commit()
    connection.close()

    result = await MemoryImporter(store).import_path(path, SESSION, dry_run=False)

    assert result["imported"] == 1
    exported = await store.export(SESSION)
    assert exported[0]["content"] == "数据库里的记忆"
