"""Compute the S0 baseline metrics from a corpus and a decision stream.

Outputs the five plan metrics: trigger precision/recall/F1, timing deviation,
memory hit rate, token cost and P95 latency. Human labels are optional; without
them the trigger and timing blocks report zero coverage instead of inventing a
number. Pass ``--reference`` to check the candidate against the MaiBot baseline
and get an exit code that CI can use.

Examples:
    python scripts/life/life_metrics.py \
      --corpus data/life/replay/replay.jsonl \
      --decisions data/life/replay/decisions.jsonl \
      --labels data/life/replay/labels.jsonl \
      --out data/life/replay/baseline.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot.life.replay.corpus import corpus_stats, load_sessions  # noqa: E402
from bot.life.replay.metrics import build_report, evaluate  # noqa: E402


def _load_jsonl(path: str | None) -> list[dict[str, Any]]:
    """Load a JSONL file of decision or label records.

    Args:
        path: File path; None yields an empty list.

    Returns:
        Parsed records, skipping malformed lines.

    Raises:
        FileNotFoundError: When a path was given but does not exist.
    """
    if not path:
        return []
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(source)
    records: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _print_summary(report: dict[str, Any]) -> None:
    """Print the report as a compact markdown table.

    Args:
        report: Baseline report produced by :func:`build_report`.
    """
    trigger = report["trigger"]
    timing = report["timing"]
    latency = report["latency"]
    memory = report["memory"]
    cost = report["cost"]
    rows = [
        (
            "sessions / messages",
            f"{report['corpus']['sessions']} / {report['corpus']['messages']}",
        ),
        ("label coverage", f"{trigger['coverage']:.0%}"),
        (
            "trigger P / R / F1",
            f"{trigger['precision']} / {trigger['recall']} / {trigger['f1']}",
        ),
        (
            "timing P50 / P90 (s)",
            f"{timing.get('p50_s')} / {timing.get('p90_s')}",
        ),
        (
            "latency P50 / P95 (ms)",
            f"{latency.get('p50_ms')} / {latency.get('p95_ms')}",
        ),
        ("memory hit rate", f"{memory['hit_rate']:.0%}"),
        ("tokens total", str(cost["tokens_total"])),
    ]
    for label, value in rows:
        print(f"| {label} | {value} |")


def main() -> int:
    """Compute and persist the baseline report.

    Returns:
        Process exit code: 0 normally, 2 when a reference check failed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, help="Path to replay.jsonl")
    parser.add_argument("--decisions", required=True, help="Decision stream JSONL")
    parser.add_argument("--labels", default="", help="Optional human labels JSONL")
    parser.add_argument(
        "--out",
        default="data/life/replay/baseline.json",
        help="Report output path",
    )
    parser.add_argument(
        "--reference",
        default="",
        help="Reference report (MaiBot) used for the acceptance checks",
    )
    args = parser.parse_args()

    try:
        sessions = load_sessions(args.corpus)
        decisions = _load_jsonl(args.decisions)
        labels = _load_jsonl(args.labels)
    except FileNotFoundError as exc:
        print(f"file not found: {exc}", file=sys.stderr)
        return 1

    report = build_report(corpus_stats(sessions), decisions, labels)
    if args.reference:
        reference_path = Path(args.reference)
        if not reference_path.exists():
            print(f"reference not found: {reference_path}", file=sys.stderr)
            return 1
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
        report["evaluation"] = evaluate(reference, report)

    print("| metric | value |")
    print("| --- | --- |")
    _print_summary(report)
    if "evaluation" in report:
        print("| check | value | target | passed |")
        print("| --- | --- | --- | --- |")
        for name, check in report["evaluation"]["checks"].items():
            print(
                f"| {name} | {check['value']} | {check['target']} | {check['passed']} |"
            )

    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"written: {output_path}")

    checks = (report.get("evaluation") or {}).get("checks") or {}
    if any(check.get("passed") is False for check in checks.values()):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
