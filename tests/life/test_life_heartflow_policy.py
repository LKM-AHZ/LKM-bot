"""Unit tests for the heart-flow decision policy."""

from __future__ import annotations

import pytest

from bot.life.config import HeartFlowConfig
from bot.life.heartflow.policy import (
    BOT_ROLES,
    HeartFlowPolicy,
    PolicyMessage,
    batch_features,
)


def _policy(**gate) -> HeartFlowPolicy:
    config = HeartFlowConfig(enabled=True)
    for key, value in gate.items():
        setattr(config.gate, key, value)
    return HeartFlowPolicy(config, bot_names=["bot", "小助手"])


def _user(message_id: str, ts: float, text: str = "", **flags):
    return PolicyMessage(
        message_id=message_id,
        ts=ts,
        sender_id=flags.pop("sender_id", "u1"),
        text=text,
        flags=dict(flags),
    )


def _bot(message_id: str, ts: float) -> PolicyMessage:
    return PolicyMessage(message_id=message_id, ts=ts, text="", is_bot=True)


def test_bot_roles_are_recognized():
    assert "bot" in BOT_ROLES
    assert "assistant" in BOT_ROLES
    assert "user" not in BOT_ROLES


def test_burst_is_merged_into_one_turn():
    decisions = _policy().decide_session(
        [
            _bot("b0", 90.0),
            _user("m0", 100.0, "看这个", at=True),
            _user("m1", 100.3, "很怪", sender_id="u2"),
            _user("m2", 100.6, "对吧", sender_id="u3"),
        ]
    )

    by_id = {item.message_id: item for item in decisions}
    assert by_id["m0"].decide == "wait"
    assert by_id["m0"].reason == "merged_batch"
    assert by_id["m1"].reason == "merged_batch"
    assert by_id["m2"].decide == "reply"
    assert by_id["m2"].reason == "direct_address"
    assert by_id["b0"].reason == "bot_message"
    # Every message of the corpus gets exactly one decision record.
    assert len(decisions) == 4


def test_hard_cap_splits_a_never_ending_burst():
    policy = _policy()
    decisions = policy.decide_session(
        [
            _bot("b0", 10.0),
            _user("m0", 20.0, "一", at=True),
            _user("m1", 23.0, "二", at=True),
            _user("m2", 26.0, "三", at=True),
        ]
    )

    # Three separate turns: each message arrives after the previous release.
    assert [
        item.decide for item in decisions if not item.message_id.startswith("b")
    ] == [
        "reply",
        "reply",
        "reply",
    ]
    assert all(item.merged is False for item in decisions)


def test_bot_message_ends_the_turn():
    decisions = _policy().decide_session(
        [
            _bot("b0", 90.0),
            _user("m0", 100.0, "随便说", at=True),
            _bot("b1", 100.5),
            _user("m1", 110.0, "再说", at=True),
        ]
    )

    by_id = {item.message_id: item for item in decisions}
    assert by_id["m0"].decide == "reply"
    assert by_id["m0"].release_at == pytest.approx(101.2)
    assert by_id["b1"].release_at == pytest.approx(100.5)
    assert by_id["m1"].decide == "reply"
    assert by_id["m1"].release_at == pytest.approx(111.2)


def test_untouched_group_stays_silent():
    decisions = _policy().decide_session([_user("m0", 100.0, "今天天气不错")])

    assert decisions[0].decide == "wait"
    assert decisions[0].reason == "idle_silence"


def test_direct_address_wakes_an_idle_group():
    decisions = _policy().decide_session([_user("m0", 100.0, "小助手在吗")])

    assert decisions[0].decide == "reply"
    assert decisions[0].reason == "direct_address"


def test_private_chat_answers_every_turn():
    decisions = _policy().decide_session(
        [_user("m0", 100.0, "嗯"), _user("m1", 200.0, "嗯嗯")],
        is_private=True,
    )

    assert [item.decide for item in decisions] == ["reply", "reply"]


def test_consecutive_waits_raise_the_bar():
    policy = _policy(base_threshold=0.4)
    decisions = policy.decide_session(
        [
            _bot("b0", 10.0),
            _user("m0", 20.0, "只是随口一说"),
            _user("m1", 40.0, "还是随口说"),
            _user("m2", 60.0, "继续随口说"),
            _user("m3", 80.0, "这次问一句?"),
        ]
    )

    by_id = {item.message_id: item for item in decisions}
    assert by_id["m0"].decide == "wait"
    # The wait streak raises the threshold for the following turns.
    assert by_id["m2"].threshold > by_id["m1"].threshold


def test_question_mark_reaches_the_gate():
    features = batch_features([_user("m0", 0.0, "这样对吗？")], ["bot"])

    assert features.question is True
    assert features.direct is False


def test_wake_word_marks_the_turn_as_direct():
    features = batch_features([_user("m0", 0.0, "Bot 你觉得呢")], ["bot"])

    assert features.wake_word is True
    assert features.direct is True


def test_sticker_only_turn_is_detected():
    features = batch_features([_user("m0", 0.0, "", image=True)], ["bot"])

    assert features.sticker_only is True
    assert features.question is False


def test_command_is_not_answered_even_with_a_direct_address():
    decisions = _policy().decide_session(
        [_user("m0", 100.0, "/help", at=True, is_command=True)]
    )

    assert decisions[0].decide == "wait"
    assert decisions[0].reason == "command"


def test_preview_reports_the_inflight_plan():
    policy = _policy()
    plan, decision = policy.preview(
        [_user("m0", 100.0, "小助手在吗", at=True)],
        is_private=False,
        now=100.0,
    )

    assert plan is not None
    assert plan.release_at == pytest.approx(101.2)
    assert decision is not None
    assert decision.reply is True

    empty_plan, empty_decision = policy.preview([])
    assert empty_plan is None
    assert empty_decision is None
