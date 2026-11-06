"""Metric math for the S0 replay baseline.

A replay run produces a decision stream: for every archived message, whether the
bot replied and at what wall-clock time. Human labels mark the same messages
with the reference answer. This module keeps the arithmetic pure so both the CLI
and the tests share one definition of every metric.

Decision record::

    {"session_key": "...", "message_id": "1", "ts": 1700000000,
     "decide": "reply" | "wait", "reason": "...", "latency_ms": 12.5,
     "tokens_in": 0, "tokens_out": 0, "memory_hits": 0, "cited": false}

Label record::

    {"session_key": "...", "message_id": "1", "reply": true,
     "reply_ts": 1700000003, "cited": false}
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Iterable

Key = tuple[str, str]


def _key(record: dict[str, Any]) -> Key:
    """Return the identity key of a decision or label record.

    Args:
        record: Decision or label dictionary.

    Returns:
        ``(session_key, message_id)`` pair.
    """
    return (
        str(record.get("session_key") or ""),
        str(record.get("message_id") or ""),
    )


def percentile(values: Iterable[float], ratio: float) -> float:
    """Return a nearest-rank percentile.

    Args:
        values: Numeric samples; an empty iterable yields 0.
        ratio: Percentile ratio in ``[0, 1]``.

    Returns:
        Selected value, rounded to two decimals.
    """
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(ratio * len(ordered)))
    return round(ordered[min(rank, len(ordered)) - 1], 2)


def prf1(predicted: Iterable[Key], labeled: Iterable[Key]) -> dict[str, float]:
    """Return precision, recall and F1 for two positive sets.

    Args:
        predicted: Keys the system decided to answer.
        labeled: Keys marked as answer-worthy by a human.

    Returns:
        Mapping with ``precision``, ``recall`` and ``f1``; zeros when both sets
        are empty.
    """
    pred = set(predicted)
    truth = set(labeled)
    if not pred and not truth:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    true_positive = len(pred & truth)
    precision = true_positive / len(pred) if pred else 0.0
    recall = true_positive / len(truth) if truth else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def trigger_metrics(
    decisions: Iterable[dict[str, Any]],
    labels: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Compare reply decisions against human reply labels.

    Args:
        decisions: Decision records.
        labels: Label records; ``reply`` marks the reference decision.

    Returns:
        Mapping with positive-set counts, ``prf1`` output, overall accuracy and
        label coverage over the decision stream.
    """
    decision_list = list(decisions)
    label_list = list(labels)
    predicted = {
        _key(record)
        for record in decision_list
        if str(record.get("decide") or "") == "reply"
    }
    labeled_positive = {
        _key(record) for record in label_list if bool(record.get("reply"))
    }
    label_map = {_key(record): bool(record.get("reply")) for record in label_list}
    correct = sum(
        1
        for record in decision_list
        if _key(record) in label_map
        and (str(record.get("decide") or "") == "reply") == label_map[_key(record)]
    )
    coverage = len(label_map) / len(decision_list) if decision_list else 0.0
    return {
        "decisions": len(decision_list),
        "labeled": len(label_map),
        "coverage": round(coverage, 4),
        "predicted_positive": len(predicted),
        "labeled_positive": len(labeled_positive),
        **prf1(predicted, labeled_positive),
        "accuracy": (round(correct / len(label_map), 4) if label_map else None),
    }


def timing_metrics(
    decisions: Iterable[dict[str, Any]],
    labels: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Measure timing deviation between decisions and labeled reply moments.

    Args:
        decisions: Decision records carrying ``ts``.
        labels: Label records carrying ``reply_ts``.

    Returns:
        Mapping with sample count, mean, P50 and P90 deviation in seconds, and a
        boolean ``within_3s_p90`` matching the plan's timing target.
    """
    decided = {
        _key(record): record.get("ts")
        for record in decisions
        if str(record.get("decide") or "") == "reply"
    }
    deviations: list[float] = []
    for label in labels:
        if not bool(label.get("reply")):
            continue
        key = _key(label)
        decision_ts = decided.get(key)
        label_ts = label.get("reply_ts")
        if decision_ts is None or label_ts is None:
            continue
        deviations.append(abs(float(decision_ts) - float(label_ts)))
    if not deviations:
        return {"samples": 0, "mean_s": None, "p50_s": None, "p90_s": None}
    p90 = percentile(deviations, 0.9)
    return {
        "samples": len(deviations),
        "mean_s": round(statistics.fmean(deviations), 2),
        "p50_s": percentile(deviations, 0.5),
        "p90_s": p90,
        "within_3s_p90": p90 <= 3.0,
    }


def latency_metrics(decisions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize per-message decision latency.

    Args:
        decisions: Decision records with an optional ``latency_ms``.

    Returns:
        Mapping with sample count and P50/P95/max latency in milliseconds.
    """
    values = [
        float(record["latency_ms"])
        for record in decisions
        if record.get("latency_ms") is not None
    ]
    if not values:
        return {"samples": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    return {
        "samples": len(values),
        "p50_ms": percentile(values, 0.5),
        "p95_ms": percentile(values, 0.95),
        "max_ms": round(max(values), 2),
    }


def cost_metrics(decisions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize token usage of one decision stream.

    Args:
        decisions: Decision records with optional ``tokens_in``/``tokens_out``.

    Returns:
        Mapping with token totals and the average cost of a reply.
    """
    decision_list = list(decisions)
    replies = [
        record for record in decision_list if str(record.get("decide") or "") == "reply"
    ]
    tokens_in = sum(int(record.get("tokens_in") or 0) for record in decision_list)
    tokens_out = sum(int(record.get("tokens_out") or 0) for record in decision_list)
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_in + tokens_out,
        "replies": len(replies),
        "tokens_per_reply": (
            round((tokens_in + tokens_out) / len(replies), 1) if replies else 0.0
        ),
    }


def memory_metrics(decisions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize how often retrieved memory accompanied a reply.

    Args:
        decisions: Decision records with an optional ``memory_hits`` count.

    Returns:
        Mapping with the reply count, replies that used memory, and hit rate.
    """
    replies = [
        record for record in decisions if str(record.get("decide") or "") == "reply"
    ]
    with_memory = sum(
        1 for record in replies if int(record.get("memory_hits") or 0) > 0
    )
    total_hits = sum(int(record.get("memory_hits") or 0) for record in replies)
    return {
        "replies": len(replies),
        "replies_with_memory": with_memory,
        "hit_rate": round(with_memory / len(replies), 4) if replies else 0.0,
        "memory_hits": total_hits,
    }


def evaluate(
    reference: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Check a candidate run against the plan's acceptance targets.

    Args:
        reference: Full report of the reference run (for example MaiBot).
        candidate: Full report of the candidate run (for example LKM-bot).

    Returns:
        Mapping with ratios, deltas and a ``checks`` block where each entry has
        ``value``, ``target`` and ``passed``; entries are None when either side
        has no sample.
    """
    trigger_ref = reference.get("trigger") or {}
    trigger_new = candidate.get("trigger") or {}
    timing_ref = reference.get("timing") or {}
    timing_new = candidate.get("timing") or {}
    memory_ref = reference.get("memory") or {}
    memory_new = candidate.get("memory") or {}
    latency_ref = reference.get("latency") or {}
    latency_new = candidate.get("latency") or {}

    f1_ratio = _ratio_from(trigger_ref, trigger_new, "f1")
    timing_delta = _delta(timing_ref, timing_new, "p90_s")
    memory_ratio = _ratio_from(memory_ref, memory_new, "hit_rate")
    latency_ratio = _ratio_from(latency_ref, latency_new, "p95_ms")

    checks = {
        "trigger_f1": {
            "value": f1_ratio,
            "target": 0.9,
            "passed": None if f1_ratio is None else f1_ratio >= 0.9,
        },
        "timing_p90_delta_s": {
            "value": timing_delta,
            "target": 3.0,
            "passed": None if timing_delta is None else timing_delta <= 3.0,
        },
        "memory_hit_rate": {
            "value": memory_ratio,
            "target": 0.9,
            "passed": None if memory_ratio is None else memory_ratio >= 0.9,
        },
        "latency_p95": {
            "value": latency_ratio,
            "target": 1.3,
            "passed": None if latency_ratio is None else latency_ratio <= 1.3,
        },
    }
    return {
        "trigger_f1_ratio": f1_ratio,
        "timing_p90_delta_s": timing_delta,
        "memory_hit_rate_ratio": memory_ratio,
        "latency_p95_ratio": latency_ratio,
        "checks": checks,
    }


def _ratio_from(
    reference: dict[str, Any],
    candidate: dict[str, Any],
    key: str,
) -> float | None:
    """Divide one candidate metric by the reference metric.

    Args:
        reference: Reference metric block.
        candidate: Candidate metric block.
        key: Metric name inside both blocks.

    Returns:
        Ratio, or None when the reference value is missing or zero.
    """
    base = reference.get(key)
    other = candidate.get(key)
    if not base or other is None:
        return None
    return round(float(other) / float(base), 4)


def _delta(
    reference: dict[str, Any],
    candidate: dict[str, Any],
    key: str,
) -> float | None:
    """Subtract one reference metric from the candidate metric.

    Args:
        reference: Reference metric block.
        candidate: Candidate metric block.
        key: Metric name inside both blocks.

    Returns:
        Delta, or None when either value is missing.
    """
    base = reference.get(key)
    other = candidate.get(key)
    if base is None or other is None:
        return None
    return round(float(other) - float(base), 2)


def build_report(
    corpus: dict[str, Any],
    decisions: Iterable[dict[str, Any]],
    labels: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the full baseline report.

    Args:
        corpus: Corpus statistics (sessions, messages, gap percentiles).
        decisions: Decision records of one run.
        labels: Human labels; omitted or empty means trigger/timing are null.

    Returns:
        JSON-serializable report with ``trigger``, ``timing``, ``latency``,
        ``cost`` and ``memory`` blocks.
    """
    decision_list = list(decisions)
    label_list = list(labels or [])
    trigger = trigger_metrics(decision_list, label_list)
    timing = timing_metrics(decision_list, label_list)
    return {
        "schema": 1,
        "corpus": corpus,
        "trigger": trigger,
        "timing": timing,
        "latency": latency_metrics(decision_list),
        "cost": cost_metrics(decision_list),
        "memory": memory_metrics(decision_list),
    }
