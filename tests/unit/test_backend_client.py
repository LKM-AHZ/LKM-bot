"""Tests for the backend service client (URL absolutization and upload guards)."""

import pytest

from bot.lkm import backend_client


def test_absolute_url_uses_public_origin():
    client = backend_client.BackendClient(
        "http://backend:8000", "t", public_origin="https://lkm.example.com/"
    )

    assert (
        client.absolute_url("/api/v1/media/1?exp=1&sig=x")
        == "https://lkm.example.com/api/v1/media/1?exp=1&sig=x"
    )


def test_absolute_url_falls_back_to_base_url():
    """Without a public origin the base URL is used (single-host development only)."""
    client = backend_client.BackendClient("http://backend:8000", "t")

    assert (
        client.absolute_url("/api/v1/media/1") == "http://backend:8000/api/v1/media/1"
    )


def test_absolute_url_keeps_already_absolute_urls():
    client = backend_client.BackendClient("http://backend:8000", "t")

    assert (
        client.absolute_url("https://cdn.example.com/a.png")
        == "https://cdn.example.com/a.png"
    )
    assert client.absolute_url("") == ""


def test_configured_requires_both_parts():
    assert backend_client.BackendClient("http://b:8000", "t").configured
    assert not backend_client.BackendClient("", "t").configured
    assert not backend_client.BackendClient("http://b:8000", "").configured


@pytest.mark.asyncio
async def test_upload_missing_file_is_unavailable(tmp_path):
    client = backend_client.BackendClient("http://b:8000", "t")

    with pytest.raises(backend_client.BackendUnavailable):
        await client.upload_media(tmp_path / "nope.png")


@pytest.mark.asyncio
async def test_upload_unconfigured_is_unavailable(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(b"x")
    client = backend_client.BackendClient("", "")

    with pytest.raises(backend_client.BackendUnavailable):
        await client.upload_media(path)


def test_client_is_a_singleton_until_reset(monkeypatch):
    monkeypatch.setenv(backend_client.BASE_URL_ENV, "http://b:8000")
    monkeypatch.setenv(backend_client.TOKEN_ENV, "t")
    backend_client.reset_backend_client()

    first = backend_client.get_backend_client()
    assert first is backend_client.get_backend_client()
    assert first.configured

    backend_client.reset_backend_client()
    assert backend_client.get_backend_client() is not first
    backend_client.reset_backend_client()
