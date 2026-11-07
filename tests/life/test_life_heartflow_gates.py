"""Unit tests for the heart-flow reply gate."""

from __future__ import annotations

import pytest

from bot.life.config import DEFAULT_GATE_WEIGHTS, GateConfig
from bot.life.heartflow.gates import (
    BatchFeatures,
    GateContext,
    dynamic_threshold,
    evaluate,
    feature_values,
    score,
)


def test_default_weights_cover_every_feature():
    assert DEFAULT_GATE_WEIGHTS["bias"] < 0
    assert DEFAULT_GATE_WEIGHTS["at_bot"] > DEFAULT_GATE_WEIGHTS["question"]
    assert set(DEFAULT_GATE_WEIGHTS) >= {
        "reply_to_bot",
        "wake_word",
        "bot_name",
        "mentions_other",
        "sticker_only",
        "burst_length",
        "relation",
    }


def test_model_input_matches_the_weight_keys():
    # A weight without a feature (or the reverse) silently changes the model.
    assert set(feature_values(BatchFeatures())) == set(DEFAULT_GATE_WEIGHTS) - {"bias"}


def test_direct_address_answers_and_bypasses_the_threshold():
    decision = evaluate(
        BatchFeatures(at_bot=True),
        GateContext(now=1000.0),
        GateConfig(),
    )

    assert decision.reply is True
    assert decision.reason == "direct_address"
    assert decision.threshold == 0.0
    assert decision.features["at_bot"] == 1.0


def test_private_chat_answers_without_a_direct_marker():
    decision = evaluate(
        BatchFeatures(message_count=1),
        GateContext(now=1000.0, is_private=True),
        GateConfig(),
    )

    assert decision.reply is True
    assert decision.reason == "direct_address"


def test_command_is_never_answered_by_the_heart_flow():
    decision = evaluate(
        BatchFeatures(command=True, at_bot=True),
        GateContext(now=1000.0),
        GateConfig(),
    )

    assert decision.reply is False
    assert decision.reason == "command"


def test_untouched_session_stays_silent():
    decision = evaluate(
        BatchFeatures(question=True),
        GateContext(now=1000.0, last_bot_reply_ts=0.0),
        GateConfig(),
    )

    assert decision.reply is False
    assert decision.reason == "idle_silence"


def test_long_idle_session_stays_silent():
    decision = evaluate(
        BatchFeatures(question=True),
        GateContext(now=2000.0, last_bot_reply_ts=1000.0),
        GateConfig(),
    )

    assert decision.reply is False
    assert decision.reason == "idle_silence"


def test_recent_reply_suppresses_a_second_answer():
    decision = evaluate(
        BatchFeatures(at_bot=True),
        GateContext(now=1001.0, last_bot_reply_ts=1000.0),
        GateConfig(),
    )

    assert decision.reply is False
    assert decision.reason == "post_reply_cooldown"


def test_likelihood_can_pass_a_tuned_threshold():
    features = BatchFeatures(message_count=3, question=True)
    context = GateContext(now=1010.0, last_bot_reply_ts=1000.0)

    strict = evaluate(features, context, GateConfig(base_threshold=0.55))
    relaxed = evaluate(features, context, GateConfig(base_threshold=0.4))

    assert strict.reply is False
    assert relaxed.reply is True
    assert relaxed.likelihood == strict.likelihood


def test_mentions_of_someone_else_lower_the_likelihood():
    base = BatchFeatures(question=True)
    other = BatchFeatures(question=True, mentions_other=True)

    assert score(other, GateConfig()) < score(base, GateConfig())


def test_more_messages_and_relation_raise_the_likelihood():
    one = BatchFeatures(message_count=1, question=True)
    three = BatchFeatures(message_count=3, question=True)

    assert score(three, GateConfig()) > score(one, GateConfig())
    assert score(one, GateConfig(), relation_score=50) > score(one, GateConfig())


def test_score_stays_inside_the_unit_interval():
    loud = BatchFeatures(
        message_count=10,
        at_bot=True,
        reply_to_bot=True,
        wake_word=True,
        bot_name=True,
        question=True,
    )

    assert 0.0 < score(loud, GateConfig(), relation_score=100.0) < 1.0
    assert 0.0 < score(BatchFeatures(message_count=1), GateConfig()) < 1.0


def test_idle_rises_the_threshold_and_caps_it():
    config = GateConfig()
    quiet, quiet_reason = dynamic_threshold(
        GateContext(now=1000.0, last_bot_reply_ts=1000.0),
        config,
    )
    idle, idle_reason = dynamic_threshold(
        GateContext(now=1000.0, last_bot_reply_ts=1000.0 - 600),
        config,
    )
    untouched, untouched_reason = dynamic_threshold(
        GateContext(now=1000.0, last_bot_reply_ts=0.0),
        config,
    )

    assert quiet == pytest.approx(config.base_threshold)
    assert quiet_reason == "base"
    assert idle == pytest.approx(config.base_threshold + config.idle_rise_max)
    assert idle_reason == "idle"
    # A session the bot never spoke in is vetoed by idle_silence, not the rise.
    assert untouched == pytest.approx(config.base_threshold)
    assert untouched_reason == "base"


def test_consecutive_waits_raise_the_threshold():
    config = GateConfig()
    once, _ = dynamic_threshold(
        GateContext(now=1000.0, last_bot_reply_ts=1000.0, consecutive_waits=1),
        config,
    )
    many, reason = dynamic_threshold(
        GateContext(now=1000.0, last_bot_reply_ts=1000.0, consecutive_waits=99),
        config,
    )

    assert once == pytest.approx(config.base_threshold + config.backoff_step)
    assert many == pytest.approx(config.base_threshold + config.backoff_max)
    assert reason == "backoff"


def test_threshold_never_exceeds_one():
    config = GateConfig(base_threshold=0.95)
    threshold, _ = dynamic_threshold(
        GateContext(now=1000.0, last_bot_reply_ts=0.0, consecutive_waits=99),
        config,
    )

    assert threshold == 1.0
