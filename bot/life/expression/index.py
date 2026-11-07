"""FAISS vector index for expression retrieval.

The index is derived state: expression rows live in SQLite, and this module keeps
a FAISS index of them for similarity search, reusing the same
``bot.core.db.vec_db.faiss_impl`` implementation the knowledge base uses. When
the embedding provider is unavailable the index reports itself disabled and the
selector falls back to keyword scoring, so expression learning keeps working
offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from bot import logger
from bot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
from bot.life.memory.embedding import EmbeddingClient

_INDEX_TEXT_SEPARATOR = " "


class _FaissEmbeddingProvider:
    """Adapt :class:`EmbeddingClient` to the FAISS storage provider protocol."""

    def __init__(self, client: EmbeddingClient, dimensions: int) -> None:
        """Store the client and the probed vector size.

        Args:
            client: LKM embedding bridge.
            dimensions: Vector size observed during probing.
        """
        self.client = client
        self.dimensions = dimensions

    def get_dim(self) -> int:
        """Return the embedding size required by the index.

        Returns:
            Vector dimensionality.
        """
        return self.dimensions

    async def get_embedding(self, text: str) -> list[float]:
        """Embed one text for the FAISS index.

        Args:
            text: Text to embed.

        Returns:
            Vector of the probed size; an empty list when embedding failed.
        """
        vector = await self.client.embed_one(text)
        return vector or []

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch for the FAISS index.

        Args:
            texts: Texts to embed.

        Returns:
            One vector per text.
        """
        return await self.client.embed(texts)


class ExpressionIndex:
    """FAISS index over learned expressions."""

    def __init__(self, data_dir: Path, embedding_client: EmbeddingClient) -> None:
        """Initialize the wrapper without touching the filesystem.

        Args:
            data_dir: Directory holding the index and document store.
            embedding_client: Embedding bridge used for vectors.
        """
        self.data_dir = Path(data_dir)
        self.embedding_client = embedding_client
        self._db: FaissVecDB | None = None
        self._error = ""
        self._synced = 0

    @property
    def available(self) -> bool:
        """Return whether the FAISS index is initialized."""
        return self._db is not None

    async def initialize(self) -> bool:
        """Probe the embedding provider and open the FAISS index.

        Returns:
            True when the index is usable.
        """
        if self._db is not None:
            return True
        probe = await self.embedding_client.embed_one("表达方式")
        if not probe:
            self._error = "embedding provider unavailable"
            logger.info(f"[life] expression index disabled: {self._error}")
            return False
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._db = FaissVecDB(
                doc_store_path=str(self.data_dir / "expressions.db"),
                index_store_path=str(self.data_dir / "expressions.index"),
                embedding_provider=_FaissEmbeddingProvider(  # ty: ignore[invalid-argument-type]  # duck-typed provider with the same three methods
                    self.embedding_client,
                    len(probe),
                ),
            )
            await self._db.initialize()
            return True
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"[life] expression index init failed: {self._error}")
            self._db = None
            return False

    async def sync(self, rows: Sequence[dict[str, Any]], store: Any) -> int:
        """Insert expressions that are not indexed yet.

        Args:
            rows: Expression rows awaiting indexing.
            store: Expression store used to mark vector state.

        Returns:
            Number of rows inserted into the index.
        """
        if self._db is None or not rows:
            return 0
        inserted = 0
        indexed_ids: list[int] = []
        for row in rows:
            text = _INDEX_TEXT_SEPARATOR.join(
                part for part in (row.get("situation"), row.get("style")) if part
            )
            try:
                await self._db.insert(
                    content=text,
                    metadata={
                        "session_key": row["session_key"],
                        "expression_id": int(row["id"]),
                    },
                    id=str(row["id"]),
                )
            except Exception as exc:
                logger.warning(f"[life] expression index insert failed: {exc}")
                break
            indexed_ids.append(int(row["id"]))
            inserted += 1
        if indexed_ids:
            await store.mark_vector_state(indexed_ids, "indexed")
            self._synced += inserted
        return inserted

    async def search(
        self,
        query: str,
        session_key: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search expressions of one session by similarity.

        Args:
            query: Context text used as the query.
            session_key: Session scope filter.
            limit: Maximum hits.

        Returns:
            Hits with ``expression_id`` and ``similarity``; empty on failure.
        """
        if self._db is None or not query.strip():
            return []
        try:
            results = await self._db.retrieve(
                query,
                k=max(limit, 1),
                metadata_filters={"session_key": session_key},
            )
        except Exception as exc:
            logger.debug(f"[life] expression index search failed: {exc}")
            return []
        hits: list[dict[str, Any]] = []
        for result in results:
            metadata = (result.data or {}).get("metadata") or {}
            expression_id = metadata.get("expression_id")
            if expression_id is None:
                continue
            hits.append(
                {
                    "expression_id": int(expression_id),
                    "similarity": float(result.similarity),
                }
            )
        return hits

    async def delete(self, expression_ids: Sequence[int]) -> None:
        """Remove expressions from the index.

        Args:
            expression_ids: Expression rows to drop.
        """
        if self._db is None:
            return
        for expression_id in expression_ids:
            try:
                await self._db.delete(str(int(expression_id)))
            except Exception as exc:
                logger.debug(f"[life] expression index delete failed: {exc}")

    async def close(self) -> None:
        """Close the underlying index."""
        if self._db is None:
            return
        try:
            await self._db.close()
        except Exception as exc:
            logger.warning(f"[life] expression index close failed: {exc}")
        self._db = None

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable index snapshot.

        Returns:
            Mapping with availability, synced count and last error.
        """
        return {
            "available": self.available,
            "data_dir": str(self.data_dir),
            "synced": self._synced,
            "last_error": self._error,
        }
