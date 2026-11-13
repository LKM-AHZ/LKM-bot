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

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Callable

import httpx

BASE_URL_ENV = "LKM_SERVICE_BASE_URL"
TOKEN_ENV = "LKM_INTERNAL_TOKEN"
PUBLIC_ORIGIN_ENV = "LKM_SERVICE_PUBLIC_ORIGIN"
TIMEOUT_ENV = "LKM_SERVICE_TIMEOUT_S"

DEFAULT_TIMEOUT_S = 10.0
#: Uploads are much slower than metadata calls (audio/images up to ``bot_media_max_bytes``).
UPLOAD_TIMEOUT_S = 120.0
#: Connecting is either fast or broken; waiting the full read budget for a dead peer
#: turns a backend outage into a two-minute delay on every reply.
CONNECT_TIMEOUT_S = 3.0
#: Consecutive transport/5xx failures before the circuit opens.
BREAKER_THRESHOLD = 3
#: How long a tripped circuit fails fast before the next probe.
BREAKER_COOLDOWN_S = 60.0
#: Transport errors are retried once (uploads are content-addressed server-side).
RETRY_DELAY_S = 0.1


class BackendUnavailable(Exception):
    """Backend is unconfigured, unreachable, or answered with an error.

    Attributes:
        retryable: Whether another attempt could plausibly succeed (transport
            error or 5xx) rather than a request-level rejection.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


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
        transport: httpx.AsyncBaseTransport | None = None,
        breaker_threshold: int = BREAKER_THRESHOLD,
        breaker_cooldown: float = BREAKER_COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.public_origin = (public_origin or "").rstrip("/")
        self.timeout = timeout
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._breaker_threshold = max(breaker_threshold, 1)
        self._breaker_cooldown = max(breaker_cooldown, 0.0)
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0
        self._stats = {
            "requests": 0,
            "uploads": 0,
            "failures": 0,
            "retries": 0,
            "short_circuited": 0,
        }

    @property
    def configured(self) -> bool:
        """Whether both an address and a token are present."""
        return bool(self.base_url and self._token)

    def _client(self) -> httpx.AsyncClient:
        """Return the process-wide client, creating it on first use.

        Reusing one client keeps the connection pool (and the TCP/TLS setup paid
        once) instead of opening a session per media object.
        """
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self._token}"},
                transport=self._transport,
            )
        return self._http

    async def aclose(self) -> None:
        """Close the pooled client; safe to call when nothing was sent."""
        client, self._http = self._http, None
        if client is not None:
            await client.aclose()

    def _timed_out(self, timeout: float | None = None) -> httpx.Timeout:
        total = timeout or self.timeout
        return httpx.Timeout(total, connect=min(CONNECT_TIMEOUT_S, total))

    def _tripped(self) -> bool:
        return self._open_until > self._clock()

    def _record_success(self) -> None:
        self._failures = 0
        self._open_until = 0.0

    def _record_failure(self) -> None:
        self._stats["failures"] += 1
        self._failures += 1
        if self._failures >= self._breaker_threshold:
            self._open_until = self._clock() + self._breaker_cooldown
            self._failures = 0

    def stats(self) -> dict[str, Any]:
        """Return request counters and circuit state for the dashboard.

        Returns:
            Copy of the counters plus ``configured`` and breaker fields.
        """
        return {
            **self._stats,
            "configured": self.configured,
            "breaker_open": self._tripped(),
            "breaker_retry_in_s": max(0.0, self._open_until - self._clock()),
        }

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
        self,
        method: str,
        path: str,
        *,
        timeout: float | None = None,
        raw: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Send one request and unwrap the ``{status, message, data}`` envelope.

        Transport errors are retried once, and consecutive transport/5xx
        failures open a short circuit so a dead backend fails fast instead of
        stalling every reply for the full timeout.

        Args:
            method: HTTP method.
            path: Path relative to the backend root.
            timeout: Per-request timeout override.
            raw: Return the response body instead of parsing JSON.
            **kwargs: Passed through to ``httpx`` (params/files/data).

        Returns:
            The envelope's ``data`` member, the raw JSON body when there is no
            envelope, or the response bytes when ``raw`` is set.

        Raises:
            BackendUnavailable: Circuit open, network error, non-2xx status, or
                an error envelope.
        """
        if not self.configured:
            raise BackendUnavailable("backend is not configured")
        if self._tripped():
            self._stats["short_circuited"] += 1
            raise BackendUnavailable(
                "backend circuit is open",
                retryable=True,
            )

        resp: httpx.Response | None = None
        for attempt in range(2):
            self._stats["requests"] += 1
            try:
                resp = await self._client().request(
                    method,
                    path,
                    timeout=self._timed_out(timeout),
                    **kwargs,
                )
            except httpx.TransportError as exc:
                self._record_failure()
                if attempt == 0 and not self._tripped():
                    self._stats["retries"] += 1
                    await asyncio.sleep(RETRY_DELAY_S)
                    continue
                raise BackendUnavailable(
                    f"backend request failed: {exc}",
                    retryable=True,
                ) from exc
            break

        if resp is None:  # pragma: no cover - the loop either returns or raises
            raise BackendUnavailable("backend request failed")

        if resp.status_code >= 500:
            self._record_failure()
            raise BackendUnavailable(
                f"backend returned {resp.status_code}: {resp.text[:200]}",
                retryable=True,
            )
        if resp.status_code >= 400:
            # A request-level rejection says nothing about backend health.
            self._stats["failures"] += 1
            raise BackendUnavailable(
                f"backend returned {resp.status_code}: {resp.text[:200]}"
            )
        self._record_success()
        if raw:
            return resp.content
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
        try:
            await asyncio.to_thread(media_path.stat)
        except OSError as exc:
            raise BackendUnavailable(f"media file not found: {media_path}") from exc
        # ponytail: the body is buffered in memory (bounded by bot_media_max_bytes).
        # httpx reads sync file objects on the event loop, so reading off-loop is the
        # safer trade; revisit with a streaming multipart encoder if the limit grows.
        payload = await asyncio.to_thread(media_path.read_bytes)
        result = await self._request(
            "POST",
            "/api/v1/internal/files",
            timeout=UPLOAD_TIMEOUT_S,
            files={
                "file": (
                    name or media_path.name,
                    payload,
                    mime_type or "application/octet-stream",
                )
            },
            data={"source": source},
        )
        if not isinstance(result, dict):
            raise BackendUnavailable("unexpected upload response")
        self._stats["uploads"] += 1
        view = dict(result)
        view["url"] = self.absolute_url(str(view.get("url") or ""))
        return view

    async def delete_media(self, media_id: str) -> None:
        """Delete one media registration (backend drops the blob when ref_count hits zero)."""
        await self._request("DELETE", f"/api/v1/internal/files/{media_id}")

    async def media_status(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Return internal file API status (object count, bytes, limits).

        Args:
            timeout: Per-request timeout override (startup probes use a short one).

        Returns:
            Status mapping, or an empty mapping on an unexpected payload.
        """
        data = await self._request(
            "GET", "/api/v1/internal/files/status", timeout=timeout
        )
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
        data = await self._request(
            "GET",
            f"/api/v1/internal/files/library/{file_id}/content",
            raw=True,
        )
        if not isinstance(data, bytes):
            raise BackendUnavailable("unexpected library content response")
        return data


_client: BackendClient | None = None


def _env_timeout() -> float:
    """Parse ``LKM_SERVICE_TIMEOUT_S``; a typo must not break startup."""
    try:
        return float(_env(TIMEOUT_ENV, str(DEFAULT_TIMEOUT_S)))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def get_backend_client() -> BackendClient:
    """Return the process-wide client (configuration is fixed at startup)."""
    global _client
    if _client is None:
        _client = BackendClient(
            _env(BASE_URL_ENV),
            _env(TOKEN_ENV),
            public_origin=_env(PUBLIC_ORIGIN_ENV),
            timeout=_env_timeout(),
        )
    return _client


async def close_backend_client() -> None:
    """Close the pooled client without dropping configuration."""
    if _client is not None:
        await _client.aclose()


def reset_backend_client() -> None:
    """Drop the cached client so the next access re-reads the environment (tests)."""
    global _client
    _client = None
