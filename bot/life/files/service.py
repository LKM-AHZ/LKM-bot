"""Sending, writing and downloading files for the Life layer (S6e)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from bot import logger
from bot.core.message.components import File
from bot.core.message.message_event_result import MessageChain
from bot.life.config import FilesConfig
from bot.life.files.store import FileStore, FileStoreError, StoredFile

SendChain = Callable[[str, MessageChain], Awaitable[Any]]


class FileService:
    """Generate documents, hand them to the platform and clean up after."""

    def __init__(
        self,
        config: FilesConfig,
        data_dir: Path,
        send_chain: SendChain,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Initialize the service without touching the filesystem.

        Args:
            config: Store limits, TTLs and extension allowlist.
            data_dir: Life data directory; the default root is ``<life>/files``.
            send_chain: Coroutine sending one message chain to a session.
            transport: Optional httpx transport (tests use a mock transport).
        """
        root = Path(config.root) if config.root else Path(data_dir) / "files"
        self.config = config
        self.store = FileStore(
            root,
            max_file_bytes=config.max_file_bytes,
            allowed_extensions=config.allowed_extensions,
            max_files=config.max_files,
        )
        self._send_chain = send_chain
        self._transport = transport
        self._counters = {
            "written": 0,
            "sent": 0,
            "downloaded": 0,
            "failed": 0,
            "cleaned": 0,
        }

    async def write_and_send(self, umo: str, name: str, content: str) -> str:
        """Write a text document and send it to the chat.

        Args:
            umo: Target session.
            name: File name; the extension must be allowed.
            content: Document text.

        Returns:
            A status line for the tool result.
        """
        text = str(content or "")
        if not text.strip():
            self._counters["failed"] += 1
            return "error: content is empty"
        try:
            stored = self.store.save(name or "note.md", text)
        except FileStoreError as exc:
            self._counters["failed"] += 1
            return f"error: {exc}"
        self._counters["written"] += 1
        return await self._send(umo, stored)

    async def send(self, umo: str, name: str) -> str:
        """Send an existing document from the store.

        Args:
            umo: Target session.
            name: File name in ``out/``.

        Returns:
            A status line for the tool result.
        """
        try:
            stored = self.store.sendable(
                name,
                max_bytes=self.config.max_send_bytes,
            )
        except FileStoreError as exc:
            self._counters["failed"] += 1
            return f"error: {exc}"
        return await self._send(umo, stored)

    async def download(self, umo: str, url: str, name: str = "") -> str:
        """Download an http(s) file into the store.

        Args:
            umo: Session that asked for the file (kept for bookkeeping).
            url: http(s) URL.
            name: Target name; inferred from the URL when empty.

        Returns:
            A status line for the tool result.
        """
        parsed = httpx.URL(url)
        if parsed.scheme not in {"http", "https"}:
            self._counters["failed"] += 1
            return "error: only http(s) URLs can be downloaded"
        target = (name or "").strip() or Path(parsed.path).name or "download"
        try:
            safe_name = self.store.sanitize(target)
            self.store.check_extension(safe_name)
        except FileStoreError as exc:
            self._counters["failed"] += 1
            return f"error: {exc}"
        try:
            payload = await self._fetch(url)
            stored = self.store.save(safe_name, payload)
        except FileStoreError as exc:
            self._counters["failed"] += 1
            return f"error: {exc}"
        except Exception as exc:
            self._counters["failed"] += 1
            logger.warning("[life] file download failed: %s", exc)
            return f"error: download failed: {type(exc).__name__}"
        self._counters["downloaded"] += 1
        return f"downloaded: {stored.name} ({stored.size} bytes)"

    async def cleanup(self) -> int:
        """Remove expired files and count them.

        Returns:
            Number of removed files.
        """
        removed = await asyncio.to_thread(
            self.store.cleanup,
            keep_days=self.config.keep_days,
            tmp_ttl_hours=self.config.tmp_ttl_hours,
        )
        self._counters["cleaned"] += removed
        return removed

    def status(self) -> dict:
        """Return counters and store size for the status endpoint.

        Returns:
            File service status block.
        """
        return {
            "enabled": True,
            "counters": dict(self._counters),
            "store": self.store.stats(),
            "keep_days": self.config.keep_days,
            "tmp_ttl_hours": self.config.tmp_ttl_hours,
            "max_send_bytes": self.config.max_send_bytes,
        }

    async def _send(self, umo: str, stored: StoredFile) -> str:
        """Hand one stored file to the platform.

        Args:
            umo: Target session.
            stored: File to send.

        Returns:
            A status line for the tool result.
        """
        chain = MessageChain(chain=[File(name=stored.name, file=str(stored.path))])
        result = await self._send_chain(umo, chain)
        if getattr(result, "ok", False):
            self._counters["sent"] += 1
            return f"sent: {stored.name} ({stored.size} bytes)"
        self._counters["failed"] += 1
        return f"error: {getattr(result, 'error', '') or 'send failed'}"

    async def _fetch(self, url: str) -> bytes:
        """Download one http(s) URL with a size cap.

        Args:
            url: http(s) URL.

        Returns:
            File content.

        Raises:
            FileStoreError: The URL scheme is wrong or the file is too large.
            httpx.HTTPError: The request failed.
        """
        parsed = httpx.URL(url)
        if parsed.scheme not in {"http", "https"}:
            raise FileStoreError("only http(s) URLs can be downloaded")
        limit = self.config.max_file_bytes
        timeout = max(1.0, float(self.config.download_timeout_seconds))
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise FileStoreError(f"download is over the {limit} byte limit")
                    chunks.append(chunk)
                return b"".join(chunks)
