"""Tests for media publishing: backend first, local token service as the fallback."""

import pytest

from bot.core import media_storage
from bot.lkm.backend_client import BackendUnavailable

pytestmark = pytest.mark.asyncio


class _Config:
    """Minimal stand-in for ``bot_config`` (only ``.get`` is used)."""

    def __init__(self, values=None) -> None:
        self._values = values or {}

    def get(self, key, default=None):
        return self._values.get(key, default)


class _Tokens:
    """Records local registrations instead of touching the real token table."""

    def __init__(self) -> None:
        self.paths = []

    async def register_file(self, path, **_kwargs):
        self.paths.append(path)
        return "local-token"


class _Client:
    """Stand-in for :class:`BackendClient` with a controllable outcome."""

    def __init__(
        self,
        *,
        configured=True,
        url="https://cdn.example.com/api/v1/media/1",
        error=None,
    ):
        self.configured = configured
        self.calls = []
        self._url = url
        self._error = error

    async def upload_media(self, path, *, source="other", name=None, mime_type=None):
        self.calls.append((path, source))
        if self._error is not None:
            raise self._error
        return {"id": "1", "url": self._url}


@pytest.fixture(autouse=True)
def reset_media_counters():
    media_storage.reset_stats()
    yield
    media_storage.reset_stats()


@pytest.fixture
def patch_media(monkeypatch):
    """Wire the module's collaborators and return a factory for the stub client."""

    def _install(client, callback="https://bot.example.com", config=None):
        tokens = _Tokens()
        values = {"callback_api_base": callback}
        values.update(config or {})
        monkeypatch.setattr(media_storage, "bot_config", _Config(values))
        monkeypatch.setattr(media_storage, "file_token_service", tokens)
        monkeypatch.setattr(media_storage, "get_backend_client", lambda: client)
        return tokens

    return _install


async def test_backend_url_is_preferred(patch_media):
    client = _Client()
    tokens = patch_media(client)

    url = await media_storage.public_media_url("/tmp/a.png", source="t2i")

    assert url == "https://cdn.example.com/api/v1/media/1"
    assert client.calls == [("/tmp/a.png", "t2i")]
    assert tokens.paths == []


async def test_backend_failure_falls_back_to_local(patch_media):
    client = _Client(error=BackendUnavailable("down"))
    tokens = patch_media(client)

    url = await media_storage.public_media_url("/tmp/a.png")

    assert url == "https://bot.example.com/api/v1/files/tokens/local-token"
    assert tokens.paths == ["/tmp/a.png"]


async def test_empty_backend_url_falls_back_to_local(patch_media):
    client = _Client(url="")
    tokens = patch_media(client)

    url = await media_storage.public_media_url("/tmp/a.png")

    assert url == "https://bot.example.com/api/v1/files/tokens/local-token"
    assert tokens.paths == ["/tmp/a.png"]


async def test_local_mode_never_calls_backend(patch_media):
    client = _Client()
    tokens = patch_media(client, config={"media_storage_mode": "local"})

    url = await media_storage.public_media_url("/tmp/a.png")

    assert url == "https://bot.example.com/api/v1/files/tokens/local-token"
    assert client.calls == []
    assert tokens.paths == ["/tmp/a.png"]


async def test_no_publisher_returns_none(patch_media):
    client = _Client(configured=False)
    patch_media(client, callback="")

    assert await media_storage.public_media_url("/tmp/a.png") is None


async def test_require_media_url_raises_when_unconfigured(patch_media):
    client = _Client(configured=False)
    patch_media(client, callback="")

    with pytest.raises(Exception):
        await media_storage.require_media_url("/tmp/a.png")


async def test_counters_separate_uploads_from_fallbacks(patch_media):
    patch_media(_Client())

    await media_storage.public_media_url("/tmp/a.png")

    assert media_storage.stats() == {
        "attempts": 1,
        "uploaded": 1,
        "failed": 0,
        "local": 0,
    }


async def test_failed_backend_upload_is_counted(patch_media):
    patch_media(_Client(error=BackendUnavailable("down")))

    await media_storage.public_media_url("/tmp/a.png")

    assert media_storage.stats() == {
        "attempts": 1,
        "uploaded": 0,
        "failed": 1,
        "local": 1,
    }
