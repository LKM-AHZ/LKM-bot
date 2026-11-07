"""Style metrics for the reply layer (S5d).

Pure helpers behind ``scripts/life/style_metrics.py``. Two inputs are supported:
a JSONL sample of outbound messages, or a saved ``/api/v1/life/status`` payload
whose ``reply.recent`` ring carries lengths, segment counts, quote flags and
Markdown leaks without storing text.

The gates mirror the acceptance criteria of S5: length distribution not
significantly different from the reference (two-sample KS, ``p > alpha``), quote
rate within tolerance and zero Markdown leaks.
"""

from __future__ import annotations

import json
import math
import re
from bisect import bisect_right
from pathlib import Path
from typing import Any

_MARKDOWN = (
    re.compile(r"^\s*#{1,6}\s", re.M),
    re.compile(r"\*\*"),
    re.compile(r"```"),
    re.compile(r"^\s*\|.*\|\s*$", re.M),
    re.compile(r"\[[^\]]+\]\([^)]+\)"),
)


def percentile(values: list[float], ratio: float) -> float:
    """Return a percentile with linear interpolation.

    Args:
        values: Sample values.
        ratio: Position in ``0..1``.

    Returns:
        The interpolated percentile, or 0.0 for an empty sample.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    position = min(max(ratio, 0.0), 1.0) * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[low])
    weight = position - low
    return float(ordered[low] * (1 - weight) + ordered[high] * weight)


def ks_2samp(sample: list[float], reference: list[float]) -> tuple[float, float]:
    """Two-sample Kolmogorov-Smirnov statistic and asymptotic p-value.

    Args:
        sample: Observed values.
        reference: Reference values.

    Returns:
        ``(D, p)``; ``p`` is 1.0 when either side is empty.
    """
    if not sample or not reference:
        return 0.0, 1.0
    left = sorted(sample)
    right = sorted(reference)
    n1, n2 = len(left), len(right)
    distance = 0.0
    for value in sorted(set(left + right)):
        cdf1 = bisect_right(left, value) / n1
        cdf2 = bisect_right(right, value) / n2
        distance = max(distance, abs(cdf1 - cdf2))
    effective = n1 * n2 / (n1 + n2)
    lam = (math.sqrt(effective) + 0.12 + 0.11 / math.sqrt(effective)) * distance
    if lam <= 0:
        return distance, 1.0
    p_value = 2 * sum(
        (-1) ** (index - 1) * math.exp(-2 * index * index * lam * lam)
        for index in range(1, 101)
    )
    return distance, min(max(p_value, 0.0), 1.0)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL file, skipping blank lines.

    Args:
        path: File path.

    Returns:
        Parsed records.

    Raises:
        ValueError: When a line is not a JSON object.
    """
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number} is not valid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"{path}:{number} is not a JSON object")
        records.append(record)
    return records


def status_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the reply ring from a saved status payload.

    Args:
        payload: Parsed ``/api/v1/life/status`` body.

    Returns:
        Recent reply entries.

    Raises:
        ValueError: When the payload has no reply ring.
    """
    data = payload.get("data", payload)
    reply = data.get("reply") if isinstance(data, dict) else None
    recent = reply.get("recent") if isinstance(reply, dict) else None
    if not isinstance(recent, list):
        raise ValueError("payload has no reply.recent ring")
    return [record for record in recent if isinstance(record, dict)]


def _lengths(records: list[dict[str, Any]]) -> list[float]:
    """Return the message length of each record.

    Args:
        records: Sample records.

    Returns:
        Character counts, preferring the ``text`` field over ``chars``.
    """
    lengths: list[float] = []
    for record in records:
        text = record.get("text")
        if isinstance(text, str):
            lengths.append(float(len(text)))
            continue
        chars = record.get("chars")
        if isinstance(chars, int):
            lengths.append(float(chars))
    return lengths


def _leaks(records: list[dict[str, Any]]) -> int:
    """Count records that still carry Markdown or a recorded leak flag.

    Args:
        records: Sample records.

    Returns:
        Number of leaking records.
    """
    count = 0
    for record in records:
        if record.get("leak") is True:
            count += 1
            continue
        text = record.get("text")
        if isinstance(text, str) and any(p.search(text) for p in _MARKDOWN):
            count += 1
    return count


def evaluate(
    records: list[dict[str, Any]],
    reference: list[dict[str, Any]] | None = None,
    alpha: float = 0.05,
    expected_quote_rate: float | None = None,
    quote_tolerance: float = 0.1,
) -> tuple[dict[str, Any], int]:
    """Compute the style report and the gate exit code.

    Args:
        records: Outbound message records.
        reference: Reference corpus records, if any.
        alpha: Significance level of the KS test.
        expected_quote_rate: Target quote rate, if it should be checked.
        quote_tolerance: Allowed absolute deviation of the quote rate.

    Returns:
        ``(report, exit_code)`` with 0 pass, 2 distribution, 3 leak, 4 quote.
    """
    lengths = _lengths(records)
    segments = [int(record.get("segments") or 1) for record in records]
    quoted = sum(1 for record in records if record.get("quoted") is True)
    leaks = _leaks(records)
    report: dict[str, Any] = {
        "replies": len(records),
        "length": {
            "mean": round(sum(lengths) / len(lengths), 1),
            "median": percentile(lengths, 0.5),
            "p90": percentile(lengths, 0.9),
            "max": max(lengths),
        },
        "segments": {
            "mean": round(sum(segments) / len(segments), 2),
            "p90": percentile([float(value) for value in segments], 0.9),
            "share_multi": round(
                sum(1 for value in segments if value > 1) / len(segments), 3
            ),
        },
        "quote_rate": round(quoted / len(records), 3),
        "markdown_leaks": leaks,
        "gates": {},
    }

    exit_code = 0
    if reference:
        distance, p_value = ks_2samp(lengths, _lengths(reference))
        significant = p_value < alpha
        report["reference"] = {
            "replies": len(reference),
            "length_mean": round(sum(_lengths(reference)) / len(reference), 1),
            "ks_d": round(distance, 3),
            "ks_p": round(p_value, 4),
            "alpha": alpha,
            "significant": significant,
        }
        report["gates"]["length_distribution"] = "fail" if significant else "pass"
        if significant:
            exit_code = 2

    report["gates"]["markdown_leak_free"] = "pass" if leaks == 0 else "fail"
    if leaks and exit_code == 0:
        exit_code = 3

    if expected_quote_rate is not None:
        delta = abs(report["quote_rate"] - expected_quote_rate)
        within = delta <= quote_tolerance
        report["gates"]["quote_rate"] = {
            "expected": expected_quote_rate,
            "actual": report["quote_rate"],
            "tolerance": quote_tolerance,
            "pass": within,
        }
        if not within and exit_code == 0:
            exit_code = 4

    return report, exit_code
