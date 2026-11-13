import os
import sys

# 将项目根目录添加到 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from unittest import mock

import pytest

from bot.core.dashboard_assets import get_dashboard_version
from main import (
    DASHBOARD_RESET_PASSWORD_ENV,
    _apply_startup_env_flags,
    check_dashboard_files,
    check_env,
)


class _version_info:
    def __init__(self, major, minor):
        self.major = major
        self.minor = minor

    def __eq__(self, other):
        if isinstance(other, tuple):
            return (self.major, self.minor) == other[:2]
        return (self.major, self.minor) == (other.major, other.minor)

    def __ge__(self, other):
        if isinstance(other, tuple):
            return (self.major, self.minor) >= other[:2]
        return (self.major, self.minor) >= (other.major, other.minor)

    def __le__(self, other):
        if isinstance(other, tuple):
            return (self.major, self.minor) <= other[:2]
        return (self.major, self.minor) <= (other.major, other.minor)

    def __gt__(self, other):
        if isinstance(other, tuple):
            return (self.major, self.minor) > other[:2]
        return (self.major, self.minor) > (other.major, other.minor)

    def __lt__(self, other):
        if isinstance(other, tuple):
            return (self.major, self.minor) < other[:2]
        return (self.major, self.minor) < (other.major, other.minor)


def _write_dashboard_dist(dist_path, version: str | None = None) -> None:
    assets = dist_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (dist_path / "index.html").write_text(
        '<script type="module" src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (assets / "app.js").write_text("export {};", encoding="utf-8")
    if version is not None:
        (assets / "version").write_text(version, encoding="utf-8")


def test_check_env(monkeypatch):
    version_info_correct = _version_info(3, 10)
    version_info_wrong = _version_info(3, 9)
    monkeypatch.setattr(sys, "version_info", version_info_correct)
    with mock.patch("os.makedirs") as mock_makedirs:
        check_env()
        # check_env uses get_bot_*_path() which returns absolute paths,
        # so just verify makedirs was called the expected number of times
        assert mock_makedirs.call_count >= 4
        # Verify all calls used exist_ok=True
        for call_args in mock_makedirs.call_args_list:
            assert call_args[1].get("exist_ok") is True

    monkeypatch.setattr(sys, "version_info", version_info_wrong)
    with pytest.raises(SystemExit):
        check_env()


def test_apply_startup_env_flags_sets_reset_password_env(monkeypatch):
    monkeypatch.delenv(DASHBOARD_RESET_PASSWORD_ENV, raising=False)

    _apply_startup_env_flags(["--webui-dir", "/tmp/webui", "--reset-password"])

    assert os.environ[DASHBOARD_RESET_PASSWORD_ENV] == "1"


def test_apply_startup_env_flags_ignores_unrelated_args(monkeypatch):
    monkeypatch.delenv(DASHBOARD_RESET_PASSWORD_ENV, raising=False)

    _apply_startup_env_flags(["--webui-dir", "/tmp/webui"])

    assert DASHBOARD_RESET_PASSWORD_ENV not in os.environ


def test_apply_startup_env_flags_does_not_reset_for_help(monkeypatch):
    monkeypatch.delenv(DASHBOARD_RESET_PASSWORD_ENV, raising=False)

    _apply_startup_env_flags(["--reset-password", "--help"])

    assert DASHBOARD_RESET_PASSWORD_ENV not in os.environ


def test_check_env_appends_user_site_packages_after_runtime_paths(monkeypatch):
    bot_root = "/tmp/bot-root"
    site_packages_path = "/tmp/bot-site-packages"
    original_sys_path = list(sys.path)

    monkeypatch.setattr(sys, "version_info", _version_info(3, 12))
    monkeypatch.setattr("main.get_bot_root", lambda: bot_root)
    monkeypatch.setattr("main.get_bot_site_packages_path", lambda: site_packages_path)
    monkeypatch.setattr("main.get_bot_config_path", lambda: "/tmp/config")
    monkeypatch.setattr("main.get_bot_plugin_path", lambda: "/tmp/plugins")
    monkeypatch.setattr("main.get_bot_temp_path", lambda: "/tmp/temp")
    monkeypatch.setattr("main.get_bot_knowledge_base_path", lambda: "/tmp/kb")
    monkeypatch.setattr(sys, "path", ["/runtime/lib", *original_sys_path])

    with mock.patch("os.makedirs"):
        check_env()

    assert sys.path[0] == bot_root
    assert sys.path[-1] == site_packages_path
    assert sys.path.index(site_packages_path) > sys.path.index("/runtime/lib")


def test_check_env_does_not_append_duplicate_user_site_packages(monkeypatch):
    bot_root = "/tmp/bot-root"
    site_packages_path = "/tmp/bot-site-packages"
    original_sys_path = list(sys.path)

    monkeypatch.setattr(sys, "version_info", _version_info(3, 12))
    monkeypatch.setattr("main.get_bot_root", lambda: bot_root)
    monkeypatch.setattr("main.get_bot_site_packages_path", lambda: site_packages_path)
    monkeypatch.setattr("main.get_bot_config_path", lambda: "/tmp/config")
    monkeypatch.setattr("main.get_bot_plugin_path", lambda: "/tmp/plugins")
    monkeypatch.setattr("main.get_bot_temp_path", lambda: "/tmp/temp")
    monkeypatch.setattr("main.get_bot_knowledge_base_path", lambda: "/tmp/kb")
    monkeypatch.setattr(sys, "path", [bot_root, *original_sys_path, site_packages_path])

    with mock.patch("os.makedirs"):
        check_env()

    assert sys.path.count(site_packages_path) == 1


def test_version_info_comparisons():
    """Test _version_info comparison operators with tuples and other instances."""
    v3_10 = _version_info(3, 10)
    v3_9 = _version_info(3, 9)
    v3_11 = _version_info(3, 11)

    # Test __eq__ with tuples
    assert v3_10 == (3, 10)
    assert v3_10 != (3, 9)
    assert v3_9 == (3, 9)

    # Test __ge__ with tuples
    assert v3_10 >= (3, 10)
    assert v3_10 >= (3, 9)
    assert not (v3_9 >= (3, 10))
    assert v3_11 >= (3, 10)

    # Test __eq__ with other _version_info instances
    assert v3_10 == _version_info(3, 10)
    assert v3_10 != v3_9
    assert v3_10 == v3_10  # Same instance

    assert v3_10 != v3_11

    # Test __ge__ with other _version_info instances
    assert v3_10 >= v3_10
    assert v3_10 >= v3_9
    assert not (v3_9 >= v3_10)
    assert v3_11 >= v3_10

    assert v3_11 >= v3_11  # Same instance


@pytest.mark.asyncio
async def test_check_dashboard_files_uses_explicit_directory(tmp_path):
    """An explicit --webui-dir is served as-is, without version negotiation."""
    dashboard_path = tmp_path / "dist"
    _write_dashboard_dist(dashboard_path)

    assert await check_dashboard_files(str(dashboard_path)) == str(
        dashboard_path.absolute()
    )


@pytest.mark.asyncio
async def test_check_dashboard_files_returns_none_without_static_files(
    monkeypatch, tmp_path
):
    """Startup has no static panel when neither --webui-dir nor data/dist exists."""
    monkeypatch.setattr(
        "bot.core.dashboard_assets.get_bot_data_path",
        lambda: str(tmp_path / "data"),
    )
    monkeypatch.setattr(
        "bot.core.dashboard_assets.__file__",
        str(tmp_path / "bot" / "core" / "dashboard_assets.py"),
    )

    assert await check_dashboard_files() is None


@pytest.mark.asyncio
async def test_check_dashboard_files_serves_data_dist_fallback(monkeypatch, tmp_path):
    """A prebuilt data/dist copy is still served by the bot process."""
    data_dir = tmp_path / "data"
    _write_dashboard_dist(data_dir / "dist")
    monkeypatch.setattr(
        "bot.core.dashboard_assets.get_bot_data_path",
        lambda: str(data_dir),
    )

    assert await check_dashboard_files() == str((data_dir / "dist").absolute())


@pytest.mark.asyncio
async def test_get_dashboard_version_returns_none_without_static_files(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "bot.core.dashboard_assets.get_bot_data_path",
        lambda: str(tmp_path / "data"),
    )

    assert await get_dashboard_version() is None


@pytest.mark.asyncio
async def test_get_dashboard_version_reads_data_dist(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    _write_dashboard_dist(data_dir / "dist", "v4.24.4")
    monkeypatch.setattr(
        "bot.core.dashboard_assets.get_bot_data_path",
        lambda: str(data_dir),
    )

    assert await get_dashboard_version() == "v4.24.4"
