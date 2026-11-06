"""Optional bridge to the upstream ``a-memorix`` memory engine.

A_memorix 2.x is an independent upstream package (AGPL-3.0, ``A-Dawn/A_memorix``)
designed for host adapters: it takes an ``EmbeddingProvider`` port and keeps its
own namespace-isolated data directory. This bridge wires that port to the LKM
embedding provider and exposes the three calls the Life layer needs.

The import is lazy and optional. When the package is missing, or the installed
alpha version changes its contract, :meth:`AMemorixBridge.start` returns False
and the adapter keeps using the built-in SQLite engine, so a broken alpha
dependency can never take the bot down.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
from pathlib import Path
from typing import Any, Sequence

from bot import logger
from bot.life.memory.embedding import EmbeddingClient


def _namespace_id(prefix: str, session_key: str) -> str:
    """Derive a stable, short namespace id for a session.

    Args:
        prefix: Configured namespace prefix.
        session_key: Session the namespace belongs to.

    Returns:
        Namespace id safe for the upstream control store.
    """
    digest = hashlib.sha1(session_key.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


class AMemorixBridge:
    """Thin in-process adapter around :class:`a_memorix.AMemorixEngine`."""

    def __init__(
        self,
        data_dir: str | Path,
        embedding_client: EmbeddingClient,
        namespace_prefix: str = "lkm",
    ) -> None:
        """Initialize the bridge without importing the upstream package.

        Args:
            data_dir: Upstream engine data root.
            embedding_client: LKM embedding bridge used for the host port.
            namespace_prefix: Prefix of per-session namespace ids.
        """
        self.data_dir = Path(data_dir)
        self.embedding_client = embedding_client
        self.namespace_prefix = namespace_prefix
        self._engine: Any = None
        self._module: Any = None
        self._error = ""
        self._known_namespaces: set[str] = set()
        self._queries = 0
        self._writes = 0

    @property
    def available(self) -> bool:
        """Return whether the engine is initialized and usable."""
        return self._engine is not None

    def _installed(self) -> bool:
        """Return whether the upstream package can be imported."""
        return importlib.util.find_spec("a_memorix") is not None

    async def start(self) -> bool:
        """Import and initialize the upstream engine.

        Returns:
            True when the engine is ready, False when it is unavailable.
        """
        if self._engine is not None:
            return True
        if not self._installed():
            self._error = "a-memorix is not installed"
            logger.info(f"[life] {self._error}; memory engine stays local")
            return False
        try:
            # Import through importlib: the dependency is optional and must
            # not be resolved statically by type checkers or import hooks.
            a_memorix = importlib.import_module("a_memorix")

            self.data_dir.mkdir(parents=True, exist_ok=True)
            engine = a_memorix.AMemorixEngine(
                data_dir=str(self.data_dir),
                host_port_factory=self._host_ports,
            )
            await engine.initialize()
            self._module = a_memorix
            self._engine = engine
            self._error = ""
            return True
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"[life] failed to start a_memorix engine: {self._error}")
            self._engine = None
            return False

    def _host_ports(self, namespace: Any) -> Any:
        """Build host ports for one namespace.

        Args:
            namespace: Namespace descriptor passed by the engine.

        Returns:
            ``NamespaceHostPorts`` with the LKM embedding provider attached.
        """
        return self._module.NamespaceHostPorts(
            embedding_provider=_EmbeddingPort(self.embedding_client),
        )

    async def _ensure_namespace(self, namespace_id: str) -> bool:
        """Create the namespace on first use.

        Args:
            namespace_id: Namespace to ensure.

        Returns:
            True when the namespace exists.
        """
        if namespace_id in self._known_namespaces:
            return True
        try:
            await self._engine.create_namespace(
                self._module.CreateNamespaceRequest(namespace_id=namespace_id)
            )
        except Exception as exc:
            # An existing namespace raises a conflict; treat it as success.
            logger.debug(f"[life] namespace {namespace_id} create: {exc}")
        self._known_namespaces.add(namespace_id)
        return True

    async def search(
        self,
        namespace: str,
        query: str,
        mode: str = "hybrid",
        limit: int = 5,
        person_id: str = "",
    ) -> list[dict[str, Any]]:
        """Search one session namespace.

        Args:
            namespace: Session key the namespace was derived from.
            query: Natural-language query.
            mode: One of the five search modes.
            limit: Maximum hits.
            person_id: Restrict to one person when supported.

        Returns:
            Normalized hit dictionaries; empty when the engine is unavailable.
        """
        if self._engine is None or not query.strip():
            return []
        namespace_id = _namespace_id(self.namespace_prefix, namespace)
        try:
            await self._ensure_namespace(namespace_id)
            request = self._module.SearchMemoryRequest(
                context=self._module.RequestContext(namespace_id=namespace_id),
                query=query,
                limit=max(1, min(int(limit), 100)),
                mode=self._module.SearchMode(mode),
                person_id=person_id,
            )
            response = await self._engine.search_memory(request)
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            logger.debug(f"[life] a_memorix search failed: {self._error}")
            return []
        self._queries += 1
        hits: list[dict[str, Any]] = []
        for hit in response.hits:
            hits.append(
                {
                    "kind": hit.kind or "note",
                    "content": hit.content or hit.title,
                    "score": float(hit.score or 0.0),
                    "ts": 0,
                    "person_id": hit.metadata.get("person_id"),
                    "meta": {"upstream": True, "memory_id": hit.memory_id},
                }
            )
        return hits

    async def remember(
        self,
        namespace: str,
        content: str,
        kind: str = "note",
        person_id: str | None = None,
    ) -> bool:
        """Write one memory into a session namespace.

        Args:
            namespace: Session key the namespace was derived from.
            content: Memory body.
            kind: Memory kind recorded as the source type.
            person_id: Optional related person.

        Returns:
            True when the engine accepted the write.
        """
        if self._engine is None or not content.strip():
            return False
        namespace_id = _namespace_id(self.namespace_prefix, namespace)
        try:
            await self._ensure_namespace(namespace_id)
            await self._engine.ingest_text(
                self._module.IngestTextRequest(
                    context=self._module.RequestContext(namespace_id=namespace_id),
                    source_type=f"lkm_{kind}",
                    text=content.strip(),
                    person_ids=(person_id,) if person_id else (),
                )
            )
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            logger.debug(f"[life] a_memorix write failed: {self._error}")
            return False
        self._writes += 1
        return True

    async def close(self) -> None:
        """Shut the upstream engine down."""
        engine, self._engine = self._engine, None
        if engine is None:
            return
        try:
            await engine.shutdown()
        except Exception as exc:
            logger.warning(f"[life] a_memorix shutdown failed: {exc}")

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable engine snapshot.

        Returns:
            Mapping with availability, data directory and counters.
        """
        return {
            "engine": "a_memorix",
            "available": self.available,
            "installed": self._installed(),
            "data_dir": str(self.data_dir),
            "namespaces": len(self._known_namespaces),
            "queries": self._queries,
            "writes": self._writes,
            "last_error": self._error,
        }


class _EmbeddingPort:
    """Adapt :class:`EmbeddingClient` to the A_memorix provider protocol."""

    def __init__(self, client: EmbeddingClient) -> None:
        """Store the LKM embedding client.

        Args:
            client: Embedding client bridging the LKM provider.
        """
        self.client = client

    async def embed(
        self,
        texts: Sequence[str],
        *,
        dimensions: int | None = None,
    ) -> list[list[float]]:
        """Embed a batch for the upstream engine.

        Args:
            texts: Texts to embed.
            dimensions: Optional requested dimensionality; the LKM provider
                decides the final size, so the value is advisory only.

        Returns:
            One vector per input text.
        """
        return await self.client.embed(texts)

    def fingerprint(self) -> dict[str, Any]:
        """Describe the embedding space to the upstream engine.

        Returns:
            Mapping with the model name and observed dimensions.
        """
        snapshot = self.client.snapshot()
        return {
            "provider": "lkm",
            "model": snapshot.get("model") or "unknown",
            "dimensions": snapshot.get("dimensions") or 0,
        }
