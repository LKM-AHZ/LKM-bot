"""Service-identity client for LKM-service (the only bot -> backend egress).

Background: the bot used to write generated media (TTS audio, T2I images, attachments) to its own
container disk and expose it through an in-process token table. With the backend integration the
objects live in the backend storage backend (Local/S3 behind one protocol); the bot only uploads
bytes and receives a signed URL that IM platforms can fetch.

Configuration is read from environment variables (same convention as ``bot/lkm/sso.py``) instead of
``cmd_config.json``: these are service credentials and must not end up in an exportable/backed-up
config file.

- ``LKM_SERVICE_BASE_URL``: backend root, e.g. ``http://backend:8000``.
- ``LKM_INTERNAL_TOKEN``: same shared token as the backend's ``LKM_BOT_INTERNAL_TOKEN``.
- ``LKM_SERVICE_PUBLIC_ORIGIN``: public origin used to absolutize media URLs
  (e.g. ``https://lkm.example.com``). When unset it falls back to the base URL origin, which is
  only correct for single-host development; production must set it or IM clients cannot fetch.

Failure semantics: every method raises :class:`BackendUnavailable` when unconfigured or
unreachable. Callers own the fallback (the media path falls back to the local token service so a
backend outage never blocks message delivery).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

BASE_URL_ENV = "LKM_SERVICE_BASE_URL"
TOKEN_ENV = "LKM_INTERNAL_TOKEN"
PUBLIC_ORIGIN_ENV = "LKM_SERVICE_PUBLIC_ORIGIN"
TIMEOUT_ENV = "LKM_SERVICE_TIMEOUT_S"

DEFAULT_TIMEOUT_S = 10.0
#: Uploads are much slower than metadata calls (audio/images up to ``bot_media_max_bytes``).
UPLOAD_TIMEOUT_S = 120.0


class BackendUnavailable(Exception):
    """Backend is unconfigured, unreachable, or answered with an error."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


class BackendClient:
    """Thin client over the backend internal file API.

    Attributes:
        base_url: Backend root without trailing slash.
        public_origin: Public origin for media URLs without trailing slash; may be empty.
        timeout: Default request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        public_origin: str = "",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.public_origin = (public_origin or "").rstrip("/")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        """Whether both an address and a token are present."""
        return bool(self.base_url and self._token)

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=timeout or self.timeout,
        )

    def absolute_url(self, url: str) -> str:
        """Absolutize a backend URL so IM platforms can fetch it directly.

        Args:
            url: Absolute URL or a gateway-relative path such as ``/api/v1/media/<id>``.

        Returns:
            Absolute URL, or the input unchanged when neither origin is configured.
        """
        if not url:
            return ""
        if url.startswith(("http://", "https://")):
            return url
        origin = self.public_origin or self.base_url
        if not origin:
            return url
        return f"{origin}{url if url.startswith('/') else '/' + url}"

    async def _request(
        self, method: str, path: str, *, timeout: float | None = None, **kwargs: Any
    ) -> Any:
        """Send one request and unwrap the ``{status, message, data}`` envelope.

        Args:
            method: HTTP method.
            path: Path relative to the backend root.
            timeout: Per-request timeout override.
            **kwargs: Passed through to ``httpx`` (params/files/data).

        Returns:
            The envelope's ``data`` member, or the raw JSON body when there is no envelope.

        Raises:
            BackendUnavailable: Network error, non-2xx status, or an error envelope.
        """
        if not self.configured:
            raise BackendUnavailable("backend is not configured")
        try:
            async with self._client(timeout) as client:
                resp = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"backend request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise BackendUnavailable(
                f"backend returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise BackendUnavailable("backend returned non-JSON response") from exc
        if isinstance(payload, dict) and "data" in payload:
            data = payload["data"]
            if data is None and payload.get("status") not in (None, "ok", 0):
                raise BackendUnavailable(
                    f"backend error: {payload.get('message') or 'unknown'}"
                )
            return data
        return payload

    async def upload_media(
        self,
        path: str | Path,
        *,
        source: str = "other",
        name: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        """Upload one media object and return its registration view.

        Args:
            path: Local file path.
            source: ``tts`` / ``t2i`` / ``attachment`` / ``other``.
            name: Overrides the original file name.
            mime_type: Overrides the content type (defaults to ``application/octet-stream``).

        Returns:
            Backend registration view; ``url`` is absolutized.

        Raises:
            BackendUnavailable: File missing, upload rejected, or backend unreachable.
        """
        media_path = Path(path)
        if not media_path.exists():
            raise BackendUnavailable(f"media file not found: {media_path}")
        files = {
            "file": (
                name or media_path.name,
                media_path.read_bytes(),
                mime_type or "application/octet-stream",
            )
        }
        result = await self._request(
            "POST",
            "/api/v1/internal/files",
            timeout=UPLOAD_TIMEOUT_S,
            files=files,
            data={"source": source},
        )
        if not isinstance(result, dict):
            raise BackendUnavailable("unexpected upload response")
        view = dict(result)
        view["url"] = self.absolute_url(str(view.get("url") or ""))
        return view

    async def delete_media(self, media_id: str) -> None:
        """Delete one media registration (backend drops the blob when ref_count hits zero)."""
        await self._request("DELETE", f"/api/v1/internal/files/{media_id}")

    async def media_status(self) -> dict[str, Any]:
        """Return internal file API status (object count, bytes, limits)."""
        data = await self._request("GET", "/api/v1/internal/files/status")
        return data if isinstance(data, dict) else {}

    async def list_library_files(
        self, *, page: int = 1, size: int = 20, keyword: str = ""
    ) -> dict[str, Any]:
        """List approved entries in the community file library (read-only).

        Args:
            page: 1-based page number.
            size: Page size.
            keyword: Optional name/content filter.

        Returns:
            ``{items, total, page, size}``.
        """
        params: dict[str, Any] = {"page": page, "size": size}
        if keyword:
            params["keyword"] = keyword
        data = await self._request(
            "GET", "/api/v1/internal/files/library", params=params
        )
        return data if isinstance(data, dict) else {}

    async def get_library_file_content(self, file_id: str) -> bytes:
        """Download one approved library file's bytes (read-only).

        Args:
            file_id: Library file UUID.

        Returns:
            Raw file bytes.

        Raises:
            BackendUnavailable: Unconfigured, unreachable, or non-approved/missing file.
        """
        if not self.configured:
            raise BackendUnavailable("backend is not configured")
        try:
            async with self._client() as client:
                resp = await client.get(
                    f"/api/v1/internal/files/library/{file_id}/content"
                )
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"backend request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise BackendUnavailable(
                f"backend returned {resp.status_code}: {resp.text[:200]}"
            )
        return resp.content


_client: BackendClient | None = None


def get_backend_client() -> BackendClient:
    """Return the process-wide client (configuration is fixed at startup)."""
    global _client
    if _client is None:
        _client = BackendClient(
            _env(BASE_URL_ENV),
            _env(TOKEN_ENV),
            public_origin=_env(PUBLIC_ORIGIN_ENV),
            timeout=float(_env(TIMEOUT_ENV, str(DEFAULT_TIMEOUT_S))),
        )
    return _client


def reset_backend_client() -> None:
    """Drop the cached client so the next access re-reads the environment (tests)."""
    global _client
    _client = None
