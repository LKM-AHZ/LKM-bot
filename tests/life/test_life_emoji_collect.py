"""Unit tests for sticker collection from the archive."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import EmojiConfig
from bot.life.emoji.collect import EmojiCollector
from bot.life.emoji.store import EmojiStore

SESSION = "aiocqhttp:GroupMessage:1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class FakeSource:
    def __init__(self, messages):
        self.messages = messages

    async def fetch_range(self, session_key, since_ts=0, until_ts=None, limit=200):
        return [row for row in self.messages if row["ts"] > since_ts][:limit]

    async def active_sessions(self, since_ts=0, limit=20):
        return sorted({row["session_key"] for row in self.messages})


def _message(refs: list[str], text: str, ts: int) -> dict:
    components = [{"type": "image", "data": {"file": ref, "url": ref}} for ref in refs]
    return {
        "session_key": SESSION,
        "plain_text": text,
        "ts": ts,
        "components": components,
    }


@pytest_asyncio.fixture
async def store(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    emoji = EmojiStore(database)
    await emoji.ensure_schema()
    yield emoji
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_collects_and_writes_one_file(store, tmp_path):
    now = int(time.time())
    messages = [_message(["https://example.com/a.png"], "笑死 这也行", now)]
    collector = EmojiCollector(
        source=FakeSource(messages),
        target=store,
        config=EmojiConfig(enabled=True),
        data_dir=tmp_path / "emoji",
        fetcher=_fetcher(PNG),
    )

    result = await collector.collect_session(SESSION)

    assert result["stored"] == 1
    row = await store.get_by_hash(SESSION, hashlib.sha256(PNG).hexdigest())
    assert row is not None
    assert Path(row["file_path"]).is_file()  # noqa: ASYNC240  # test assertion on a temp file
    assert row["mime"] == "image/png"
    assert row["tags"]
    assert row["caption"] == "笑死 这也行"
    assert await store.cursor(SESSION) == now


@pytest.mark.asyncio
async def test_same_bytes_are_stored_once(store, tmp_path):
    now = int(time.time())
    messages = [
        _message(["https://example.com/a.png"], "一", now),
        _message(["https://example.com/b.png"], "二", now + 1),
    ]
    collector = EmojiCollector(
        source=FakeSource(messages),
        target=store,
        config=EmojiConfig(enabled=True),
        data_dir=tmp_path / "emoji",
        fetcher=_fetcher(PNG),
    )

    result = await collector.collect_session(SESSION)

    assert result["stored"] == 1
    assert collector.snapshot()["duplicates"] == 1


@pytest.mark.asyncio
async def test_rejects_non_images_and_base64(store, tmp_path):
    now = int(time.time())
    messages = [
        _message(["https://example.com/text.png"], "文本", now),
        _message(["base64://<100 chars>"], "base64", now + 1),
    ]
    collector = EmojiCollector(
        source=FakeSource(messages),
        target=store,
        config=EmojiConfig(enabled=True),
        data_dir=tmp_path / "emoji",
        fetcher=_fetcher(b"<html>not an image</html>"),
    )

    result = await collector.collect_session(SESSION)

    assert result["stored"] == 0
    assert collector.snapshot()["skipped"] >= 1


@pytest.mark.asyncio
async def test_respects_max_bytes(store, tmp_path):
    now = int(time.time())
    messages = [_message(["https://example.com/big.png"], "大图", now)]
    collector = EmojiCollector(
        source=FakeSource(messages),
        target=store,
        config=EmojiConfig(enabled=True, max_bytes=8),
        data_dir=tmp_path / "emoji",
        fetcher=_fetcher(PNG),
    )

    result = await collector.collect_session(SESSION)

    assert result["stored"] == 0


@pytest.mark.asyncio
async def test_remote_disabled_skips_http(store, tmp_path):
    now = int(time.time())
    messages = [_message(["https://example.com/a.png"], "远程", now)]
    collector = EmojiCollector(
        source=FakeSource(messages),
        target=store,
        config=EmojiConfig(enabled=True, allow_remote=False),
        data_dir=tmp_path / "emoji",
    )

    result = await collector.collect_session(SESSION)

    assert result["stored"] == 0
    assert collector.snapshot()["failed"] == 1


@pytest.mark.asyncio
async def test_local_file_reference_is_read(store, tmp_path):
    now = int(time.time())
    source_file = tmp_path / "local.png"
    source_file.write_bytes(PNG)
    messages = [_message([source_file.as_uri()], "本地图", now)]
    collector = EmojiCollector(
        source=FakeSource(messages),
        target=store,
        config=EmojiConfig(enabled=True),
        data_dir=tmp_path / "emoji",
    )

    result = await collector.collect_session(SESSION)

    assert result["stored"] == 1


def _fetcher(payload: bytes):
    async def fetch(reference: str) -> bytes | None:
        return payload

    return fetch
