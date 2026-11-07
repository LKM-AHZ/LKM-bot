"""Collect stickers from the archive.

Images reach the archive as components; this module turns the ones that can still
be resolved (http(s) URLs or local paths; ``base64://`` payloads are folded by the
archiver and can no longer be recovered) into files on disk plus rows in
``life_emoji``. Dedupe is by SHA-256 of the bytes, so a meme forwarded ten times
costs one file.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence
from urllib.parse import urlparse

import aiohttp

from bot import logger
from bot.life.config import EmojiConfig
from bot.life.emoji.store import EmojiStore

Fetcher = Callable[[str], Awaitable[bytes | None]]

_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"RIFF", "image/webp", ".webp"),
)
_MAX_CONTEXT_CHARS = 80


class EmojiCollector:
    """Turn archived images into sticker rows."""

    def __init__(
        self,
        source: Any,
        target: EmojiStore,
        config: EmojiConfig | None = None,
        data_dir: Path | None = None,
        fetcher: Fetcher | None = None,
    ) -> None:
        """Initialize the collector.

        Args:
            source: Archive store exposing ``fetch_range`` and ``active_sessions``.
            target: Sticker store.
            config: Collection settings.
            data_dir: Directory that receives sticker files.
            fetcher: Optional byte fetcher used by tests or offline deployments.
        """
        self.source = source
        self.target = target
        self.config = config or EmojiConfig()
        self.data_dir = Path(data_dir or "data/life/emoji")
        self.fetcher = fetcher
        self._counters = {
            "rounds": 0,
            "sessions": 0,
            "seen": 0,
            "stored": 0,
            "duplicates": 0,
            "failed": 0,
            "skipped": 0,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return collection counters.

        Returns:
            Copy of the internal counters.
        """
        return dict(self._counters)

    async def run(self, session_keys: Sequence[str] | None = None) -> dict[str, Any]:
        """Collect from recently active sessions.

        Args:
            session_keys: Sessions to process; None discovers active ones.

        Returns:
            Per-round counters.
        """
        if not self.config.enabled:
            return {"enabled": False}
        self._counters["rounds"] += 1
        if session_keys is None:
            lookback = (
                int(time.time()) - max(self.config.collect_interval_minutes, 1) * 120
            )
            session_keys = await self.source.active_sessions(
                since_ts=lookback, limit=20
            )
        keys = list(session_keys)[:20]
        for session_key in keys:
            try:
                await self.collect_session(session_key)
            except Exception:
                logger.exception(f"[life] emoji collection failed for {session_key}")
        self._counters["sessions"] += len(keys)
        self.target.prune_files(self.data_dir, self.config.max_storage_files)
        return {"enabled": True, **self.snapshot()}

    async def collect_session(self, session_key: str) -> dict[str, Any]:
        """Collect stickers from one session since its cursor.

        Args:
            session_key: Session scope.

        Returns:
            Counters of this session.
        """
        cursor = await self.target.cursor(session_key)
        messages = await self.source.fetch_range(
            session_key,
            since_ts=cursor,
            limit=max(self.config.max_collect_messages, 1),
        )
        if not messages:
            self._counters["skipped"] += 1
            return {"session": session_key, "skipped": True}
        rows: list[dict[str, Any]] = []
        batch_hashes: set[str] = set()
        for message in messages:
            context = str(message.get("plain_text") or "").strip()[:_MAX_CONTEXT_CHARS]
            for component in message.get("components") or []:
                reference = self._image_reference(component)
                if not reference:
                    continue
                self._counters["seen"] += 1
                payload = await self._fetch(reference)
                if payload is None:
                    self._counters["failed"] += 1
                    continue
                digest = hashlib.sha256(payload).hexdigest()
                mime, suffix = self._sniff(payload)
                if not mime:
                    self._counters["skipped"] += 1
                    continue
                if len(payload) > self.config.max_bytes:
                    self._counters["skipped"] += 1
                    continue
                if digest in batch_hashes:
                    self._counters["duplicates"] += 1
                    continue
                existing = await self.target.get_by_hash(session_key, digest)
                if existing is not None:
                    self._counters["duplicates"] += 1
                    continue
                batch_hashes.add(digest)
                path = self._write_file(session_key, digest, suffix, payload)
                if path is None:
                    self._counters["failed"] += 1
                    continue
                rows.append(
                    {
                        "session_key": session_key,
                        "sha256": digest,
                        "file_path": str(path),
                        "mime": mime,
                        "size_bytes": len(payload),
                        "tags": self._tags(context),
                        "caption": context,
                        "created_at": int(time.time()),
                        "updated_at": int(time.time()),
                        "source": "archive",
                    }
                )
            if len(rows) >= 50:
                break
        inserted = await self.target.insert_many(rows)
        self._counters["stored"] += inserted
        await self.target.set_cursor(session_key, int(messages[-1].get("ts") or 0))
        return {"session": session_key, "images": len(rows), "stored": inserted}

    def _image_reference(self, component: Any) -> str | None:
        """Return the resolvable reference of an image component.

        Args:
            component: Archived component payload.

        Returns:
            http(s) URL or local path, or None when the image is gone.
        """
        if not isinstance(component, dict):
            return None
        if str(component.get("type") or "").lower() != "image":
            return None
        data = (
            component.get("data")
            if isinstance(component.get("data"), dict)
            else component
        )
        for key in ("url", "file", "path"):
            value = data.get(key)
            if not isinstance(value, str) or not value:
                continue
            if value.startswith("base64://"):
                continue
            if value.startswith(("http://", "https://", "file://")) or value.startswith(
                "/"
            ):
                return value
        return None

    async def _fetch(self, reference: str) -> bytes | None:
        """Fetch bytes of one image reference.

        Args:
            reference: http(s) URL, file URI or local path.

        Returns:
            File bytes, or None when fetching failed.
        """
        if self.fetcher is not None:
            return await self.fetcher(reference)
        if reference.startswith(("http://", "https://")):
            if not self.config.allow_remote:
                return None
            try:
                timeout = aiohttp.ClientTimeout(
                    total=max(self.config.download_timeout_seconds, 1.0)
                )
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(reference) as response:
                        if response.status != 200:
                            return None
                        if (
                            int(response.headers.get("Content-Length") or 0)
                            > self.config.max_bytes
                        ):
                            return None
                        return await response.content.read(self.config.max_bytes + 1)
            except Exception as exc:
                logger.debug(f"[life] emoji download failed: {exc}")
                return None
        path: Path | None = None
        if reference.startswith("file://"):
            parsed = urlparse(reference)
            path = Path(parsed.path)
        else:
            path = Path(reference)
        try:
            if not path.is_file():
                return None
            if path.stat().st_size > self.config.max_bytes:
                return None
            return path.read_bytes()
        except OSError as exc:
            logger.debug(f"[life] emoji read failed: {exc}")
            return None

    def _sniff(self, payload: bytes) -> tuple[str, str]:
        """Detect the image type from its magic bytes.

        Args:
            payload: File bytes.

        Returns:
            ``(mime, suffix)`` or ``("", "")`` when it is not a supported image.
        """
        for magic, mime, suffix in _MAGIC:
            if payload.startswith(magic):
                return mime, suffix
        return "", ""

    def _write_file(
        self,
        session_key: str,
        digest: str,
        suffix: str,
        payload: bytes,
    ) -> Path | None:
        """Persist sticker bytes under the session directory.

        Args:
            session_key: Session scope.
            digest: Content hash.
            suffix: File suffix.
            payload: File bytes.

        Returns:
            Written path, or None when writing failed.
        """
        session_dir = (
            self.data_dir / hashlib.sha1(session_key.encode()).hexdigest()[:16]
        )
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            path = session_dir / f"{digest}{suffix}"
            if not path.exists():
                path.write_bytes(payload)
            return path
        except OSError as exc:
            logger.warning(f"[life] failed to write emoji file: {exc}")
            return None

    def _tags(self, context: str) -> list[str]:
        """Derive retrieval tags from the surrounding message text.

        Args:
            context: Message text that carried the image.

        Returns:
            Up to ``max_tags`` short labels.
        """
        if not context:
            return []
        words = [
            word
            for word in context.replace("，", " ").replace("。", " ").split()
            if 1 < len(word) <= 12
        ]
        if not words:
            words = [context[:12]]
        return words[: max(self.config.max_tags, 1)]
