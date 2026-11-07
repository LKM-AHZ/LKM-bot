"""Print the Life diagnostics report from a saved status payload.

Usage:
    python scripts/life/diagnostics.py --status data/life/status.json
    python scripts/life/diagnostics.py --status status.json --out report.json

Exit codes: 0 pass, 1 warn, 2 fail.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.life.diagnostics import derive_metrics, evaluate_checks, exit_code, summarize


def main() -> int:
    """Run the CLI.

    Returns:
        Process exit code: 0 pass, 1 warn, 2 fail.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status", required=True, help="Saved /api/v1/life/status JSON."
    )
    parser.add_argument("--out", help="Write the report JSON here.")
    args = parser.parse_args()

    payload = json.loads(Path(args.status).read_text(encoding="utf-8"))
    # The dashboard wraps payloads in {"status": "ok", "data": {...}}.
    status = payload.get("data", payload) if isinstance(payload, dict) else payload
    metrics = derive_metrics(status)
    checks = evaluate_checks(metrics)
    report = {
        "overall": summarize(checks),
        "metrics": metrics,
        "checks": checks,
    }

    print(f"overall={report['overall']} checks={len(checks)}")
    for check in checks:
        print(f"  [{check['status']:4}] {check['name']}: {check['detail']}")

    heartflow = metrics["heartflow"]
    memory = metrics["memory"]
    print(
        "heartflow: "
        f"turns={heartflow['turns']} replied={heartflow['replied']} "
        f"reply_rate={heartflow['reply_rate']} wait_rate={heartflow['wait_rate']}"
    )
    print(
        "memory: "
        f"total={memory['total']} coverage={memory['coverage']} "
        f"latest_age_days={memory['latest_age_days']}"
    )
    print(
        "review: "
        f"pending_facts={metrics['review']['pending_facts']} "
        f"conflicts={metrics['review']['conflicted_facts']} "
        f"pending_jargon={metrics['review']['pending_jargon']}"
    )

    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
