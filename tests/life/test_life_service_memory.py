"""Service-level tests for the memory wiring (init, tools, digest loop, stop)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import LifeConfig
from bot.life.service import LifeService, set_life_service


class FakeProvider:
    async def get_embeddings(self, texts):
        return [[1.0, 0.5, 0.25] for _ in texts]

    def get_model(self):
        return "fake-embedding"

    async def text_chat(self, prompt=None, system_prompt=None):
        class Response:
            completion_text = "摘要：用户在讨论迁移计划。"

        return Response()


class FakeProviderManager:
    def __init__(self) -> None:
        self.llm_tools = FakeToolManager()
        self.provider = FakeProvider()

    async def get_using_provider_async(self, _provider_type):
        return self.provider

    async def get_provider_by_id(self, _provider_id):
        return self.provider


class FakeToolManager:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.handlers: dict[str, object] = {}

    def add_func(self, name, func_args, desc, handler) -> None:
        self.registered.append(name)
        self.handlers[name] = handler

    def remove_func(self, name) -> None:
        if name in self.registered:
            self.registered.remove(name)
        self.handlers.pop(name, None)


class FakeContext:
    def __init__(self) -> None:
        self.provider_manager = FakeProviderManager()


@pytest_asyncio.fixture
async def db(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    yield database
    await database.engine.dispose()


def _service(db, tmp_path, context, memory_enabled: bool = True) -> LifeService:
    config = LifeConfig(enabled=True)
    config.scope.mode = "all"
    config.memory.enabled = memory_enabled
    config.memory.digest.enabled = False
    return LifeService(
        db,
        star_context=context,
        config=config,
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )


@pytest.mark.asyncio
async def test_memory_disabled_by_default(db, tmp_path):
    context = FakeContext()
    service = _service(db, tmp_path, context, memory_enabled=False)
    await service.initialize()
    try:
        assert service.memory is None
        status = await service.status()
        assert status["memory"] == {"enabled": False}
        assert context.provider_manager.llm_tools.registered == []
    finally:
        await service.stop()
        set_life_service(None)


class FakeEvent:
    unified_msg_origin = "aiocqhttp:GroupMessage:1"

    def get_group_id(self):
        return "1"


class FakeMessage:
    def __init__(self, sender_id: str, name: str) -> None:
        self.session_key = FakeEvent.unified_msg_origin
        self.platform_id = "aiocqhttp"
        self.sender_id = sender_id
        self.sender_name = name
        self.group_id = "1"
        self.ts = 1_700_000_000


@pytest.mark.asyncio
async def test_person_layer_serves_the_profile_tool(db, tmp_path):
    context = FakeContext()
    config = LifeConfig(enabled=True)
    config.scope.mode = "all"
    config.memory.enabled = True
    config.memory.digest.enabled = False
    config.person.enabled = True
    service = LifeService(
        db,
        star_context=context,
        config=config,
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )
    await service.initialize()
    try:
        assert service.person is not None
        status = await service.status()
        assert status["person"]["enabled"] is True

        await service.person.observe_messages([FakeMessage("42", "Alice")])
        await service.person.ingest_facts(
            FakeEvent.unified_msg_origin,
            [{"person_id": "Alice", "content": "Alice 喜欢猫", "importance": 0.8}],
        )

        handler = context.provider_manager.llm_tools.handlers["get_person_profile"]
        payload = await handler(FakeEvent(), person="Alice")

        assert "Alice" in payload
        assert "喜欢猫" in payload
    finally:
        await service.stop()
        set_life_service(None)


@pytest.mark.asyncio
async def test_expression_layer_initializes_and_stops(db, tmp_path):
    context = FakeContext()
    config = LifeConfig(enabled=True)
    config.scope.mode = "all"
    config.expression.enabled = True
    config.expression.use_vector = False
    config.expression.learn_interval_minutes = 60
    service = LifeService(
        db,
        star_context=context,
        config=config,
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )
    await service.initialize()
    try:
        assert service.expression is not None
        status = await service.status()
        assert status["expression"]["enabled"] is True
        assert status["expression"]["index"] is None
        task = service._tasks["expression"]
        assert not task.done()
    finally:
        await service.stop()
        set_life_service(None)

    assert task.done()


@pytest.mark.asyncio
async def test_jargon_layer_initializes_and_stops(db, tmp_path):
    context = FakeContext()
    config = LifeConfig(enabled=True)
    config.scope.mode = "all"
    config.jargon.enabled = True
    config.jargon.use_llm = False
    config.jargon.learn_interval_minutes = 60
    service = LifeService(
        db,
        star_context=context,
        config=config,
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )
    await service.initialize()
    try:
        assert service.jargon is not None
        status = await service.status()
        assert status["jargon"]["enabled"] is True
        task = service._tasks["jargon"]
        assert not task.done()
    finally:
        await service.stop()
        set_life_service(None)

    assert task.done()


@pytest.mark.asyncio
async def test_emoji_layer_registers_tool_and_stops(db, tmp_path):
    context = FakeContext()
    config = LifeConfig(enabled=True)
    config.scope.mode = "all"
    config.emoji.enabled = True
    config.emoji.collect_interval_minutes = 60
    service = LifeService(
        db,
        star_context=context,
        config=config,
        config_path=tmp_path / "life" / "config.json",
        data_dir=tmp_path / "life",
    )
    await service.initialize()
    try:
        assert service.emoji is not None
        status = await service.status()
        assert status["emoji"]["enabled"] is True
        assert "send_meme" in context.provider_manager.llm_tools.registered
        task = service._tasks["emoji"]
        assert not task.done()
    finally:
        await service.stop()
        set_life_service(None)

    assert task.done()
    assert "send_meme" not in context.provider_manager.llm_tools.registered


@pytest.mark.asyncio
async def test_memory_initializes_registers_tools_and_stops(db, tmp_path):
    context = FakeContext()
    service = _service(db, tmp_path, context)
    await service.initialize()
    try:
        assert service.memory is not None
        assert context.provider_manager.llm_tools.registered == [
            "search_memory",
            "remember",
            "get_person_profile",
        ]
        status = await service.status()
        assert status["memory"]["enabled"] is True
        assert status["memory"]["engine"] == "local"
    finally:
        await service.stop()
        set_life_service(None)

    assert context.provider_manager.llm_tools.registered == []
    assert service.memory.enabled is False
