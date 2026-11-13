"""Tests for the backend service client (URL absolutization and upload guards)."""

import httpx
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


def _client(handler, **kwargs) -> backend_client.BackendClient:
    return backend_client.BackendClient(
        "http://b:8000",
        "t",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_transport_error_is_retried_once():
    calls = []

    def handler(request):
        calls.append(request.url)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"status": "ok", "data": {"objects": 1}})

    client = _client(handler)
    try:
        status = await client.media_status()
    finally:
        await client.aclose()

    assert status == {"objects": 1}
    assert len(calls) == 2
    assert client.stats()["retries"] == 1


@pytest.mark.asyncio
async def test_circuit_opens_and_fails_fast_after_repeated_failures():
    now = [0.0]

    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client = _client(
        handler,
        breaker_threshold=2,
        breaker_cooldown=10.0,
        clock=lambda: now[0],
    )
    try:
        with pytest.raises(backend_client.BackendUnavailable):
            await client.media_status()
        assert client.stats()["breaker_open"] is True

        stats = client.stats()
        with pytest.raises(backend_client.BackendUnavailable) as caught:
            await client.media_status()
        assert caught.value.retryable is True
        assert client.stats()["short_circuited"] == stats["short_circuited"] + 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_circuit_probes_again_after_cooldown():
    now = [0.0]
    healthy = [False]

    def handler(request):
        if not healthy[0]:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"status": "ok", "data": {"objects": 2}})

    client = _client(
        handler,
        breaker_threshold=1,
        breaker_cooldown=10.0,
        clock=lambda: now[0],
    )
    try:
        with pytest.raises(backend_client.BackendUnavailable):
            await client.media_status()
        assert client.stats()["breaker_open"] is True

        healthy[0] = True
        now[0] = 11.0
        assert await client.media_status() == {"objects": 2}
        assert client.stats()["breaker_open"] is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_client_error_does_not_trip_the_circuit():
    def handler(request):
        return httpx.Response(404, text="missing")

    client = _client(handler, breaker_threshold=1)
    try:
        for _ in range(2):
            with pytest.raises(backend_client.BackendUnavailable):
                await client.media_status()
    finally:
        await client.aclose()

    assert client.stats()["breaker_open"] is False
    assert client.stats()["requests"] == 2


@pytest.mark.asyncio
async def test_library_content_returns_raw_bytes():
    def handler(request):
        assert request.url.path.endswith("/library/f1/content")
        return httpx.Response(200, content=b"\x89PNG")

    client = _client(handler)
    try:
        assert await client.get_library_file_content("f1") == b"\x89PNG"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_upload_posts_file_bytes_from_a_worker_thread(tmp_path):
    body = b"\x89PNG-bytes"

    def handler(request):
        assert body in request.content
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {"id": "1", "url": "/api/v1/media/1"},
            },
        )

    path = tmp_path / "a.png"
    path.write_bytes(body)
    client = _client(handler)
    try:
        view = await client.upload_media(path, source="t2i")
    finally:
        await client.aclose()

    assert view["url"] == "http://b:8000/api/v1/media/1"
    assert client.stats()["uploads"] == 1
