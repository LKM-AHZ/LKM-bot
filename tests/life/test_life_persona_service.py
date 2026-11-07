"""Unit tests for the persona service: resolution, caching and prompt rendering."""

from __future__ import annotations

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import LifeConfig, PersonaConfig
from bot.life.persona.service import PersonaService

PERSONA = "persona-a"
SESSION = "aiocqhttp:GroupMessage:1"


class FakeEvent:
    def __init__(
        self,
        umo: str = SESSION,
        group_id: str | None = "1",
        persona_id: str = PERSONA,
    ) -> None:
        self.unified_msg_origin = umo
        self._group_id = group_id
        self._persona_id = persona_id

    def get_group_id(self):
        return self._group_id

    def get_extra(self, key: str):
        return self._persona_id if key == "_trace_persona_id" else None


class FakeRequest:
    def __init__(self, system_prompt: str = "基础系统提示") -> None:
        self.prompt = "在吗"
        self.system_prompt = system_prompt


@pytest_asyncio.fixture
async def persona(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    config = LifeConfig(enabled=True)
    config.scope.mode = "all"
    config.persona = PersonaConfig(enabled=True)
    service = PersonaService(config.persona, database, life_config=config)
    await service.initialize()
    yield service
    await service.close()
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_group_block_is_prepended_with_group_prompt(persona):
    await persona.upsert(
        {
            "persona_id": PERSONA,
            "base_prompt": "说话随意一点。",
            "group_prompt": "群里别太热情。",
            "private_prompt": "私聊可以黏人一点。",
            "quote_rate": 0.4,
            "multi_segment_prob": 0.25,
            "typo_rate": 0.0,
            "reply_max_chars": 60,
        }
    )
    req = FakeRequest()

    assert await persona.inject(FakeEvent(), req) is True

    assert req.system_prompt.startswith("<life_persona>")
    assert "群里别太热情。" in req.system_prompt
    assert "私聊可以黏人一点。" not in req.system_prompt
    assert "60 字" in req.system_prompt
    assert "40%" in req.system_prompt
    assert "25%" in req.system_prompt
    assert "打错字" not in req.system_prompt
    assert req.system_prompt.endswith("基础系统提示")


@pytest.mark.asyncio
async def test_private_scene_uses_private_prompt(persona):
    await persona.upsert(
        {
            "persona_id": PERSONA,
            "group_prompt": "群里别太热情。",
            "private_prompt": "私聊可以黏人一点。",
        }
    )
    req = FakeRequest()

    assert await persona.inject(FakeEvent(group_id=None), req) is True

    assert "私聊可以黏人一点。" in req.system_prompt
    assert "群里别太热情。" not in req.system_prompt


@pytest.mark.asyncio
async def test_unknown_persona_falls_back_to_the_default_profile(persona):
    await persona.upsert({"persona_id": "", "base_prompt": "默认人设"})
    req = FakeRequest()

    assert await persona.inject(FakeEvent(persona_id="unknown"), req) is True

    assert "默认人设" in req.system_prompt


@pytest.mark.asyncio
async def test_missing_profile_leaves_the_prompt_untouched(persona):
    req = FakeRequest()

    assert await persona.inject(FakeEvent(), req) is False

    assert req.system_prompt == "基础系统提示"


@pytest.mark.asyncio
async def test_block_is_not_injected_twice(persona):
    await persona.upsert({"persona_id": PERSONA, "base_prompt": "人设"})
    req = FakeRequest(system_prompt="<life_persona>\n已经注入过\n</life_persona>")

    assert await persona.inject(FakeEvent(), req) is False


@pytest.mark.asyncio
async def test_profile_is_cached_between_turns(persona, monkeypatch):
    await persona.upsert({"persona_id": PERSONA, "base_prompt": "人设"})
    calls = 0
    original = persona.store.get

    async def counting_get(persona_id):
        nonlocal calls
        calls += 1
        return await original(persona_id)

    monkeypatch.setattr(persona.store, "get", counting_get)
    await persona.profile_for(PERSONA)
    await persona.profile_for(PERSONA)

    assert calls == 1
    assert (await persona.status())["counters"]["cache_hits"] >= 1


@pytest.mark.asyncio
async def test_upsert_invalidates_the_cache(persona):
    await persona.upsert({"persona_id": PERSONA, "base_prompt": "旧人设"})
    assert await persona.profile_for(PERSONA) is not None
    await persona.upsert({"persona_id": PERSONA, "base_prompt": "新人设"})

    profile = await persona.profile_for(PERSONA)

    assert profile["base_prompt"] == "新人设"


@pytest.mark.asyncio
async def test_disabled_persona_layer_is_a_noop(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    service = PersonaService(PersonaConfig(), database)
    await service.initialize()
    try:
        await service.upsert({"persona_id": PERSONA, "base_prompt": "人设"})
        req = FakeRequest()

        assert await service.inject(FakeEvent(), req) is False
        assert req.system_prompt == "基础系统提示"
    finally:
        await service.close()
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_session_outside_allowlist_is_skipped(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    config = LifeConfig(enabled=True)
    config.scope.mode = "allowlist"
    config.persona = PersonaConfig(enabled=True)
    service = PersonaService(config.persona, database, life_config=config)
    await service.initialize()
    try:
        await service.upsert({"persona_id": PERSONA, "base_prompt": "人设"})
        req = FakeRequest()

        assert await service.inject(FakeEvent(umo="other:GroupMessage:9"), req) is False
    finally:
        await service.close()
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_markdown_allowed_drops_the_ban_line(persona):
    await persona.upsert(
        {"persona_id": PERSONA, "allow_markdown": True, "typo_rate": 0.0}
    )
    profile = await persona.profile_for(PERSONA)

    block = persona.render_block(profile, "group")

    assert "不要输出 Markdown" not in block
    assert "打错字" not in block


@pytest.mark.asyncio
async def test_block_respects_the_character_budget(persona):
    persona.config.block_max_chars = 220
    await persona.upsert(
        {
            "persona_id": PERSONA,
            "base_prompt": "很长的人设" * 200,
            "group_prompt": "同样很长的群设定" * 200,
        }
    )
    profile = await persona.profile_for(PERSONA)

    block = persona.render_block(profile, "group")

    assert len(block) <= 220
    assert block.startswith("<life_persona>")
