"""Unit tests for the embedding bridge and its circuit breaker."""

from __future__ import annotations

import pytest

from bot.life.config import MemoryEmbeddingConfig
from bot.life.memory.embedding import EmbeddingClient, EmbeddingUnavailable


class FakeProvider:
    def __init__(self, fail: bool = False, dim: int = 3) -> None:
        self.fail = fail
        self.dim = dim
        self.calls = 0

    async def get_embeddings(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider exploded")
        return [[float(index + 1)] * self.dim for index, _ in enumerate(texts)]

    def get_model(self):
        return "fake-embedding"


class FakeManager:
    def __init__(self, provider) -> None:
        self.provider = provider

    async def get_using_provider_async(self, _provider_type):
        return self.provider

    async def get_provider_by_id(self, _provider_id):
        return self.provider


class FakeContext:
    def __init__(self, provider) -> None:
        self.provider_manager = FakeManager(provider)


@pytest.mark.asyncio
async def test_embed_returns_vectors_and_tracks_model(monkeypatch):
    provider = FakeProvider()
    client = EmbeddingClient(FakeContext(provider))

    vectors = await client.embed(["hello", "world"])

    assert len(vectors) == 2
    assert len(vectors[0]) == 3
    snapshot = client.snapshot()
    assert snapshot["model"] == "fake-embedding"
    assert snapshot["dimensions"] == 3
    assert snapshot["calls"] == 1


@pytest.mark.asyncio
async def test_embed_without_provider_raises():
    client = EmbeddingClient(FakeContext(None))

    with pytest.raises(EmbeddingUnavailable):
        await client.embed(["hello"])

    assert client.available is True
    assert await client.embed_one("hello") is None


@pytest.mark.asyncio
async def test_breaker_opens_and_half_opens_after_cooldown(monkeypatch):
    provider = FakeProvider(fail=True)
    config = MemoryEmbeddingConfig(failure_threshold=2, cooldown_seconds=30)
    client = EmbeddingClient(FakeContext(provider), config)

    for _ in range(2):
        with pytest.raises(EmbeddingUnavailable):
            await client.embed(["boom"])

    assert client.available is False
    with pytest.raises(EmbeddingUnavailable):
        await client.embed(["boom"])

    # Cooldown elapsed: one half-open trial is allowed again.
    now = __import__("time").monotonic()
    monkeypatch.setattr("bot.life.memory.embedding.time.monotonic", lambda: now + 31)
    provider.fail = False

    vectors = await client.embed(["recovered"])

    assert vectors == [[1.0, 1.0, 1.0]]
    assert client.snapshot()["breaker_open"] is False


@pytest.mark.asyncio
async def test_disabled_embedding_skips_provider():
    provider = FakeProvider()
    config = MemoryEmbeddingConfig(enabled=False)
    client = EmbeddingClient(FakeContext(provider), config)

    assert await client.embed_one("hello") is None
    assert provider.calls == 0
