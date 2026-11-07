"""Command line for the S5d reply style metrics.

Thin wrapper around :mod:`bot.life.reply.style`; the gates are documented in
``scripts/life/README.md``:

* ``--sample`` JSONL of outbound messages (``{"text": ...}``);
* ``--status`` saved ``/api/v1/life/status`` payload (uses ``reply.recent``);
* ``--reference`` JSONL of reference messages for the two-sample KS test.

Exit codes: 0 pass, 2 length distribution differs, 3 Markdown leak, 4 quote rate
outside tolerance.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bot.life.reply.style import evaluate, read_jsonl, status_records


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, evaluate the sample and print the report.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description="Life reply style metrics (S5d)")
    parser.add_argument("--sample", type=Path, help="JSONL of outbound messages")
    parser.add_argument("--status", type=Path, help="saved /life/status JSON")
    parser.add_argument("--reference", type=Path, help="JSONL of reference messages")
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--expected-quote-rate", type=float, default=None)
    parser.add_argument("--quote-tolerance", type=float, default=0.1)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if bool(args.sample) == bool(args.status):
        parser.error("pass exactly one of --sample or --status")
    try:
        if args.sample:
            records = read_jsonl(args.sample)
        else:
            records = status_records(
                json.loads(args.status.read_text(encoding="utf-8"))
            )
        reference = read_jsonl(args.reference) if args.reference else None
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not records:
        print("error: sample is empty", file=sys.stderr)
        return 1

    report, exit_code = evaluate(
        records,
        reference=reference,
        alpha=args.alpha,
        expected_quote_rate=args.expected_quote_rate,
        quote_tolerance=args.quote_tolerance,
    )
    print(
        f"replies={report['replies']} "
        f"len(median/p90/max)={report['length']['median']}/"
        f"{report['length']['p90']}/{report['length']['max']} "
        f"segments(mean/share_multi)={report['segments']['mean']}/"
        f"{report['segments']['share_multi']} "
        f"quote_rate={report['quote_rate']} leaks={report['markdown_leaks']}"
    )
    if "reference" in report:
        print(
            f"KS D={report['reference']['ks_d']} "
            f"p={report['reference']['ks_p']} "
            f"alpha={report['reference']['alpha']} "
            f"significant={report['reference']['significant']}"
        )
    print(f"gates={report['gates']} exit={exit_code}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
