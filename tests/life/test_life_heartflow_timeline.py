"""Unit tests for the focus timeline: turning, folding and checkpoints."""

from __future__ import annotations

import pytest

from bot.life.heartflow.focus.timeline import (
    ROLE_BOT,
    ROLE_USER,
    STATUS_CLOSED,
    STATUS_COMPRESSED,
    STATUS_OPEN,
    FocusMessage,
    FocusTimeline,
    FocusTurn,
)

SESSION = "aiocqhttp:GroupMessage:1"


def _message(ts: float, text: str, role: str = ROLE_USER, name: str = "A"):
    return FocusMessage(role=role, text=text, ts=ts, sender_name=name)


def _timeline(**overrides) -> FocusTimeline:
    defaults = {"keep_turns": 2, "turn_max_chars": 40, "digest_max_chars": 200}
    defaults.update(overrides)
    return FocusTimeline(session_key=SESSION, **defaults)


def test_messages_within_the_gap_share_a_turn():
    timeline = _timeline()

    first = timeline.add_message(_message(100.0, "一"), gap_seconds=300)
    second = timeline.add_message(_message(200.0, "二"), gap_seconds=300)

    assert first is second
    assert len(timeline.turns) == 1
    assert timeline.total_messages == 2
    assert timeline.turns[0].status == STATUS_OPEN


def test_idle_gap_starts_a_new_turn():
    timeline = _timeline()

    timeline.add_message(_message(100.0, "一"), gap_seconds=300)
    second = timeline.add_message(_message(401.0, "二"), gap_seconds=300)

    assert len(timeline.turns) == 2
    assert timeline.turns[0].status == STATUS_CLOSED
    assert timeline.turns[0].ended_ts == 100.0
    assert second.index == 2


def test_close_turn_marks_the_open_turn():
    timeline = _timeline()
    timeline.add_message(_message(100.0, "一"), gap_seconds=300)

    closed = timeline.close_turn(150.0)

    assert closed is not None
    assert closed.status == STATUS_CLOSED
    assert closed.ended_ts == 150.0
    assert timeline.open_turn is None
    assert timeline.close_turn() is None


def test_reply_is_appended_and_closes_the_turn():
    timeline = _timeline()
    timeline.add_message(_message(100.0, "你好"), gap_seconds=300)

    turn = timeline.add_reply(_message(110.0, "在的", role=ROLE_BOT, name=None))

    assert turn.status == STATUS_CLOSED
    assert [item.role for item in turn.messages] == [ROLE_USER, ROLE_BOT]
    assert turn.messages[1].line().startswith("我:")
    assert timeline.open_turn is None


def test_reply_without_an_open_turn_opens_and_closes_one():
    timeline = _timeline()

    turn = timeline.add_reply(_message(100.0, "我说一句", role=ROLE_BOT))

    assert turn.status == STATUS_CLOSED
    assert len(timeline.turns) == 1
    assert timeline.open_turn is None


def test_compress_folds_the_oldest_turns_into_the_digest():
    timeline = _timeline(keep_turns=1)
    timeline.add_message(_message(100.0, "一"), gap_seconds=300)
    timeline.add_message(_message(200.0, "二"), gap_seconds=300)
    timeline.add_message(_message(700.0, "三"), gap_seconds=300)

    folded = timeline.compress()

    assert len(folded) == 1
    assert folded[0].status == STATUS_COMPRESSED
    assert folded[0].messages == []
    assert folded[0].summary == "A: 一 / A: 二"
    assert timeline.digest == "A: 一 / A: 二"
    assert timeline.compressed_turns == 1
    assert len(timeline.turns) == 1
    assert timeline.turns[0].index == 2


def test_compress_keeps_the_open_turn_even_when_it_is_the_only_one():
    timeline = _timeline(keep_turns=1)
    timeline.add_message(_message(100.0, "一"), gap_seconds=300)
    timeline.add_message(_message(150.0, "二"), gap_seconds=300)

    folded = timeline.compress()

    assert folded == []
    assert timeline.digest == ""
    assert timeline.open_turn is not None


def test_compress_accumulates_the_digest():
    timeline = _timeline(keep_turns=1, digest_max_chars=500)
    timeline.add_message(_message(100.0, "一"), gap_seconds=300)
    timeline.add_message(_message(500.0, "二"), gap_seconds=300)
    timeline.compress()
    timeline.add_message(_message(900.0, "三"), gap_seconds=300)
    timeline.compress()

    assert timeline.digest == "A: 一\nA: 二"
    assert timeline.compressed_turns == 2


def test_digest_drops_the_oldest_lines_when_it_overflows():
    timeline = _timeline(keep_turns=1, digest_max_chars=20)
    for index in range(4):
        timeline.add_message(
            _message(100.0 + index * 400, f"第{index}句"),
            gap_seconds=300,
        )

    timeline.compress()

    assert timeline.digest.startswith("…")
    assert "第0句" not in timeline.digest
    assert timeline.digest.endswith("第2句")


def test_turn_text_is_capped():
    turn = FocusTurn(index=1, started_ts=0.0)
    turn.messages = [_message(0.0, "很长" * 50)]

    text = turn.text(10)

    assert text.endswith("…")
    assert len(text) <= 11


def test_digest_block_is_rendered_only_when_folded():
    timeline = _timeline(keep_turns=1)

    assert timeline.render_digest_block() is None

    timeline.add_message(_message(100.0, "一"), gap_seconds=300)
    timeline.add_message(_message(600.0, "二"), gap_seconds=300)
    timeline.compress()

    block = timeline.render_digest_block()
    assert block is not None
    assert "<life_focus>" in block
    assert "A: 一" in block


def test_digest_block_respects_the_block_budget():
    timeline = _timeline(keep_turns=1, digest_max_chars=500, block_max_chars=30)
    timeline.add_message(_message(100.0, "一" * 50), gap_seconds=300)
    timeline.add_message(_message(600.0, "二"), gap_seconds=300)
    timeline.compress()

    block = timeline.render_digest_block()

    assert block is not None
    assert len(block) < 200


def test_checkpoint_roundtrip():
    timeline = _timeline(keep_turns=1)
    timeline.add_message(_message(100.0, "一", name="Alice"), gap_seconds=300)
    timeline.add_message(_message(600.0, "二", name="Bob"), gap_seconds=300)
    timeline.compress()

    restored = FocusTimeline.from_checkpoint(
        timeline.checkpoint(),
        keep_turns=1,
        turn_max_chars=40,
        digest_max_chars=200,
    )

    assert restored is not None
    assert restored.session_key == SESSION
    assert restored.digest == timeline.digest
    assert restored.next_index == timeline.next_index
    assert restored.total_messages == 2
    assert restored.compressed_turns == 1
    assert len(restored.turns) == 1
    assert restored.turns[0].messages[0].sender_name == "Bob"


def test_checkpoint_restores_next_index_without_the_field():
    payload = {
        "session_key": SESSION,
        "turns": [{"index": 7, "started_ts": 1.0, "messages": []}],
    }

    timeline = FocusTimeline.from_checkpoint(payload)

    assert timeline is not None
    assert timeline.next_index == 8


@pytest.mark.parametrize("payload", [None, {}, {"session_key": ""}])
def test_checkpoint_rejects_unusable_payloads(payload):
    assert FocusTimeline.from_checkpoint(payload) is None


def test_messages_without_text_are_dropped_from_checkpoints():
    assert FocusMessage.from_dict({"role": "user", "text": ""}) is None
    assert FocusMessage.from_dict("nope") is None
    assert FocusTurn.from_dict({"index": "x"}) is None
