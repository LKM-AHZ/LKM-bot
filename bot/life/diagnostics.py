"""Statistics and diagnostics for the Life layer (S6g).

The report reuses the status snapshot every service already exposes, derives a
few ratios the panel wants (reply rate, vector coverage, review backlog) and
turns them into pass/warn checks. It is deliberately read-only: a diagnostic
never changes configuration, sends a message or triggers a learning round.
"""

from __future__ import annotations

import time
from typing import Any

PENDING_REVIEW_WARN = 20
"""Pending facts plus pending slang terms before review is considered behind."""

VECTOR_COVERAGE_WARN = 0.5
"""Share of memories with an embedding below which retrieval is degraded."""

MEMORY_STALE_DAYS_WARN = 7
"""Age of the newest memory before the digest is considered stale."""

TOOL_RESULT_FAILURE_WARN = 0.3
"""Share of failed condense calls that suggests the provider is unhealthy."""

SUBAGENT_FAILURE_WARN = 0.3
"""Share of failed sub-agent runs."""

ACP_FAILURE_WARN = 0.3
"""Share of ACP requests that failed or timed out."""

REPLY_RATE_MIN_WARN = 0.02
"""Below this the heart-flow gate may be too strict."""

REPLY_RATE_MAX_WARN = 0.8
"""Above this the gate may be answering everything."""

MIN_SAMPLE = 20
"""Sample size below which ratios are not judged."""


def _ratio(part: int, whole: int) -> float:
    """Return ``part / whole`` as a float, or 0.0 for an empty denominator.

    Args:
        part: Numerator.
        whole: Denominator.

    Returns:
        Ratio in ``[0, 1]`` when the inputs are sane, otherwise 0.0.
    """
    if whole <= 0:
        return 0.0
    return round(min(max(part / whole, 0.0), 1.0), 3)


def derive_metrics(status: dict[str, Any]) -> dict[str, Any]:
    """Derive the numbers the diagnostics checks need from a status snapshot.

    Args:
        status: Payload returned by ``LifeService.status``.

    Returns:
        Nested metric mapping (heart-flow, memory, review backlog and the
        per-feature counters).
    """
    heartflow = status.get("heartflow") or {}
    hf_counters = heartflow.get("counters") or {}
    turns = int(hf_counters.get("turns") or 0)
    replied = int(hf_counters.get("replied") or 0)
    merged = int(hf_counters.get("merged") or 0)

    memory = status.get("memory") or {}
    memory_storage = memory.get("storage") or {}
    total = int(memory_storage.get("total") or 0)
    embedded = int(memory_storage.get("embedded") or 0)
    latest_ts = memory_storage.get("latest_ts")
    latest_age_days = (
        round((time.time() - float(latest_ts)) / 86400, 1) if latest_ts else None
    )

    person_storage = (status.get("person") or {}).get("storage") or {}
    facts_by_status = person_storage.get("facts_by_status") or {}
    jargon_storage = (status.get("jargon") or {}).get("storage") or {}
    jargon_by_status = jargon_storage.get("by_status") or {}

    tool_result = status.get("tool_result") or {}
    tr_counters = tool_result.get("counters") or {}
    subagent = status.get("subagent") or {}
    sub_counters = subagent.get("counters") or {}
    monitor = status.get("monitor") or {}
    files = status.get("files") or {}
    acp = status.get("acp") or {}
    reaction = status.get("reaction") or {}

    return {
        "heartflow": {
            "enabled": bool(heartflow.get("enabled")),
            "shadow": bool(heartflow.get("shadow")),
            "turns": turns,
            "replied": replied,
            "waited": int(hf_counters.get("waited") or 0),
            "merged": merged,
            "capacity_drops": int(hf_counters.get("capacity_drops") or 0),
            "aborted": int(hf_counters.get("aborted") or 0),
            "reply_rate": _ratio(replied, turns),
            "wait_rate": _ratio(int(hf_counters.get("waited") or 0), turns),
            "merge_rate": _ratio(merged, turns),
        },
        "memory": {
            "enabled": bool(memory.get("enabled")),
            "total": total,
            "embedded": embedded,
            "coverage": _ratio(embedded, total),
            "embedding_enabled": bool((memory.get("embedding") or {}).get("enabled")),
            "latest_age_days": latest_age_days,
            "by_kind": memory_storage.get("by_kind") or {},
        },
        "review": {
            "pending_facts": int(facts_by_status.get("pending") or 0),
            "conflicted_facts": int(
                facts_by_status.get("conflicted")
                or person_storage.get("conflicts")
                or 0
            ),
            "pending_jargon": int(jargon_by_status.get("pending") or 0),
        },
        "tool_result": {
            "attempts": int(tr_counters.get("attempts") or 0),
            "failed": int(tr_counters.get("failed") or 0),
            "chars_saved": int(tr_counters.get("chars_saved") or 0),
        },
        "subagent": {
            "started": int(sub_counters.get("started") or 0),
            "completed": int(sub_counters.get("completed") or 0),
            "failed": int(sub_counters.get("failed") or 0),
        },
        "monitor": {
            "records": int(monitor.get("records") or 0),
            "dropped": int((monitor.get("counters") or {}).get("dropped") or 0),
            "deliveries": int((monitor.get("counters") or {}).get("deliveries") or 0),
        },
        "files": {
            "files": int((files.get("store") or {}).get("files") or 0),
            "max_files": int(files.get("max_files") or 0),
            "sent": int((files.get("counters") or {}).get("sent") or 0),
        },
        "acp": {
            "enabled": bool(acp.get("enabled")),
            "asks": int((acp.get("counters") or {}).get("asks") or 0),
            "failed": int((acp.get("counters") or {}).get("failed") or 0),
            "timeouts": int((acp.get("counters") or {}).get("timeouts") or 0),
        },
        "reaction": {
            "sent": int((reaction.get("counters") or {}).get("sent") or 0),
            "failed": int((reaction.get("counters") or {}).get("failed") or 0),
        },
    }


def _check(name: str, ok: bool, detail: str, *, warn: bool = False) -> dict[str, Any]:
    """Build one check entry.

    Args:
        name: Check name.
        ok: Whether the check passes.
        detail: Human-readable explanation.
        warn: When the check fails, report ``warn`` instead of ``fail``.

    Returns:
        Check dictionary.
    """
    if ok:
        return {"name": name, "status": "pass", "detail": detail}
    return {"name": name, "status": "warn" if warn else "fail", "detail": detail}


def evaluate_checks(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn derived metrics into pass/warn checks.

    Args:
        metrics: Output of :func:`derive_metrics`.

    Returns:
        Ordered list of check dictionaries.
    """
    checks: list[dict[str, Any]] = []
    heartflow = metrics["heartflow"]
    memory = metrics["memory"]
    review = metrics["review"]
    tool_result = metrics["tool_result"]
    subagent = metrics["subagent"]
    monitor = metrics["monitor"]
    files = metrics["files"]
    acp = metrics["acp"]
    reaction = metrics["reaction"]

    if heartflow["enabled"]:
        turns = heartflow["turns"]
        if turns < MIN_SAMPLE:
            checks.append(
                _check(
                    "heartflow_reply_rate",
                    True,
                    f"only {turns} turn(s); ratio not judged yet",
                )
            )
        else:
            rate = heartflow["reply_rate"]
            checks.append(
                _check(
                    "heartflow_reply_rate",
                    REPLY_RATE_MIN_WARN <= rate <= REPLY_RATE_MAX_WARN,
                    f"reply rate {rate:.3f} over {turns} turns",
                    warn=True,
                )
            )
        drops = heartflow["capacity_drops"]
        checks.append(
            _check(
                "heartflow_capacity",
                drops == 0,
                f"{drops} capacity drop(s)",
                warn=True,
            )
        )
    else:
        checks.append(
            _check("heartflow_reply_rate", True, "heart-flow disabled", warn=True)
        )

    if memory["enabled"] and memory["total"]:
        coverage = memory["coverage"]
        if memory["embedding_enabled"] and memory["total"] >= 10:
            checks.append(
                _check(
                    "memory_vector_coverage",
                    coverage >= VECTOR_COVERAGE_WARN,
                    f"{memory['embedded']}/{memory['total']} memories embedded "
                    f"({coverage:.2f})",
                    warn=True,
                )
            )
        else:
            checks.append(
                _check(
                    "memory_vector_coverage",
                    True,
                    "vector search disabled or too few memories",
                    warn=True,
                )
            )
        age = memory["latest_age_days"]
        checks.append(
            _check(
                "memory_freshness",
                age is not None and age <= MEMORY_STALE_DAYS_WARN,
                f"newest memory is {age} days old"
                if age is not None
                else "no memory yet",
                warn=True,
            )
        )
    else:
        checks.append(
            _check(
                "memory_vector_coverage",
                True,
                "memory disabled or empty",
                warn=True,
            )
        )

    backlog = (
        review["pending_facts"] + review["conflicted_facts"] + review["pending_jargon"]
    )
    checks.append(
        _check(
            "memory_review_backlog",
            backlog <= PENDING_REVIEW_WARN,
            f"{backlog} item(s) awaiting review "
            f"(facts {review['pending_facts']}, conflicts "
            f"{review['conflicted_facts']}, slang {review['pending_jargon']})",
            warn=True,
        )
    )

    attempts = tool_result["attempts"]
    failed = tool_result["failed"]
    checks.append(
        _check(
            "tool_result_condense",
            not (
                attempts >= 10 and _ratio(failed, attempts) > TOOL_RESULT_FAILURE_WARN
            ),
            f"{failed}/{attempts} condense call(s) failed, "
            f"{tool_result['chars_saved']} chars saved",
            warn=True,
        )
    )

    started = subagent["started"]
    checks.append(
        _check(
            "subagent_health",
            not (
                started >= 5
                and _ratio(subagent["failed"], started) > SUBAGENT_FAILURE_WARN
            ),
            f"{subagent['failed']}/{started} sub-agent run(s) failed",
            warn=True,
        )
    )

    checks.append(
        _check(
            "monitor_ring",
            monitor["dropped"] == 0,
            f"{monitor['records']} record(s) kept, {monitor['dropped']} dropped",
            warn=True,
        )
    )

    if files["max_files"]:
        checks.append(
            _check(
                "files_store",
                files["files"] < files["max_files"],
                f"{files['files']} file(s) stored of {files['max_files']} allowed",
                warn=True,
            )
        )

    if acp["enabled"]:
        failure = _ratio(acp["failed"] + acp["timeouts"], acp["asks"])
        checks.append(
            _check(
                "acp_health",
                not (acp["asks"] >= 5 and failure > ACP_FAILURE_WARN),
                f"{acp['failed']} failed / {acp['timeouts']} timed out "
                f"of {acp['asks']} ask(s)",
                warn=True,
            )
        )

    if reaction["sent"] or reaction["failed"]:
        checks.append(
            _check(
                "reaction_health",
                _ratio(reaction["failed"], reaction["sent"] + reaction["failed"])
                <= 0.5,
                f"{reaction['failed']} failed of "
                f"{reaction['sent'] + reaction['failed']} reaction(s)",
                warn=True,
            )
        )

    return checks


def summarize(checks: list[dict[str, Any]]) -> str:
    """Return the worst status of a check list.

    Args:
        checks: Check dictionaries.

    Returns:
        ``fail`` when any check failed, else ``warn`` when any warned, else
        ``pass``.
    """
    statuses = {check["status"] for check in checks}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "pass"


def exit_code(report: dict[str, Any]) -> int:
    """Map an overall status to the CLI exit code.

    Args:
        report: Diagnostics report.

    Returns:
        0 for pass, 1 for warn, 2 for fail.
    """
    return {"pass": 0, "warn": 1, "fail": 2}.get(str(report.get("overall")), 2)


async def build_report(service: Any) -> dict[str, Any]:
    """Build the diagnostics report for a running Life service.

    Args:
        service: ``LifeService`` whose status is being inspected.

    Returns:
        Mapping with ``overall``, ``metrics``, ``checks`` and ``generated_at``.
    """
    try:
        status = await service.status()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        checks = [_check("life_status", False, detail)]
        return {
            "overall": "fail",
            "metrics": {},
            "checks": checks,
            "generated_at": time.time(),
        }
    metrics = derive_metrics(status)
    checks = evaluate_checks(metrics)
    return {
        "overall": summarize(checks),
        "metrics": metrics,
        "checks": checks,
        "generated_at": time.time(),
    }
