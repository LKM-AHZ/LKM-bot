"""Exercise packaged panel discovery and the real static routes without Astro."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bot.core.dashboard_assets import resolve_dashboard_dist
from bot.dashboard.api.static_files import router


@pytest.mark.asyncio
async def test_bundled_panel_serves_entry_and_assets_with_explicit_override(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "bot.core.dashboard_assets.__file__",
        str(tmp_path / "bot" / "core" / "dashboard_assets.py"),
    )
    monkeypatch.setattr(
        "bot.core.dashboard_assets.get_bot_data_path", lambda: str(tmp_path / "data")
    )
    bundled = tmp_path / "bot" / "dashboard" / "dist"
    (bundled / "assets").mkdir(parents=True)
    (bundled / "index.html").write_text('<script src="/bot/assets/app.js"></script>')
    (bundled / "assets" / "app.js").write_text("console.log('bundled');")
    assert resolve_dashboard_dist() == bundled
    app = FastAPI()
    app.state.dashboard_static_folder = str(resolve_dashboard_dist())
    app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        entry = await client.get("/")
        assert entry.status_code == 200
        assert "/bot/assets/app.js" in entry.text
        assert (await client.get("/assets/app.js")).status_code == 200
        assert (await client.get("/api/absent")).status_code == 404
    override = tmp_path / "data" / "dist"
    override.mkdir(parents=True)
    (override / "index.html").write_text("Override")
    assert resolve_dashboard_dist() == override
    assert resolve_dashboard_dist(bundled) == bundled
