"""Unit tests for shared focus timelines (MaiBot 1.4 focus groups)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from bot.core.db.sqlite import SQLiteDatabase
from bot.life.config import FocusConfig
from bot.life.heartflow.focus.service import FocusService
from bot.life.ingest import IngestedMessage

A = "aiocqhttp:GroupMessage:1"
B = "aiocqhttp:GroupMessage:2"
C = "aiocqhttp:GroupMessage:3"


def _message(umo: str, text: str, ts: int = 100) -> IngestedMessage:
    return IngestedMessage(
        platform_id="aiocqhttp",
        session_key=umo,
        message_id=f"m-{umo}-{ts}",
        ts=ts,
        sender_id="u1",
        sender_name="小明",
        group_id=umo.rsplit(":", 1)[-1],
        components=[],
        plain_text=text,
    )


@pytest_asyncio.fixture
async def focus(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "life.db"))
    await database.initialize()
    config = FocusConfig(enabled=True, share_groups=[[A, B]])
    service = FocusService(database, config, life_config=None)
    await service.initialize()
    yield service
    await service.close()
    await database.engine.dispose()


def test_focus_key_without_groups_is_the_session() -> None:
    service = FocusService(object(), FocusConfig())

    assert service.focus_key(A) == A


def test_focus_key_is_shared_and_stable() -> None:
    service = FocusService(object(), FocusConfig(share_groups=[[B, A], []]))
    key = service.focus_key(A)

    assert key == service.focus_key(B)
    assert key == "shared:" + "|".join(sorted([A, B]))
    assert service.focus_key(C) == C


@pytest.mark.asyncio
async def test_shared_group_feeds_one_timeline(focus) -> None:
    recorded = await focus.observe_messages(
        [_message(A, "大家好"), _message(B, "你也好")]
    )

    assert recorded == 2
    timeline = await focus.timeline(focus.focus_key(A))
    texts = [
        message.text
        for turn in timeline.turns
        for message in turn.messages
    ]
    assert texts == ["大家好", "你也好"]
    assert timeline.session_key == focus.focus_key(B)


@pytest.mark.asyncio
async def test_sessions_outside_the_group_keep_their_own(focus) -> None:
    await focus.observe_messages([_message(C, "另一条线")])

    shared = await focus.timeline(focus.focus_key(A))
    separate = await focus.timeline(C)

    assert shared.turns == []
    assert [message.text for turn in separate.turns for message in turn.messages] == [
        "另一条线"
    ]


@pytest.mark.asyncio
async def test_delete_removes_the_shared_timeline(focus) -> None:
    await focus.observe_messages([_message(A, "只在共享里")])

    removed = await focus.delete(B)

    assert removed >= 1
    timeline = await focus.timeline(focus.focus_key(A))
    assert timeline.turns == []
