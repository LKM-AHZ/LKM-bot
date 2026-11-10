"""Sandboxed on-disk store for generated documents and scratch files."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from bot.life.common import prune_oldest_files

_UNSAFE = re.compile(r"[^\w.\-]+", re.UNICODE)
# Windows reserved device names must not become file names.
_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class FileStoreError(ValueError):
    """A file name, extension or size was rejected."""


@dataclass(frozen=True, slots=True)
class StoredFile:
    """One file in the store."""

    name: str
    path: Path
    size: int
    created_at: float

    def to_dict(self) -> dict:
        """Return a serializable view without the absolute path.

        Returns:
            Name, size and creation timestamp.
        """
        return {
            "name": self.name,
            "size": self.size,
            "created_at": round(self.created_at, 3),
        }


class FileStore:
    """Write, resolve and clean files under one root directory."""

    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int = 2 * 1024 * 1024,
        allowed_extensions: list[str] | tuple[str, ...] = (),
        max_files: int = 500,
    ) -> None:
        """Initialize the store without touching the filesystem.

        Args:
            root: Sandbox root; ``out/`` and ``tmp/`` live under it.
            max_file_bytes: Cap of one written file.
            allowed_extensions: Lowercase extensions with a leading dot.
            max_files: Files kept in ``out/``.
        """
        self.root = Path(root)
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.allowed_extensions = {
            str(extension).lower() for extension in allowed_extensions
        }
        self.max_files = max(1, int(max_files))

    @property
    def out_dir(self) -> Path:
        """Directory for generated documents."""
        return self.root / "out"

    @property
    def tmp_dir(self) -> Path:
        """Directory for scratch files."""
        return self.root / "tmp"

    def ensure_dirs(self) -> None:
        """Create the root and both subdirectories."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def sanitize(self, name: str) -> str:
        """Return a safe base name for the store.

        Args:
            name: Requested file name (may contain a path).

        Returns:
            A name without directories or unsafe characters.

        Raises:
            FileStoreError: The name is empty or reserved.
        """
        base = Path(str(name or "").replace("\\", "/")).name
        base = _UNSAFE.sub("_", base).strip(" ._")
        if not base or base in {".", ".."}:
            raise FileStoreError("file name is empty")
        stem = base.split(".", 1)[0].lower()
        if stem in _RESERVED:
            raise FileStoreError("file name is reserved")
        return base[:120]

    def check_extension(self, name: str) -> None:
        """Reject a name whose extension is not allowed.

        Args:
            name: Sanitized file name.

        Raises:
            FileStoreError: The extension is not in the allowlist.
        """
        extension = Path(name).suffix.lower()
        if self.allowed_extensions and extension not in self.allowed_extensions:
            raise FileStoreError(f"extension {extension or '(none)'} is not allowed")

    def save(
        self,
        name: str,
        content: bytes | str,
        *,
        subdir: str = "out",
    ) -> StoredFile:
        """Write one file into the sandbox.

        Args:
            name: Requested name; sanitized and overwritten when it exists.
            content: Bytes or UTF-8 text.
            subdir: ``out`` or ``tmp``.

        Returns:
            The stored file.

        Raises:
            FileStoreError: Name, extension or size was rejected.
        """
        if subdir not in {"out", "tmp"}:
            raise FileStoreError("subdir must be out or tmp")
        safe_name = self.sanitize(name)
        self.check_extension(safe_name)
        payload = content.encode("utf-8") if isinstance(content, str) else content
        if len(payload) > self.max_file_bytes:
            raise FileStoreError(
                f"file is {len(payload)} bytes, over the {self.max_file_bytes} limit"
            )
        self.ensure_dirs()
        path = (self.out_dir if subdir == "out" else self.tmp_dir) / safe_name
        path.write_bytes(payload)
        self.enforce_limit()
        return StoredFile(
            name=safe_name,
            path=path,
            size=len(payload),
            created_at=path.stat().st_mtime,
        )

    def resolve(self, name: str, *, subdir: str = "out") -> Path:
        """Resolve one file inside the sandbox.

        Args:
            name: File name as handed out earlier.
            subdir: ``out`` or ``tmp``.

        Returns:
            The absolute path.

        Raises:
            FileStoreError: The name escapes the sandbox or the file is missing.
        """
        safe_name = self.sanitize(name)
        base = (self.out_dir if subdir == "out" else self.tmp_dir).resolve()
        path = (base / safe_name).resolve()
        if not path.is_relative_to(base):
            raise FileStoreError("file name escapes the sandbox")
        if not path.is_file():
            raise FileStoreError(f"no such file: {safe_name}")
        return path

    def sendable(self, name: str, *, max_bytes: int) -> StoredFile:
        """Resolve a file that may be handed to the platform.

        Args:
            name: File name in ``out/``.
            max_bytes: Platform-side size cap.

        Returns:
            The stored file.

        Raises:
            FileStoreError: The file is missing, not regular or too large.
        """
        path = self.resolve(name)
        size = path.stat().st_size
        if size > max_bytes:
            raise FileStoreError(f"file is {size} bytes, over the {max_bytes} limit")
        return StoredFile(
            name=path.name,
            path=path,
            size=size,
            created_at=path.stat().st_mtime,
        )

    def cleanup(
        self,
        *,
        keep_days: int = 7,
        tmp_ttl_hours: int = 24,
        now: float | None = None,
    ) -> int:
        """Remove expired scratch files and old documents.

        Args:
            keep_days: Documents older than this many days are removed.
            tmp_ttl_hours: Scratch files older than this many hours are removed.
            now: Wall-clock timestamp; tests inject one.

        Returns:
            Number of removed files.
        """
        current = time.time() if now is None else now
        removed = 0
        for directory, ttl_seconds in (
            (self.out_dir, max(0, keep_days) * 86400),
            (self.tmp_dir, max(0, tmp_ttl_hours) * 3600),
        ):
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                if not path.is_file():
                    continue
                if current - path.stat().st_mtime > ttl_seconds:
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        continue
        removed += self.enforce_limit()
        return removed

    def enforce_limit(self) -> int:
        """Remove the oldest documents beyond ``max_files``.

        Returns:
            Number of removed files.
        """
        return prune_oldest_files(self.out_dir, self.max_files, "document")

    def list_recent(self, limit: int = 20) -> list[StoredFile]:
        """Return the newest documents.

        Args:
            limit: Maximum files returned.

        Returns:
            Stored files, newest first.
        """
        if not self.out_dir.is_dir():
            return []
        files = [
            StoredFile(
                name=path.name,
                path=path,
                size=path.stat().st_size,
                created_at=path.stat().st_mtime,
            )
            for path in self.out_dir.iterdir()
            if path.is_file()
        ]
        files.sort(key=lambda item: item.created_at, reverse=True)
        return files[: max(0, int(limit))]

    def stats(self) -> dict:
        """Return counts for the status endpoint.

        Returns:
            File count, total bytes and both subdirectory sizes.
        """
        files = 0
        total = 0
        for directory in (self.out_dir, self.tmp_dir):
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                if path.is_file():
                    files += 1
                    total += path.stat().st_size
        return {"files": files, "bytes": total}
