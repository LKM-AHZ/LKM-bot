from bot.core.utils.bot_path import get_bot_root
from bot.core.utils.runtime_env import is_packaged_desktop_runtime


def test_desktop_client_env_marks_desktop_runtime_without_frozen(monkeypatch):
    monkeypatch.setenv("BOT_DESKTOP_CLIENT", "1")
    monkeypatch.delattr("sys.frozen", raising=False)

    assert is_packaged_desktop_runtime() is True


def test_desktop_client_uses_home_root_without_explicit_bot_root(monkeypatch):
    monkeypatch.setenv("BOT_DESKTOP_CLIENT", "1")
    monkeypatch.delenv("BOT_ROOT", raising=False)
    monkeypatch.delattr("sys.frozen", raising=False)

    assert get_bot_root().endswith(".bot")


def test_explicit_bot_root_overrides_desktop_default(monkeypatch, tmp_path):
    explicit_root = tmp_path / "bot-root"
    monkeypatch.setenv("BOT_DESKTOP_CLIENT", "1")
    monkeypatch.setenv("BOT_ROOT", str(explicit_root))
    monkeypatch.delattr("sys.frozen", raising=False)

    assert get_bot_root() == str(explicit_root.resolve())
