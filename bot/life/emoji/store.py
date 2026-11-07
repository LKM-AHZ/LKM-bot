"""Storage for the sticker library and its collection cursor."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import col

from bot import logger
from bot.core.db import BaseDatabase
from bot.life.emoji.models import LifeEmoji, LifeEmojiAxis, LifeEmojiCursor


class EmojiStore:
    """Async SQLite store for stickers."""

    def __init__(self, db_helper: BaseDatabase) -> None:
        """Initialize the store.

        Args:
            db_helper: Core async database helper.
        """
        self.db = db_helper

    async def ensure_schema(self) -> None:
        """Create the emoji tables when missing."""
        async with self.db.get_db() as session:
            connection = await session.connection()
            for model in (LifeEmoji, LifeEmojiAxis, LifeEmojiCursor):
                await connection.run_sync(
                    lambda sync_connection, model=model: model.__table__.create(  # ty: ignore[unresolved-attribute]  # SQLModel adds __table__ at runtime
                        sync_connection,
                        checkfirst=True,
                    )
                )
            await session.commit()

    async def insert_many(self, rows: Sequence[dict[str, Any]]) -> int:
        """Insert stickers, ignoring duplicates by content hash.

        Args:
            rows: Fully specified row payloads.

        Returns:
            Number of rows actually inserted.
        """
        if not rows:
            return 0
        async with self.db.get_db() as session:
            result = await session.execute(
                sqlite_insert(LifeEmoji)
                .values(list(rows))
                .on_conflict_do_nothing(
                    index_elements=["session_key", "sha256"],
                )
            )
            await session.commit()
            return max(int(result.rowcount or 0), 0)  # ty: ignore[unresolved-attribute]  # CursorResult exposes rowcount

    async def get(self, emoji_id: int) -> dict[str, Any] | None:
        """Return one sticker row.

        Args:
            emoji_id: Row id.

        Returns:
            Row dictionary, or None when missing.
        """
        async with self.db.get_db() as session:
            row = await session.get(LifeEmoji, emoji_id)
            return row.model_dump() if row else None

    async def get_by_hash(self, session_key: str, sha256: str) -> dict[str, Any] | None:
        """Return one sticker by content hash.

        Args:
            session_key: Session scope.
            sha256: Content hash.

        Returns:
            Row dictionary, or None when unknown.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifeEmoji).where(
                    col(LifeEmoji.session_key) == session_key,
                    col(LifeEmoji.sha256) == sha256,
                )
            )
            row = result.scalars().first()
            return row.model_dump() if row else None

    async def list_emojis(
        self,
        session_key: str,
        status: str = "active",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return stickers of one session, most used first.

        Args:
            session_key: Session scope.
            status: Lifecycle status filter.
            limit: Maximum rows.

        Returns:
            Row dictionaries.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifeEmoji)
                .where(
                    col(LifeEmoji.session_key) == session_key,
                    col(LifeEmoji.status) == status,
                )
                .order_by(
                    desc(col(LifeEmoji.usage_count)),
                    desc(col(LifeEmoji.created_at)),
                )
                .limit(max(limit, 1))
            )
            return [row.model_dump() for row in result.scalars().all()]

    async def pending_embeddings(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return stickers that still need a vector.

        Args:
            limit: Maximum rows.

        Returns:
            Row dictionaries.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifeEmoji)
                .where(col(LifeEmoji.embedding).is_(None))
                .order_by(col(LifeEmoji.id))
                .limit(max(limit, 1))
            )
            return [row.model_dump() for row in result.scalars().all()]

    async def set_embedding(
        self,
        emoji_id: int,
        vector: bytes,
        model: str,
        dimensions: int,
    ) -> None:
        """Attach a vector to a sticker.

        Args:
            emoji_id: Row id.
            vector: Packed float vector.
            model: Embedding model identifier.
            dimensions: Vector size.
        """
        async with self.db.get_db() as session:
            row = await session.get(LifeEmoji, emoji_id)
            if row is None:
                return
            row.embedding = vector
            row.embedding_model = model
            row.embedding_dim = dimensions
            row.updated_at = int(time.time())
            await session.commit()

    async def update_tags(
        self,
        emoji_id: int,
        tags: list[str] | None = None,
        status: str | None = None,
        caption: str | None = None,
    ) -> bool:
        """Update labels or status of one sticker.

        Args:
            emoji_id: Row id.
            tags: Replacement tag list.
            status: ``active`` or ``rejected``.
            caption: Replacement caption.

        Returns:
            True when the row was updated.
        """
        async with self.db.get_db() as session:
            row = await session.get(LifeEmoji, emoji_id)
            if row is None:
                return False
            if tags is not None:
                row.tags = list(tags)
            if caption is not None:
                row.caption = caption
            if status in ("active", "rejected"):
                row.status = status
            row.updated_at = int(time.time())
            await session.commit()
            return True

    async def bump_usage(self, emoji_id: int) -> None:
        """Record that a sticker was sent.

        Args:
            emoji_id: Row id.
        """
        now = int(time.time())
        async with self.db.get_db() as session:
            row = await session.get(LifeEmoji, emoji_id)
            if row is None:
                return
            row.usage_count = int(row.usage_count or 0) + 1
            row.last_used_ts = now
            row.updated_at = now
            await session.commit()

    async def delete(
        self,
        emoji_ids: Iterable[int] | None = None,
        session_key: str | None = None,
    ) -> list[str]:
        """Delete stickers and return their file paths.

        Args:
            emoji_ids: Explicit row ids.
            session_key: Delete every sticker of one session.

        Returns:
            File paths that are no longer referenced.

        Raises:
            ValueError: When neither filter is given.
        """
        if not emoji_ids and not session_key:
            raise ValueError("delete requires emoji_ids or session_key")
        async with self.db.get_db() as session:
            if emoji_ids:
                rows = await session.execute(
                    select(LifeEmoji).where(col(LifeEmoji.id).in_(tuple(emoji_ids)))
                )
            else:
                rows = await session.execute(
                    select(LifeEmoji).where(col(LifeEmoji.session_key) == session_key)
                )
            paths = [str(row.file_path) for row in rows.scalars().all()]
            if emoji_ids:
                await session.execute(
                    delete(LifeEmoji).where(col(LifeEmoji.id).in_(tuple(emoji_ids)))
                )
            else:
                await session.execute(
                    delete(LifeEmoji).where(col(LifeEmoji.session_key) == session_key)
                )
            await session.commit()
        return paths

    async def cursor(self, session_key: str) -> int:
        """Return the last collection timestamp of a session.

        Args:
            session_key: Session scope.

        Returns:
            Last processed timestamp, or 0 when never collected.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifeEmojiCursor).where(
                    col(LifeEmojiCursor.session_key) == session_key
                )
            )
            row = result.scalars().first()
            return int(row.last_ts) if row else 0

    async def set_cursor(self, session_key: str, last_ts: int) -> None:
        """Persist the collection cursor of a session.

        Args:
            session_key: Session scope.
            last_ts: Last processed timestamp.
        """
        now = int(time.time())
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifeEmojiCursor).where(
                    col(LifeEmojiCursor.session_key) == session_key
                )
            )
            row = result.scalars().first()
            if row is None:
                session.add(
                    LifeEmojiCursor(
                        session_key=session_key,
                        last_ts=last_ts,
                        updated_at=now,
                    )
                )
            else:
                row.last_ts = last_ts
                row.updated_at = now
            await session.commit()

    async def stats(self) -> dict[str, Any]:
        """Return sticker counters.

        Returns:
            Mapping with totals, bytes, vector coverage and usage.
        """
        async with self.db.get_db() as session:
            total = (
                await session.execute(select(func.count()).select_from(LifeEmoji))
            ).scalar_one()
            size = (
                await session.execute(select(func.sum(LifeEmoji.size_bytes)))
            ).scalar_one()
            embedded = (
                await session.execute(
                    select(func.count())
                    .select_from(LifeEmoji)
                    .where(col(LifeEmoji.embedding).is_not(None))
                )
            ).scalar_one()
            used = (
                await session.execute(
                    select(func.count())
                    .select_from(LifeEmoji)
                    .where(col(LifeEmoji.usage_count) > 0)
                )
            ).scalar_one()
        return {
            "total": int(total or 0),
            "bytes": int(size or 0),
            "embedded": int(embedded or 0),
            "used": int(used or 0),
        }

    def prune_files(self, directory: Path, keep: int) -> int:
        """Delete the oldest files when the library exceeds its cap.

        Args:
            directory: Sticker directory.
            keep: Maximum number of files to keep.

        Returns:
            Number of files deleted.
        """
        if keep <= 0 or not directory.exists():
            return 0
        files = sorted(
            (path for path in directory.iterdir() if path.is_file()),
            key=lambda path: path.stat().st_mtime,
        )
        excess = len(files) - keep
        removed = 0
        for path in files[: max(excess, 0)]:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning(f"[life] failed to prune emoji file {path}: {exc}")
        return removed

    async def pending_axes(self, limit: int = 12) -> list[dict[str, Any]]:
        """Return active stickers that still need tag-axis vectors.

        Args:
            limit: Maximum rows.

        Returns:
            Row dictionaries of stickers without any axis row.
        """
        async with self.db.get_db() as session:
            has_axis = (
                select(col(LifeEmojiAxis.id))
                .where(
                    col(LifeEmojiAxis.session_key) == col(LifeEmoji.session_key),
                    col(LifeEmojiAxis.sha256) == col(LifeEmoji.sha256),
                )
                .exists()
            )
            result = await session.execute(
                select(LifeEmoji)
                .where(col(LifeEmoji.status) == "active", ~has_axis)
                .order_by(col(LifeEmoji.id))
                .limit(max(limit, 1))
            )
            return [row.model_dump() for row in result.scalars().all()]

    async def upsert_axis(
        self,
        session_key: str,
        sha256: str,
        axis: str,
        text: str,
        vector: bytes,
        model: str,
        dimensions: int,
    ) -> None:
        """Insert or replace one embedded tag axis.

        Args:
            session_key: Session owning the sticker.
            sha256: Sticker content hash.
            axis: ``emotion`` or ``content``.
            text: Tag text that produced the vector.
            vector: Packed float vector.
            model: Embedding model identifier.
            dimensions: Vector size.
        """
        async with self.db.get_db() as session:
            await session.execute(
                sqlite_insert(LifeEmojiAxis)
                .values(
                    session_key=session_key,
                    sha256=sha256,
                    axis=axis,
                    text=text,
                    vector=vector,
                    dim=dimensions,
                    model=model,
                    updated_at=int(time.time()),
                )
                .on_conflict_do_update(
                    index_elements=["session_key", "sha256", "axis"],
                    set_={
                        "text": text,
                        "vector": vector,
                        "dim": dimensions,
                        "model": model,
                        "updated_at": int(time.time()),
                    },
                )
            )
            await session.commit()

    async def active_axes(self, session_key: str) -> list[dict[str, Any]]:
        """Return axis vectors joined with their active stickers.

        Args:
            session_key: Session scope.

        Returns:
            Sticker rows enriched with ``axis`` / ``axis_vector`` / ``axis_text``.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                select(LifeEmoji, LifeEmojiAxis)
                .join(
                    LifeEmojiAxis,
                    (col(LifeEmojiAxis.session_key) == col(LifeEmoji.session_key))
                    & (col(LifeEmojiAxis.sha256) == col(LifeEmoji.sha256)),
                )
                .where(
                    col(LifeEmoji.session_key) == session_key,
                    col(LifeEmoji.status) == "active",
                )
            )
            rows: list[dict[str, Any]] = []
            for emoji, axis in result.all():
                row = emoji.model_dump()
                row["axis"] = axis.axis
                row["axis_vector"] = axis.vector
                row["axis_text"] = axis.text
                rows.append(row)
            return rows
