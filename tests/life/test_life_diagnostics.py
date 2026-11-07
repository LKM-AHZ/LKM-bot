"""Tests for the Life diagnostics report: metrics, checks and exit codes."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from bot.life.diagnostics import (
    PENDING_REVIEW_WARN,
    build_report,
    derive_metrics,
    evaluate_checks,
    exit_code,
    summarize,
)


def _status(**overrides) -> dict:
    base = {
        "enabled": True,
        "configured": True,
        "heartflow": {
            "enabled": True,
            "shadow": False,
            "counters": {
                "turns": 100,
                "merged": 30,
                "replied": 40,
                "waited": 60,
                "shadowed": 0,
                "capacity_drops": 0,
                "aborted": 0,
            },
        },
        "memory": {
            "enabled": True,
            "storage": {"total": 100, "embedded": 90, "latest_ts": int(time.time())},
            "embedding": {"enabled": True},
        },
        "person": {"storage": {"facts_by_status": {"active": 10, "pending": 2}}},
        "jargon": {"storage": {"by_status": {"active": 5, "pending": 3}}},
        "tool_result": {"counters": {"attempts": 20, "failed": 1, "chars_saved": 900}},
        "subagent": {"counters": {"started": 10, "completed": 9, "failed": 1}},
        "monitor": {"records": 50, "counters": {"dropped": 0, "deliveries": 5}},
        "files": {"store": {"files": 10}, "max_files": 500, "counters": {"sent": 3}},
        "acp": {"enabled": False, "counters": {"asks": 0, "failed": 0, "timeouts": 0}},
        "reaction": {"counters": {"sent": 2, "failed": 0}},
    }
    base.update(overrides)
    return base


def _checks_by_name(checks: list[dict]) -> dict[str, dict]:
    return {check["name"]: check for check in checks}


def test_derive_metrics_computes_rates() -> None:
    metrics = derive_metrics(_status())
    assert metrics["heartflow"]["reply_rate"] == 0.4
    assert metrics["heartflow"]["wait_rate"] == 0.6
    assert metrics["heartflow"]["merge_rate"] == 0.3
    assert metrics["memory"]["coverage"] == 0.9
    assert metrics["review"]["pending_facts"] == 2
    assert metrics["review"]["pending_jargon"] == 3
    assert metrics["tool_result"]["chars_saved"] == 900


def test_health_status_reports_all_pass() -> None:
    checks = evaluate_checks(derive_metrics(_status()))
    assert summarize(checks) == "pass"
    assert {check["status"] for check in checks} == {"pass"}


def test_heartflow_reply_rate_anomalies_warn() -> None:
    silent = _status(
        heartflow={
            "enabled": True,
            "shadow": False,
            "counters": {"turns": 100, "replied": 0, "waited": 100},
        }
    )
    checks = _checks_by_name(evaluate_checks(derive_metrics(silent)))
    assert checks["heartflow_reply_rate"]["status"] == "warn"

    chatty = _status(
        heartflow={
            "enabled": True,
            "shadow": False,
            "counters": {"turns": 100, "replied": 95, "waited": 5},
        }
    )
    checks = _checks_by_name(evaluate_checks(derive_metrics(chatty)))
    assert checks["heartflow_reply_rate"]["status"] == "warn"


def test_small_sample_is_not_judged() -> None:
    tiny = _status(
        heartflow={
            "enabled": True,
            "shadow": False,
            "counters": {"turns": 3, "replied": 0, "waited": 3},
        }
    )
    checks = _checks_by_name(evaluate_checks(derive_metrics(tiny)))
    assert checks["heartflow_reply_rate"]["status"] == "pass"
    assert "not judged" in checks["heartflow_reply_rate"]["detail"]


def test_memory_checks_warn_on_stale_and_low_coverage() -> None:
    stale = _status(
        memory={
            "enabled": True,
            "storage": {
                "total": 100,
                "embedded": 10,
                "latest_ts": int(time.time()) - 30 * 86400,
            },
            "embedding": {"enabled": True},
        }
    )
    checks = _checks_by_name(evaluate_checks(derive_metrics(stale)))
    assert checks["memory_vector_coverage"]["status"] == "warn"
    assert checks["memory_freshness"]["status"] == "warn"


def test_review_backlog_warns() -> None:
    backlog = _status(
        person={
            "storage": {
                "facts_by_status": {"pending": PENDING_REVIEW_WARN + 1},
            }
        }
    )
    checks = _checks_by_name(evaluate_checks(derive_metrics(backlog)))
    assert checks["memory_review_backlog"]["status"] == "warn"


def test_feature_checks_warn_on_failures() -> None:
    broken = _status(
        tool_result={"counters": {"attempts": 20, "failed": 15}},
        subagent={"counters": {"started": 10, "failed": 6}},
        monitor={"records": 100, "counters": {"dropped": 5}},
        files={"store": {"files": 500}, "max_files": 500, "counters": {}},
        acp={"enabled": True, "counters": {"asks": 10, "failed": 5, "timeouts": 1}},
        reaction={"counters": {"sent": 1, "failed": 3}},
    )
    checks = _checks_by_name(evaluate_checks(derive_metrics(broken)))
    for name in (
        "tool_result_condense",
        "subagent_health",
        "monitor_ring",
        "files_store",
        "acp_health",
        "reaction_health",
    ):
        assert checks[name]["status"] == "warn", name


def test_disabled_features_stay_pass() -> None:
    off = _status(
        heartflow={"enabled": False, "counters": {}},
        memory={"enabled": False, "storage": {}, "embedding": {"enabled": False}},
        acp={"enabled": False, "counters": {"asks": 0, "failed": 0, "timeouts": 0}},
        reaction={"counters": {}},
    )
    checks = evaluate_checks(derive_metrics(off))
    assert {check["status"] for check in checks} == {"pass"}


@pytest.mark.asyncio
async def test_build_report_uses_service_status() -> None:
    service = SimpleNamespace(status=_async(_status()))
    report = await build_report(service)
    assert report["overall"] == "pass"
    assert exit_code(report) == 0
    assert report["metrics"]["heartflow"]["reply_rate"] == 0.4
    assert report["generated_at"] > 0


@pytest.mark.asyncio
async def test_build_report_survives_status_failure() -> None:
    async def boom():
        raise RuntimeError("db gone")

    service = SimpleNamespace(status=boom)
    report = await build_report(service)
    assert report["overall"] == "fail"
    assert exit_code(report) == 2
    assert report["checks"][0]["status"] == "fail"


def _async(value):
    async def call():
        return value

    return call


def test_summarize_precedence() -> None:
    assert summarize([]) == "pass"
    assert summarize([{"status": "pass"}, {"status": "warn"}]) == "warn"
    assert summarize([{"status": "warn"}, {"status": "fail"}]) == "fail"


@pytest.mark.asyncio
async def test_diagnostics_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.dashboard.api import life as life_api
    import bot.life.service as life_service_module

    service = SimpleNamespace(status=_async(_status()))
    monkeypatch.setattr(
        life_service_module,
        "get_life_service",
        lambda: service,
    )

    payload = await life_api.life_diagnostics(_auth=None)

    assert payload["status"] == "ok"
    assert payload["data"]["enabled"] is True
    assert payload["data"]["overall"] == "pass"


@pytest.mark.asyncio
async def test_diagnostics_endpoint_without_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bot.dashboard.api import life as life_api
    import bot.life.service as life_service_module

    monkeypatch.setattr(life_service_module, "get_life_service", lambda: None)
    payload = await life_api.life_diagnostics(_auth=None)
    assert payload["data"]["enabled"] is False
    assert payload["data"]["overall"] == "pass"
