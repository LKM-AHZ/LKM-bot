"""Unit tests for the S5d reply style metrics and gates."""

from __future__ import annotations

import json

import pytest

from bot.life.reply.style import (
    evaluate,
    ks_2samp,
    percentile,
    read_jsonl,
    status_records,
)


def _records(texts: list[str], segments: int = 1, quoted: bool = False):
    return [{"text": text, "segments": segments, "quoted": quoted} for text in texts]


def test_percentile_interpolates_between_values():
    values = [10.0, 20.0, 30.0, 40.0]

    assert percentile(values, 0.5) == 25.0
    assert percentile(values, 1.0) == 40.0
    assert percentile([], 0.5) == 0.0


def test_identical_samples_are_not_significant():
    sample = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]

    distance, p_value = ks_2samp(sample, list(sample))

    assert distance == 0.0
    assert p_value > 0.9


def test_shifted_samples_are_significant():
    sample = [10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0]
    reference = [80.0, 90.0, 100.0, 110.0, 120.0, 130.0, 140.0, 150.0]

    distance, p_value = ks_2samp(sample, reference)

    assert distance == 1.0
    assert p_value < 0.05


def test_empty_side_is_not_significant():
    assert ks_2samp([], [1.0]) == (0.0, 1.0)


def test_read_jsonl_skips_blank_lines(tmp_path):
    path = tmp_path / "sample.jsonl"
    path.write_text('{"text": "a"}\n\n{"text": "b"}\n', encoding="utf-8")

    assert [record["text"] for record in read_jsonl(path)] == ["a", "b"]


def test_read_jsonl_rejects_invalid_lines(tmp_path):
    path = tmp_path / "sample.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")

    with pytest.raises(ValueError):
        read_jsonl(path)


def test_status_records_accepts_the_wrapped_payload():
    payload = {"data": {"reply": {"recent": [{"chars": 12, "segments": 2}]}}}

    assert status_records(payload) == [{"chars": 12, "segments": 2}]


def test_status_records_rejects_a_payload_without_a_ring():
    with pytest.raises(ValueError):
        status_records({"data": {"reply": {"enabled": True}}})


def test_report_summarizes_lengths_segments_and_quotes():
    records = _records(
        ["一二三。", "四五六七八九十。", "短。"], segments=2, quoted=True
    )

    report, exit_code = evaluate(records)

    assert report["replies"] == 3
    assert report["length"]["median"] == 4.0
    assert report["segments"]["share_multi"] == 1.0
    assert report["quote_rate"] == 1.0
    assert exit_code == 0


def test_markdown_leaks_are_detected_and_fail_the_gate():
    records = _records(["**加粗**没有剥掉", "```代码块```", "干净的回复。"])

    report, exit_code = evaluate(records)

    assert report["markdown_leaks"] == 2
    assert report["gates"]["markdown_leak_free"] == "fail"
    assert exit_code == 3


def test_recorded_leak_flag_is_honoured():
    report, _ = evaluate([{"chars": 10, "segments": 1, "leak": True}])

    assert report["markdown_leaks"] == 1


def test_length_gate_fails_on_a_shifted_reference():
    records = _records(["短。"] * 8)
    reference = _records(["这是一段明显长很多的回复内容，用来把分布拉开。"] * 8)

    report, exit_code = evaluate(records, reference=reference)

    assert report["gates"]["length_distribution"] == "fail"
    assert exit_code == 2


def test_length_gate_passes_on_a_matching_reference():
    records = _records(["一二三四五。", "六七八九十。"] * 4)
    reference = _records(["五四三二一。", "十九八七六。"] * 4)

    report, exit_code = evaluate(records, reference=reference)

    assert report["gates"]["length_distribution"] == "pass"
    assert exit_code == 0


def test_quote_gate_fails_when_out_of_tolerance():
    records = _records(["好的。"], quoted=False)

    report, exit_code = evaluate(records, expected_quote_rate=0.5, quote_tolerance=0.1)

    assert report["gates"]["quote_rate"]["pass"] is False
    assert exit_code == 4


def test_quote_gate_passes_when_within_tolerance():
    records = _records(["好的。"], quoted=True)

    report, exit_code = evaluate(records, expected_quote_rate=0.9, quote_tolerance=0.2)

    assert report["gates"]["quote_rate"]["pass"] is True
    assert exit_code == 0


def test_reference_report_carries_the_ks_result():
    records = _records(["一二三。"] * 4)
    reference = _records(["四五六。"] * 4)

    report, _ = evaluate(records, reference=reference)

    assert report["reference"]["replies"] == 4
    assert 0.0 <= report["reference"]["ks_p"] <= 1.0


def test_json_round_trip_of_the_report():
    report, _ = evaluate(_records(["好的。"]))

    encoded = json.dumps(report, ensure_ascii=False)

    assert json.loads(encoded)["replies"] == 1
