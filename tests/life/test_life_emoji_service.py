"""Unit tests for sticker selection, sending and the agent tool."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import EmojiConfig, LifeConfig, MemoryEmbeddingConfig
from bot.life.emoji.service import EmojiService
from bot.life.emoji.tools import register_emoji_tools
from bot.life.memory.store import tokenize
from bot.life.sender import SendResult

SESSION = "aiocqhttp:GroupMessage:1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class FakeContext:
    provider_manager = None


class FakeSender:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []

    async def send_chain(self, umo, chain) -> SendResult:
        self.calls.append(umo)
        return SendResult(
            ok=self.ok, platform_message_id="42", error=None if self.ok else "boom"
        )


class FakeToolManager:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def add_func(self, name, func_args, desc, handler) -> None:
        self.handlers[name] = handler

    def remove_func(self, name) -> None:
        self.handlers.pop(name, None)


class FakeEvent:
    def __init__(self, umo: str = SESSION) -> None:
        self.unified_msg_origin = umo

    def get_group_id(self):
        return "1"


async def _seed(service: EmojiService, tmp_path, caption: str, name: str) -> dict:
    path = tmp_path / name
    path.write_bytes(PNG)
    await service.store.insert_many(
        [
            {
                "session_key": SESSION,
                "sha256": name,
                "file_path": str(path),
                "mime": "image/png",
                "size_bytes": len(PNG),
                "tags": sorted(tokenize(caption))[:6],
                "caption": caption,
                "created_at": int(time.time()),
                "updated_at": int(time.time()),
            }
        ]
    )
    return await service.store.get_by_hash(SESSION, name)


@pytest_asyncio.fixture
async def service(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    life_config = LifeConfig(enabled=True)
    life_config.scope.mode = "all"
    instance = EmojiService(
        context=FakeContext(),
        config=EmojiConfig(enabled=True),
        db_helper=database,
        data_dir=tmp_path,
        life_config=life_config,
        sender=FakeSender(),
        embedding_config=MemoryEmbeddingConfig(enabled=False),
    )
    await instance.initialize()
    yield instance
    await instance.close()
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_pick_scores_by_tags(service, tmp_path):
    await _seed(service, tmp_path, "笑死 这也行", "a.png")
    await _seed(service, tmp_path, "今天天气不错", "b.png")

    picked = await service.pick(SESSION, "笑死")

    assert picked is not None
    assert picked["caption"].startswith("笑死")


@pytest.mark.asyncio
async def test_pick_returns_none_below_min_score(service, tmp_path):
    service.config.min_score = 0.9
    await _seed(service, tmp_path, "笑死", "a.png")

    assert await service.pick(SESSION, "完全无关的天气话题") is None


@pytest.mark.asyncio
async def test_send_marks_usage_and_consumes_budget(service, tmp_path):
    await _seed(service, tmp_path, "笑死", "a.png")

    first = await service.send(SESSION, "笑死")
    second = await service.send(SESSION, "笑死")

    assert first["ok"] is True
    assert second["ok"] is False
    row = await service.store.get_by_hash(SESSION, "a.png")
    assert row["usage_count"] == 1
    assert service.sender.calls == [SESSION]


@pytest.mark.asyncio
async def test_send_reports_missing_file(service, tmp_path):
    await _seed(service, tmp_path, "笑死", "a.png")
    (tmp_path / "a.png").unlink()

    result = await service.send(SESSION, "笑死")

    assert result["ok"] is False
    assert "missing" in str(result["error"])


@pytest.mark.asyncio
async def test_send_failure_does_not_consume_budget(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "fail.db"))
    await database.initialize()
    service = EmojiService(
        context=FakeContext(),
        config=EmojiConfig(enabled=True),
        db_helper=database,
        data_dir=tmp_path,
        life_config=LifeConfig(enabled=True),
        sender=FakeSender(ok=False),
        embedding_config=MemoryEmbeddingConfig(enabled=False),
    )
    await service.initialize()
    try:
        await _seed(service, tmp_path, "笑死", "a.png")
        result = await service.send(SESSION, "笑死")
        assert result["ok"] is False
        assert service.selector.budget.can_send(SESSION) is True
    finally:
        await service.close()
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_hourly_cap_blocks_sending(service, tmp_path):
    service.config.cooldown_seconds = 0
    service.config.max_per_hour = 1
    await _seed(service, tmp_path, "笑死", "a.png")

    assert service.selector.budget.can_send(SESSION) is True
    service.selector.budget.mark_sent(SESSION)

    assert service.selector.budget.can_send(SESSION) is False
    assert await service.pick(SESSION, "笑死") is None


@pytest.mark.asyncio
async def test_tool_sends_and_reports_errors(service, tmp_path):
    await _seed(service, tmp_path, "笑死", "a.png")
    manager = FakeToolManager()
    register_emoji_tools(manager, service)

    handler = manager.handlers["send_meme"]
    first = await handler(FakeEvent(), description="笑死")
    second = await handler(FakeEvent(), description="笑死")

    assert first == "sent"
    assert second.startswith("error:")


@pytest.mark.asyncio
async def test_tool_respects_scope(service, tmp_path):
    await _seed(service, tmp_path, "笑死", "a.png")
    manager = FakeToolManager()
    register_emoji_tools(manager, service)
    service.life_config.scope.mode = "allowlist"
    service.life_config.scope.enabled_umos = ["aiocqhttp:GroupMessage:other"]

    result = await manager.handlers["send_meme"](FakeEvent(), description="笑死")

    assert result.startswith("error:")


@pytest.mark.asyncio
async def test_forget_deletes_files(service, tmp_path):
    await _seed(service, tmp_path, "笑死", "a.png")

    deleted = await service.forget(session_key=SESSION)

    assert deleted == 1
    assert not (tmp_path / "a.png").exists()
