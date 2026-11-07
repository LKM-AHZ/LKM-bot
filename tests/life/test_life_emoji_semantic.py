"""Unit tests for intent-based sticker matching (VLM tag axes, MaiBot 1.4)."""

from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import EmojiConfig, MemoryEmbeddingConfig
from bot.life.emoji.semantic import (
    CONTENT,
    EMOTION,
    EmojiTags,
    extract_tags,
    parse_tags,
    score_axes,
)
from bot.life.emoji.service import EmojiService
from bot.life.emoji.store import EmojiStore
from bot.life.memory.store import pack_vector

SESSION = "aiocqhttp:GroupMessage:1"


@pytest_asyncio.fixture
async def store(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    emoji = EmojiStore(database)
    await emoji.ensure_schema()
    yield emoji, database
    await database.engine.dispose()


def _row(digest: str, path: str = "/tmp/a.png", **overrides) -> dict:
    now = int(time.time())
    return {
        "session_key": SESSION,
        "sha256": digest,
        "file_path": path,
        "mime": "image/png",
        "size_bytes": 100,
        "tags": ["笑"],
        "caption": "笑死",
        "created_at": now,
        "updated_at": now,
        **overrides,
    }


class _FakeProvider:
    def __init__(self, text: str | None) -> None:
        self.text = text
        self.image_urls: list[str] | None = None

    async def text_chat(self, prompt: str, image_urls=None, system_prompt="", **_):
        self.image_urls = image_urls
        return SimpleNamespace(completion_text=self.text)


class _FakeEmbedding:
    available = True

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping

    def snapshot(self) -> dict:
        return {"model": "fake-embedding"}

    async def embed_one(self, text: str):
        return self.mapping.get(text)


def test_tags_normalize_dedupe_and_strip() -> None:
    tags = EmojiTags(
        emotion_tags=[" 开心 ", "开心", ""],
        content_tags=["猫", "猫", " 点赞 "],
    )

    assert tags.emotion_tags == ["开心"]
    assert tags.content_tags == ["猫", "点赞"]
    assert tags.axis_texts() == {"content": "猫、点赞", "emotion": "开心"}


def test_tags_require_content_and_cap_sizes() -> None:
    with pytest.raises(ValueError):
        EmojiTags(emotion_tags=[], content_tags=[])
    with pytest.raises(ValueError):
        EmojiTags(content_tags=["a", "b", "c", "d", "e", "f"])


def test_parse_tags_accepts_fenced_json() -> None:
    tags = parse_tags(
        '```json\n{"emotion_tags": ["无语"], "content_tags": ["问号"]}\n```'
    )

    assert tags.axis_texts() == {"content": "问号", "emotion": "无语"}


def test_parse_tags_rejects_broken_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_tags("oops")


@pytest.mark.asyncio
async def test_extract_tags_uses_the_vision_input() -> None:
    provider = _FakeProvider('{"emotion_tags": ["开心"], "content_tags": ["猫"]}')

    tags = await extract_tags(provider, "/tmp/a.png")

    assert tags is not None
    assert tags.content_tags == ["猫"]
    assert provider.image_urls == ["/tmp/a.png"]


@pytest.mark.asyncio
async def test_extract_tags_soft_fails_without_provider_or_text() -> None:
    assert await extract_tags(None, "/tmp/a.png") is None
    assert await extract_tags(_FakeProvider(None), "/tmp/a.png") is None
    assert await extract_tags(_FakeProvider("oops"), "/tmp/a.png") is None


def test_score_axes_orders_and_filters_by_axis() -> None:
    rows = [
        {"axis": EMOTION, "axis_vector": pack_vector([1.0, 0.0]), "id": 1},
        {"axis": CONTENT, "axis_vector": pack_vector([0.0, 1.0]), "id": 2},
        {"axis": CONTENT, "axis_vector": pack_vector([0.7, 0.7]), "id": 3},
    ]

    scored = score_axes([1.0, 0.0], rows)

    assert [row["id"] for _, row in scored] == [1, 3, 2]
    assert [row["id"] for _, row in score_axes([1.0, 0.0], rows, axis=CONTENT)] == [
        3,
        2,
    ]
    assert score_axes([1.0, 0.0], []) == []


@pytest.mark.asyncio
async def test_axis_round_trip_and_pending_filter(store) -> None:
    emoji_store, _ = store
    digest = hashlib.sha256(b"a").hexdigest()
    await emoji_store.insert_many([_row(digest)])

    pending = await emoji_store.pending_axes()
    assert [row["sha256"] for row in pending] == [digest]

    await emoji_store.upsert_axis(
        SESSION, digest, EMOTION, "开心", pack_vector([1.0, 0.0]), "m", 2
    )
    await emoji_store.upsert_axis(
        SESSION, digest, CONTENT, "猫", pack_vector([0.0, 1.0]), "m", 2
    )

    assert await emoji_store.pending_axes() == []
    axes = await emoji_store.active_axes(SESSION)
    assert sorted(row["axis"] for row in axes) == [CONTENT, EMOTION]
    assert all(row["sha256"] == digest for row in axes)


@pytest_asyncio.fixture
async def service(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    config = EmojiConfig(
        enabled=True,
        semantic_enabled=True,
        semantic_backfill_limit=5,
        cooldown_seconds=0,
    )
    context = SimpleNamespace(provider_manager=None)
    emoji_service = EmojiService(
        context=context,
        config=config,
        db_helper=database,
        data_dir=tmp_path,
        embedding_config=MemoryEmbeddingConfig(enabled=False),
    )
    await emoji_service.initialize()
    yield emoji_service, database
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_index_tag_axes_labels_and_embeds_each_axis(service) -> None:
    emoji_service, _ = service
    digest = hashlib.sha256(b"a").hexdigest()
    await emoji_service.store.insert_many([_row(digest)])
    emoji_service.embedding = _FakeEmbedding(
        {"开心": [1.0, 0.0], "猫": [0.0, 1.0]}
    )
    provider = _FakeProvider('{"emotion_tags": ["开心"], "content_tags": ["猫"]}')
    emoji_service.context.provider_manager = SimpleNamespace(
        get_using_provider_async=lambda *_: _await(provider)
    )

    indexed = await emoji_service.index_tag_axes()

    assert indexed == 1
    axes = await emoji_service.store.active_axes(SESSION)
    assert {row["axis"]: row["axis_text"] for row in axes} == {
        EMOTION: "开心",
        CONTENT: "猫",
    }
    assert emoji_service._counters["tagged"] == 1


@pytest.mark.asyncio
async def test_index_tag_axes_is_off_by_default(tmp_path) -> None:
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    emoji_service = EmojiService(
        context=SimpleNamespace(),
        config=EmojiConfig(enabled=True),
        db_helper=database,
        data_dir=tmp_path,
        embedding_config=MemoryEmbeddingConfig(enabled=False),
    )
    await emoji_service.initialize()

    assert await emoji_service.index_tag_axes() == 0
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_pick_semantic_matches_the_closest_axis(service) -> None:
    emoji_service, _ = service
    happy = hashlib.sha256(b"happy").hexdigest()
    cat = hashlib.sha256(b"cat").hexdigest()
    await emoji_service.store.insert_many(
        [_row(happy, "/tmp/happy.gif"), _row(cat, "/tmp/cat.gif")]
    )
    await emoji_service.store.upsert_axis(
        SESSION, happy, EMOTION, "开心", pack_vector([0.0, 1.0]), "m", 2
    )
    await emoji_service.store.upsert_axis(
        SESSION, cat, CONTENT, "猫", pack_vector([1.0, 0.0]), "m", 2
    )
    emoji_service.embedding = _FakeEmbedding({"开心": [0.0, 1.0]})

    picked = await emoji_service.pick_semantic(SESSION, "开心")

    assert picked is not None
    assert picked["file_path"] == "/tmp/happy.gif"
    assert picked["score"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_pick_semantic_falls_back_when_no_axis_is_close(service) -> None:
    emoji_service, _ = service
    digest = hashlib.sha256(b"cat").hexdigest()
    await emoji_service.store.insert_many([_row(digest)])
    await emoji_service.store.upsert_axis(
        SESSION, digest, CONTENT, "猫", pack_vector([1.0, 0.0]), "m", 2
    )
    emoji_service.embedding = _FakeEmbedding({"开心": [0.0, 1.0]})
    emoji_service.config.semantic_min_score = 0.9

    fallback = {"file_path": "/tmp/fallback.gif", "score": 0.0}
    calls: list[str] = []

    async def fake_pick(session_key: str, context: str, require_budget: bool = True):
        calls.append(context)
        return fallback

    emoji_service.pick = fake_pick

    assert await emoji_service.pick_semantic(SESSION, "开心") is fallback
    assert calls == ["开心"]


async def _await(value):
    return value
