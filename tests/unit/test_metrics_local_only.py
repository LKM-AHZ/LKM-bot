"""The bot must never report usage data to a third party.

Upstream batched anonymous counters and POSTed them to a hosted endpoint (hostname, Python
version, OS, installation id). This fork keeps only the local half, so the test pins two things:
the module performs no network I/O, and ``Metric.upload`` still records the local platform row.
"""

import inspect

import pytest

from bot.core.utils import metrics


def test_metrics_module_has_no_network_client():
    """A future edit must not quietly reintroduce telemetry."""
    source = inspect.getsource(metrics)
    for forbidden in ("aiohttp", "httpx", "requests", "urllib", "http://", "https://"):
        assert forbidden not in source, f"metrics.py gained {forbidden!r} again"


@pytest.mark.asyncio
async def test_upload_is_local_only(monkeypatch):
    recorded = []

    async def _record(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(
        metrics.Metric, "_save_platform_stats", lambda kw: _record(**kw)
    )

    await metrics.Metric.upload(
        adapter_name="telegram", adapter_type="telegram", msg_event_tick=1
    )

    assert recorded == [
        {"adapter_name": "telegram", "adapter_type": "telegram", "msg_event_tick": 1}
    ]


@pytest.mark.asyncio
async def test_upload_survives_a_broken_database(monkeypatch):
    """A stats write failure must never propagate into the message pipeline."""

    class _Broken:
        async def insert_platform_stats(self, **_kwargs):
            raise RuntimeError("db down")

    monkeypatch.setattr(metrics, "db_helper", _Broken())

    await metrics.Metric.upload(adapter_name="telegram")
