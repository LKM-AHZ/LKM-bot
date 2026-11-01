"""Tests for panel login delegation to community accounts (`LKM_BOT_AUTH_MODE`)."""

import pytest

from bot.dashboard.services import auth_service as auth_mod
from bot.lkm.account_auth import AccountAuthUnavailable

JWT_SECRET = "unit-test-jwt-secret-0123456789abcdef"
LOCAL_PASSWORD = "Local-Pass-1"


@pytest.fixture(autouse=True)
def _patch_local_password(monkeypatch):
    """Make the local password check deterministic and skip DB-backed helpers."""

    async def _upgraded(_db, _config):
        return True

    async def _change_required(_db, _config):
        return False

    monkeypatch.setattr(auth_mod, "is_password_storage_upgraded", _upgraded)
    monkeypatch.setattr(auth_mod, "is_password_change_required", _change_required)
    monkeypatch.setattr(auth_mod, "get_dashboard_password_hash", lambda *a, **k: "hash")
    monkeypatch.setattr(auth_mod, "is_totp_enabled", lambda _config: False)
    monkeypatch.setattr(
        auth_mod,
        "verify_dashboard_password",
        lambda _stored, candidate: candidate == LOCAL_PASSWORD,
    )


def _service():
    config = {
        "dashboard": {"username": "panel-admin", "jwt_secret": JWT_SECRET},
    }
    return auth_mod.AuthService(object(), config)


async def _login(service, username, password):
    return await service.login(
        {"username": username, "password": password},
        trusted_device_cookie_token="",
    )


@pytest.mark.asyncio
async def test_local_mode_ignores_community_accounts(monkeypatch):
    called = []

    async def _community(*args, **kwargs):
        called.append(args)
        return {"user_id": "u", "username": "alma", "account_level": "admin"}

    monkeypatch.setattr(auth_mod, "auth_mode", lambda *_a, **_k: "local")
    monkeypatch.setattr(auth_mod, "verify_community_account", _community)

    result = await _login(_service(), "panel-admin", LOCAL_PASSWORD)

    assert result.status_code == 200
    assert called == []
    assert result.data["auth_source"] == "password"


@pytest.mark.asyncio
async def test_backend_mode_accepts_community_admin(monkeypatch):
    async def _community(account, password, *, ip_address=""):
        assert (account, password) == ("alma", "community-pw")
        return {"user_id": "u", "username": "alma", "account_level": "admin"}

    monkeypatch.setattr(auth_mod, "auth_mode", lambda *_a, **_k: "backend")
    monkeypatch.setattr(auth_mod, "verify_community_account", _community)

    result = await _login(_service(), "alma", "community-pw")

    assert result.status_code == 200
    assert result.data["auth_source"] == "community"
    assert result.data["username"] == "panel-admin"


@pytest.mark.asyncio
async def test_backend_mode_rejects_non_admin(monkeypatch):
    async def _community(*_args, **_kwargs):
        return {"user_id": "u", "username": "alma", "account_level": "local"}

    monkeypatch.setattr(auth_mod, "auth_mode", lambda *_a, **_k: "backend")
    monkeypatch.setattr(auth_mod, "verify_community_account", _community)

    result = await _login(_service(), "alma", "community-pw")

    assert result.status_code == 403


@pytest.mark.asyncio
async def test_backend_mode_never_falls_back_to_local_password(monkeypatch):
    """An AUTH outage must fail closed even when the local password matches."""

    async def _community(*_args, **_kwargs):
        raise AccountAuthUnavailable("down")

    monkeypatch.setattr(auth_mod, "auth_mode", lambda *_a, **_k: "backend")
    monkeypatch.setattr(auth_mod, "verify_community_account", _community)

    result = await _login(_service(), "panel-admin", LOCAL_PASSWORD)

    assert result.status_code == 503


@pytest.mark.asyncio
async def test_both_mode_falls_back_to_local_password(monkeypatch):
    async def _community(*_args, **_kwargs):
        return None

    monkeypatch.setattr(auth_mod, "auth_mode", lambda *_a, **_k: "both")
    monkeypatch.setattr(auth_mod, "verify_community_account", _community)

    result = await _login(_service(), "panel-admin", LOCAL_PASSWORD)

    assert result.status_code == 200
    assert result.data["auth_source"] == "password"


@pytest.mark.asyncio
async def test_both_mode_still_accepts_community_admin(monkeypatch):
    async def _community(*_args, **_kwargs):
        return {"user_id": "u", "username": "alma", "account_level": "admin"}

    monkeypatch.setattr(auth_mod, "auth_mode", lambda *_a, **_k: "both")
    monkeypatch.setattr(auth_mod, "verify_community_account", _community)

    result = await _login(_service(), "alma", "community-pw")

    assert result.status_code == 200
    assert result.data["auth_source"] == "community"


@pytest.mark.asyncio
async def test_jwt_records_community_source(monkeypatch):
    """The session must carry where the credentials came from (audit trail in the token)."""

    async def _community(*_args, **_kwargs):
        return {"user_id": "u", "username": "alma", "account_level": "admin"}

    monkeypatch.setattr(auth_mod, "auth_mode", lambda *_a, **_k: "backend")
    monkeypatch.setattr(auth_mod, "verify_community_account", _community)

    result = await _login(_service(), "alma", "community-pw")

    import jwt

    payload = jwt.decode(result.jwt_token, JWT_SECRET, algorithms=["HS256"])
    assert payload["auth_source"] == "community"
