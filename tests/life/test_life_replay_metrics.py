"""Unit tests for the S0 replay corpus helpers and metric math."""

from __future__ import annotations

from datetime import datetime

import pytest

from bot.life.replay.corpus import (
    corpus_stats,
    message_text,
    message_timestamp,
    parse_timestamp,
    part_text,
)
from bot.life.replay.metrics import (
    build_report,
    cost_metrics,
    evaluate,
    latency_metrics,
    memory_metrics,
    percentile,
    prf1,
    timing_metrics,
    trigger_metrics,
)


def _decision(session: str, message: str, decide: str, **extra) -> dict:
    return {"session_key": session, "message_id": message, "decide": decide, **extra}


def _label(session: str, message: str, reply: bool, **extra) -> dict:
    return {
        "session_key": session,
        "message_id": message,
        "reply": reply,
        **extra,
    }


def test_parse_timestamp_handles_iso_and_epoch():
    assert parse_timestamp(100) == 100.0
    iso = "2026-11-06T12:00:00"
    assert parse_timestamp(iso) == pytest.approx(
        datetime.fromisoformat(iso).timestamp()
    )
    assert parse_timestamp("not a date") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp(True) is None


def test_part_and_message_text_extraction():
    assert part_text({"type": "plain", "text": "hi"}) == "hi"
    assert part_text({"type": "plain", "data": {"text": "nested"}}) == "nested"
    assert part_text({"type": "image", "file": "a.png"}) == ""
    assert message_text({"parts": [{"type": "plain", "text": "a"}]}) == "a"
    assert message_text({"text": "fallback"}) == "fallback"


def test_corpus_stats_counts_gaps_and_media():
    sessions = [
        {
            "session_key": "s1",
            "messages": [
                {"role": "user", "created_at": "2026-11-06T12:00:00", "parts": []},
                {"role": "bot", "created_at": "2026-11-06T12:00:30", "parts": []},
                {
                    "role": "user",
                    "created_at": "2026-11-06T12:02:30",
                    "parts": [{"type": "image"}],
                },
            ],
        },
        {"session_key": "s2", "messages": []},
    ]

    stats = corpus_stats(sessions)

    assert stats["sessions"] == 2
    assert stats["messages"] == 3
    assert stats["sessions_with_media"] == 1
    assert stats["gap_p50_seconds"] == 30.0
    assert stats["roles"] == {"user": 2, "bot": 1}


def test_prf1_and_percentile_edges():
    assert prf1([], []) == {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    perfect = prf1([("s", "1")], [("s", "1")])
    assert perfect["f1"] == 1.0
    partial = prf1([("s", "1"), ("s", "2")], [("s", "2")])
    assert partial["precision"] == 0.5
    assert partial["recall"] == 1.0
    assert percentile([], 0.5) == 0.0
    assert percentile([1, 2, 3, 4], 0.5) == 2.0
    assert percentile([30, 120], 0.5) == 30.0
    assert percentile([30, 120], 0.9) == 120.0


def test_trigger_metrics_computes_f1_accuracy_and_coverage():
    decisions = [
        _decision("s", "m0", "reply"),
        _decision("s", "m1", "wait"),
        _decision("s", "m2", "reply"),
        _decision("s", "m3", "wait"),
    ]
    labels = [
        _label("s", "m0", True),
        _label("s", "m1", False),
        _label("s", "m2", False),
    ]

    trigger = trigger_metrics(decisions, labels)

    assert trigger["predicted_positive"] == 2
    assert trigger["labeled_positive"] == 1
    assert trigger["precision"] == 0.5
    assert trigger["recall"] == 1.0
    assert trigger["accuracy"] == pytest.approx(0.6667)
    assert trigger["coverage"] == 0.75


def test_trigger_metrics_without_labels_reports_no_accuracy():
    trigger = trigger_metrics([_decision("s", "m0", "wait")], [])

    assert trigger["labeled"] == 0
    assert trigger["coverage"] == 0.0
    assert trigger["accuracy"] is None


def test_timing_metrics_skips_missing_reply_ts():
    decisions = [
        _decision("s", "m0", "reply", ts=1000),
        _decision("s", "m1", "reply", ts=2000),
    ]
    labels = [
        _label("s", "m0", True, reply_ts=1002),
        _label("s", "m1", True),
        _label("s", "m2", False, reply_ts=5000),
    ]

    timing = timing_metrics(decisions, labels)

    assert timing["samples"] == 1
    assert timing["p50_s"] == 2.0
    assert timing["within_3s_p90"] is True


def test_latency_cost_and_memory_blocks():
    decisions = [
        _decision("s", "m0", "reply", latency_ms=10, tokens_in=100, tokens_out=50),
        _decision("s", "m1", "reply", latency_ms=30, tokens_in=100, memory_hits=2),
        _decision("s", "m2", "wait", latency_ms=50, tokens_in=10),
    ]

    latency = latency_metrics(decisions)
    cost = cost_metrics(decisions)
    memory = memory_metrics(decisions)

    assert latency["p50_ms"] == 30.0
    assert latency["p95_ms"] == 50.0
    assert cost["tokens_total"] == 260
    assert cost["replies"] == 2
    assert cost["tokens_per_reply"] == 130.0
    assert memory["hit_rate"] == 0.5
    assert memory["memory_hits"] == 2


def test_build_report_and_evaluate():
    corpus = {"sessions": 1, "messages": 2}
    report = build_report(
        corpus,
        [_decision("s", "m0", "reply", ts=1000), _decision("s", "m1", "wait")],
        [_label("s", "m0", True, reply_ts=1001)],
    )

    assert report["schema"] == 1
    assert report["trigger"]["f1"] == 1.0
    assert report["timing"]["p90_s"] == 1.0

    reference = {
        "trigger": {"f1": 1.0},
        "timing": {"p90_s": 1.0},
        "memory": {"hit_rate": 1.0},
        "latency": {"p95_ms": 100.0},
    }
    evaluation = evaluate(reference, report)

    assert evaluation["trigger_f1_ratio"] == 1.0
    assert evaluation["timing_p90_delta_s"] == 0.0
    assert evaluation["memory_hit_rate_ratio"] == 0.0
    assert evaluation["latency_p95_ratio"] is None
    assert evaluation["checks"]["trigger_f1"]["passed"] is True
    assert evaluation["checks"]["memory_hit_rate"]["passed"] is False


def test_timestamp_fallback_uses_ts_field():
    iso = "2026-11-06T12:00:00"
    assert message_timestamp({"created_at": iso}) == pytest.approx(
        datetime.fromisoformat(iso).timestamp()
    )
    assert message_timestamp({"ts": 42}) == 42.0
    assert message_timestamp({}) is None
