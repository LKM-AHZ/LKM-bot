"""Unit tests for the optional A_memorix bridge fallback behaviour."""

from __future__ import annotations

import importlib.util

import pytest

from bot.life.config import MemoryEmbeddingConfig
from bot.life.memory.embedding import EmbeddingClient
from bot.life.memory.upstream import AMemorixBridge, _namespace_id


class FakeContext:
    provider_manager = None


def _bridge(tmp_path) -> AMemorixBridge:
    client = EmbeddingClient(FakeContext(), MemoryEmbeddingConfig())
    return AMemorixBridge(tmp_path / "upstream", client)


def test_namespace_id_is_stable_and_bounded():
    first = _namespace_id("lkm", "aiocqhttp:GroupMessage:123")
    second = _namespace_id("lkm", "aiocqhttp:GroupMessage:123")

    assert first == second
    assert first.startswith("lkm_")
    assert len(first) <= 32


@pytest.mark.asyncio
async def test_missing_package_falls_back_cleanly(tmp_path):
    if importlib.util.find_spec("a_memorix") is not None:
        pytest.skip("a_memorix is installed; the fallback path cannot be exercised")

    bridge = _bridge(tmp_path)

    assert await bridge.start() is False
    assert bridge.available is False
    assert await bridge.search("aiocqhttp:GroupMessage:1", "query") == []
    assert await bridge.remember("aiocqhttp:GroupMessage:1", "content") is False
    assert bridge.snapshot()["installed"] is False
    await bridge.close()
