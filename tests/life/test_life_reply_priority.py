"""Life is the single reply-decision engine for its sessions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.builtin_stars.bot.group_chat_context import GroupChatContext
from bot.life.pipeline import LifeIngestStage

SESSION = "aiocqhttp:GroupMessage:1"


class FakeEvent:
    def __init__(self) -> None:
        self.unified_msg_origin = SESSION
        self._extra: dict = {}

    def get_group_id(self) -> str:
        return "1"

    def set_extra(self, key: str, value) -> None:
        self._extra[key] = value

    def get_extra(self, key: str, default=None):
        return self._extra.get(key, default)


def _service(in_scope: bool, archived: bool = True):
    async def ingest(event):
        return archived

    return SimpleNamespace(
        _running=True,
        config=SimpleNamespace(
            session_enabled=lambda umo, group_id=None: in_scope,
        ),
        ingest=ingest,
    )


@pytest.mark.asyncio
async def test_in_scope_session_marks_life_ownership():
    event = FakeEvent()
    stage = LifeIngestStage()
    stage.service = _service(in_scope=True)

    await stage.process(event)

    assert event.get_extra("_life_scope_active") is True
    assert event.get_extra("_life_archived") is True


@pytest.mark.asyncio
async def test_out_of_scope_session_leaves_upstream_engine_alone():
    event = FakeEvent()
    stage = LifeIngestStage()
    stage.service = _service(in_scope=False, archived=False)

    await stage.process(event)

    assert event.get_extra("_life_scope_active") is None
    assert event.get_extra("_life_archived") is None


@pytest.mark.asyncio
async def test_life_owned_session_skips_upstream_probability():
    event = FakeEvent()
    event.set_extra("_life_scope_active", True)

    # No config/context is needed: the early return happens before cfg().
    context = GroupChatContext(acm=None, context=None)  # type: ignore[arg-type]

    assert await context.need_active_reply(event) is False
