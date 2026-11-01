"""Tests for panel-account delegation to LKM-service AUTH."""

import pytest

from bot.lkm import account_auth


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Client:
    """Fake ``httpx.AsyncClient`` returning a canned response (and recording the call)."""

    def __init__(self, response, sink):
        self._response = response
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, path, json=None):
        self._sink.append((path, json))
        return self._response


def _install(monkeypatch, response, *, base="http://auth:8001", token="t0ken"):
    monkeypatch.setenv(account_auth.AUTH_BASE_URL_ENV, base)
    monkeypatch.setenv(account_auth.AUTH_TOKEN_ENV, token)
    calls = []
    monkeypatch.setattr(
        account_auth.httpx, "AsyncClient", lambda **kwargs: _Client(response, calls)
    )
    return calls


def test_mode_defaults_to_local():
    assert account_auth.auth_mode({"dashboard": {}}) == "local"
    assert account_auth.auth_mode({}) == "local"


@pytest.mark.parametrize("value", ["both", "backend", "LOCAL", " backend "])
def test_mode_parsing(value):
    assert (
        account_auth.auth_mode({"dashboard": {"auth_mode": value}})
        == value.strip().lower()
    )


def test_unknown_mode_never_widens():
    """A typo must not turn into backend delegation."""
    assert account_auth.auth_mode({"dashboard": {"auth_mode": "backnd"}}) == "local"


@pytest.mark.asyncio
async def test_success_returns_minimal_identity(monkeypatch):
    payload = {"user_id": "u-1", "username": "alma", "account_level": "admin"}
    calls = _install(monkeypatch, _Response(200, payload))

    identity = await account_auth.verify_community_account(
        "alma", "pw", ip_address="1.2.3.4"
    )

    assert identity == payload
    path, body = calls[0]
    assert path == "/api/v1/auth/internal/verify-password"
    assert body == {"account": "alma", "password": "pw", "ip_address": "1.2.3.4"}


@pytest.mark.parametrize("status", [401, 403, 423])
@pytest.mark.asyncio
async def test_rejections_return_none(monkeypatch, status):
    _install(monkeypatch, _Response(status, {"detail": "nope"}))

    assert await account_auth.verify_community_account("alma", "bad") is None


@pytest.mark.parametrize("status", [500, 502, 503])
@pytest.mark.asyncio
async def test_server_errors_are_unavailable(monkeypatch, status):
    """A broken AUTH must be distinguishable from a wrong password."""
    _install(monkeypatch, _Response(status, {"detail": "boom"}, text="boom"))

    with pytest.raises(account_auth.AccountAuthUnavailable):
        await account_auth.verify_community_account("alma", "pw")


@pytest.mark.asyncio
async def test_malformed_body_is_unavailable(monkeypatch):
    _install(monkeypatch, _Response(200, {"username": "alma"}))

    with pytest.raises(account_auth.AccountAuthUnavailable):
        await account_auth.verify_community_account("alma", "pw")


@pytest.mark.asyncio
async def test_unconfigured_raises(monkeypatch):
    monkeypatch.delenv(account_auth.AUTH_BASE_URL_ENV, raising=False)
    monkeypatch.delenv(account_auth.AUTH_TOKEN_ENV, raising=False)

    with pytest.raises(account_auth.AccountAuthUnavailable):
        await account_auth.verify_community_account("alma", "pw")
