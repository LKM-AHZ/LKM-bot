"""Unit tests for the focus service: archive hook, reply, compression, prompt."""

from __future__ import annotations

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import FocusConfig, LifeConfig
from bot.life.heartflow.focus.service import FocusService
from bot.life.ingest import IngestedMessage

SESSION = "aiocqhttp:GroupMessage:1"


class FakeEvent:
    def __init__(self, umo: str = SESSION, group_id: str | None = "1") -> None:
        self.unified_msg_origin = umo
        self._group_id = group_id

    def get_group_id(self):
        return self._group_id


class FakeRequest:
    def __init__(self) -> None:
        self.prompt = "你好"
        self.system_prompt = "You are a group member."


def _message(
    ts: float,
    text: str = "一",
    *,
    umo: str = SESSION,
    message_id: str = "m1",
    flags: dict | None = None,
) -> IngestedMessage:
    return IngestedMessage(
        platform_id="aiocqhttp",
        session_key=umo,
        message_id=message_id,
        ts=int(ts),
        sender_id="u1",
        sender_name="Alice",
        group_id="1",
        plain_text=text,
        flags=flags or {},
    )


@pytest_asyncio.fixture
async def service(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    life_config = LifeConfig(enabled=True)
    life_config.scope.enabled_umos = [SESSION]
    focus = FocusService(
        db_helper=database,
        config=FocusConfig(enabled=True, keep_turns=2),
        life_config=life_config,
    )
    await focus.initialize()
    yield focus
    await focus.close()
    await database.engine.dispose()


@pytest.mark.asyncio
async def test_observe_messages_groups_by_gap(service):
    recorded = await service.observe_messages(
        [_message(100.0, "一", message_id="m1"), _message(200.0, "二", message_id="m2")]
    )
    recorded += await service.observe_messages([_message(900.0, "三", message_id="m3")])

    detail = await service.session_detail(SESSION)

    assert recorded == 3
    assert len(detail["turns"]) == 2
    assert detail["total_messages"] == 3


@pytest.mark.asyncio
async def test_observe_messages_marks_media_and_skips_empty(service):
    await service.observe_messages(
        [
            _message(100.0, "", message_id="m1", flags={"image": True}),
            _message(101.0, "", message_id="m2"),
        ]
    )

    detail = await service.session_detail(SESSION)

    assert len(detail["turns"]) == 1
    assert detail["turns"][0]["messages"][0]["text"] == "[图片]"


@pytest.mark.asyncio
async def test_observe_messages_respects_the_scope(service):
    service.life_config.scope.enabled_umos = ["other:session"]

    recorded = await service.observe_messages([_message(100.0)])

    assert recorded == 0
    assert (await service.session_detail(SESSION))["turns"] == []


@pytest.mark.asyncio
async def test_reply_closes_the_turn(service):
    await service.observe_messages([_message(100.0, "你好")])

    recorded = await service.note_reply(SESSION, "在的", ts=110.0)

    detail = await service.session_detail(SESSION)
    assert recorded is True
    assert len(detail["turns"]) == 1
    assert detail["turns"][0]["status"] == "closed"
    messages = detail["turns"][0]["messages"]
    assert [item["role"] for item in messages] == ["user", "bot"]
    assert messages[1]["text"] == "在的"


@pytest.mark.asyncio
async def test_reply_ignores_empty_text(service):
    assert await service.note_reply(SESSION, "   ") is False


@pytest.mark.asyncio
async def test_compression_folds_and_persists(service):
    for index in range(5):
        await service.observe_messages(
            [_message(100.0 + index * 900, f"第{index}句", message_id=f"m{index}")]
        )

    result = await service.compress_round()

    detail = await service.session_detail(SESSION)
    assert result["enabled"] is True
    assert detail["compressed_turns"] >= 1
    assert detail["digest"]
    stored = await service.store.list_turns(SESSION)
    assert any(turn["status"] == "compressed" for turn in stored)


@pytest.mark.asyncio
async def test_inject_adds_the_block_once(service):
    for index in range(5):
        await service.observe_messages(
            [_message(100.0 + index * 900, f"第{index}句", message_id=f"m{index}")]
        )
    await service.compress_round()

    request = FakeRequest()
    changed = await service.inject(FakeEvent(), request)

    assert changed is True
    assert "<life_focus>" in request.system_prompt
    assert await service.inject(FakeEvent(), request) is False


@pytest.mark.asyncio
async def test_inject_is_skipped_without_a_digest(service):
    await service.observe_messages([_message(100.0, "你好")])
    request = FakeRequest()

    assert await service.inject(FakeEvent(), request) is False
    assert "<life_focus>" not in request.system_prompt


@pytest.mark.asyncio
async def test_inject_can_be_disabled(service):
    service.config.inject = False

    assert await service.inject(FakeEvent(), FakeRequest()) is False


@pytest.mark.asyncio
async def test_checkpoint_all_flushes_dirty_timelines(service):
    await service.observe_messages([_message(100.0, "你好")])

    flushed = await service.checkpoint_all()

    assert flushed >= 0
    assert (await service.store.list_turns(SESSION))[0]["messages"][0]["text"] == "你好"


@pytest.mark.asyncio
async def test_delete_forgets_the_session(service):
    await service.observe_messages([_message(100.0, "你好")])

    deleted = await service.delete(SESSION)

    assert deleted == 1
    assert (await service.session_detail(SESSION))["turns"] == []


@pytest.mark.asyncio
async def test_status_reports_storage_and_counters(service):
    await service.observe_messages([_message(100.0, "你好")])

    status = await service.status()

    assert status["enabled"] is True
    assert status["configured"] is True
    assert status["storage"]["sessions"] == 1
    assert status["counters"]["observed"] == 1


@pytest.mark.asyncio
async def test_closed_service_ignores_messages(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "closed.db"))
    await database.initialize()
    focus = FocusService(database, FocusConfig(enabled=True))
    await focus.initialize()
    await focus.close()

    assert await focus.observe_messages([_message(100.0)]) == 0
    assert await focus.note_reply(SESSION, "在的") is False
    await database.engine.dispose()
