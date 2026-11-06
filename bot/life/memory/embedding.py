"""Embedding bridge between the Life memory layer and LKM providers.

Vectors always come from the provider layer (``ProviderType.EMBEDDING``), so the
memory layer never hard-codes a vendor. The client adds the production concerns
the plan requires: batching, timeout, concurrency cap, circuit breaker and a
health snapshot the dashboard can show.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Sequence

from bot import logger
from bot.core.provider.entities import ProviderType
from bot.life.config import MemoryEmbeddingConfig


class EmbeddingUnavailable(RuntimeError):
    """Raised when embeddings cannot be produced right now."""


class EmbeddingClient:
    """Embedding client bound to the active LKM embedding provider."""

    def __init__(
        self,
        context: Any,
        config: MemoryEmbeddingConfig | None = None,
    ) -> None:
        """Initialize the client without resolving a provider.

        Args:
            context: Star context exposing ``provider_manager``.
            config: Embedding settings; defaults are used when omitted.
        """
        self.context = context
        self.config = config or MemoryEmbeddingConfig()
        self._semaphore = asyncio.Semaphore(max(self.config.max_concurrency, 1))
        self._failures = 0
        self._open_until = 0.0
        self._last_error = ""
        self._model = ""
        self._dim = 0
        self._calls = 0
        self._errors = 0

    @property
    def enabled(self) -> bool:
        """Return whether vectorization is configured on."""
        return self.config.enabled

    @property
    def available(self) -> bool:
        """Return whether a request may be attempted right now."""
        if not self.config.enabled:
            return False
        return not self._breaker_open()

    def _breaker_open(self) -> bool:
        """Return True while the circuit breaker rejects calls."""
        if self._open_until <= 0:
            return False
        if time.monotonic() >= self._open_until:
            # Cooldown expired: allow a single half-open trial.
            self._open_until = 0.0
            self._failures = max(self.config.failure_threshold - 1, 0)
            return False
        return True

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable health snapshot.

        Returns:
            Mapping with model, dimension, breaker state and counters.
        """
        return {
            "enabled": self.config.enabled,
            "available": self.available,
            "model": self._model,
            "dimensions": self._dim,
            "failures": self._failures,
            "breaker_open": self._breaker_open(),
            "last_error": self._last_error,
            "calls": self._calls,
            "errors": self._errors,
        }

    async def _resolve_provider(self) -> Any:
        """Return the configured embedding provider instance.

        Returns:
            Provider instance exposing ``get_embeddings``.

        Raises:
            EmbeddingUnavailable: When no provider is configured or resolvable.
        """
        manager = getattr(self.context, "provider_manager", None)
        if manager is None:
            raise EmbeddingUnavailable("provider manager is unavailable")
        provider = None
        if self.config.provider_id:
            provider = await manager.get_provider_by_id(self.config.provider_id)
        if provider is None:
            provider = await manager.get_using_provider_async(ProviderType.EMBEDDING)
        if provider is None:
            raise EmbeddingUnavailable("no embedding provider is configured")
        return provider

    def _model_name(self, provider: Any) -> str:
        """Derive a stable model identifier for stored vectors.

        Args:
            provider: Embedding provider instance.

        Returns:
            Model name, falling back to the provider class name.
        """
        getter = getattr(provider, "get_model", None)
        if callable(getter):
            try:
                model = getter()
                if model:
                    return str(model)
            except Exception:
                pass
        config = getattr(provider, "provider_config", None) or {}
        for key in ("embedding_model", "model"):
            if config.get(key):
                return str(config[key])
        return type(provider).__name__

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Args:
            texts: Non-empty texts; empty strings are skipped by the caller.

        Returns:
            One vector per input text.

        Raises:
            EmbeddingUnavailable: When the provider is missing, the breaker is
                open, or the call fails or times out.
        """
        cleaned = [text.strip() for text in texts]
        if not cleaned or not any(cleaned):
            return []
        if self._breaker_open():
            raise EmbeddingUnavailable("embedding circuit breaker is open")

        provider = await self._resolve_provider()
        self._model = self._model or self._model_name(provider)
        batch_size = max(self.config.batch_size, 1)
        vectors: list[list[float]] = []
        async with self._semaphore:
            for start in range(0, len(cleaned), batch_size):
                chunk = cleaned[start : start + batch_size]
                try:
                    async with asyncio.timeout(self.config.timeout_seconds):
                        batch = await provider.get_embeddings(chunk)
                except (TimeoutError, asyncio.CancelledError) as exc:
                    self._record_failure(
                        f"timeout after {self.config.timeout_seconds}s"
                    )
                    raise EmbeddingUnavailable("embedding request timed out") from exc
                except Exception as exc:
                    self._record_failure(f"{type(exc).__name__}: {exc}")
                    raise EmbeddingUnavailable(str(exc)) from exc
                if not batch or len(batch) != len(chunk):
                    self._record_failure("provider returned an unexpected batch size")
                    raise EmbeddingUnavailable("embedding batch size mismatch")
                vectors.extend([list(map(float, vector)) for vector in batch])
                self._calls += 1
        self._failures = 0
        self._last_error = ""
        if vectors and not self._dim:
            self._dim = len(vectors[0])
        return vectors

    async def embed_one(self, text: str) -> list[float] | None:
        """Embed a single text, returning None instead of raising.

        Args:
            text: Text to embed.

        Returns:
            Vector, or None when embedding is unavailable.
        """
        if not text.strip() or not self.config.enabled:
            return None
        try:
            vectors = await self.embed([text])
        except EmbeddingUnavailable as exc:
            logger.debug(f"[life] memory embedding skipped: {exc}")
            return None
        return vectors[0] if vectors else None

    def _record_failure(self, error: str) -> None:
        """Update breaker state after a failed call.

        Args:
            error: Human-readable failure reason.
        """
        self._errors += 1
        self._failures += 1
        self._last_error = error
        if self._failures >= max(self.config.failure_threshold, 1):
            self._open_until = time.monotonic() + max(self.config.cooldown_seconds, 1)
            logger.warning(
                f"[life] embedding breaker opened for "
                f"{self.config.cooldown_seconds}s: {error}"
            )
