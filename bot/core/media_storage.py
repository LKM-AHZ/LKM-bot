"""Publish generated media and produce a URL that messaging platforms can fetch.

Two publishers, selected per call:

1. **Backend** (``LKM_SERVICE_BASE_URL`` + ``LKM_INTERNAL_TOKEN`` configured): upload the bytes to
   LKM-service storage and return its signed URL. Objects survive container rebuilds and are
   shared across replicas.
2. **Local** (fallback): register the file in the in-process token table and return
   ``<callback_api_base>/api/v1/files/tokens/<token>`` — the pre-integration behaviour.

``media_storage_mode`` in the bot config controls the preference: ``auto`` (default, backend first
then local), ``local`` (never call the backend), ``backend`` (backend only; a failure is logged and
still falls back so a backend outage never drops a reply).
"""

from __future__ import annotations

from typing import Any

from bot.core import bot_config, file_token_service
from bot.core.utils.media_utils import file_uri_to_path, is_file_uri
from bot.lkm.backend_client import BackendUnavailable, get_backend_client


def _log() -> Any:
    """Return the bot logger lazily.

    Must go through ``LogManager.GetLogger``: loguru formatting expects the extra fields it
    injects, and a stdlib module logger raises during formatting on the request path.
    """
    from bot.core.log import LogManager

    return LogManager.GetLogger("bot.core.media_storage")


async def public_media_url(path: str, *, source: str = "other") -> str | None:
    """Publish one local media file and return a fetchable URL.

    Args:
        path: Local path or ``file://`` URI.
        source: ``tts`` / ``t2i`` / ``attachment`` / ``other`` (backend analytics only).

    Returns:
        Absolute URL, or None when neither the backend nor ``callback_api_base`` is configured.
    """
    mode = str(bot_config.get("media_storage_mode", "auto") or "auto").strip().lower()
    if mode not in {"auto", "local", "backend"}:
        mode = "auto"

    if mode != "local":
        client = get_backend_client()
        if client.configured:
            local_path = file_uri_to_path(path) if is_file_uri(path) else str(path)
            try:
                view = await client.upload_media(local_path, source=source)
                url = str(view.get("url") or "")
                if url:
                    return url
                _log().warning("Backend media upload returned an empty url: %s", path)
            except (BackendUnavailable, OSError) as exc:
                # Falling back keeps replies flowing; the warning tells operators the backend
                # link is broken without turning a media failure into a message failure.
                _log().warning(
                    "Backend media upload failed, falling back to local: %s", exc
                )
        elif mode == "backend":
            _log().warning(
                "media_storage_mode=backend but "
                "LKM_SERVICE_BASE_URL/LKM_INTERNAL_TOKEN are not configured",
            )

    callback_host = str(bot_config.get("callback_api_base") or "").strip()
    if not callback_host:
        return None
    token = await file_token_service.register_file(path)
    _log().debug(
        "Registered local media: %s/api/v1/files/tokens/%s", callback_host, token
    )
    return f"{callback_host}/api/v1/files/tokens/{token}"


async def require_media_url(path: str, *, source: str = "other") -> str:
    """Return a fetchable media URL or raise when no publisher is configured.

    Args:
        path: Local path or ``file://`` URI.
        source: ``tts`` / ``t2i`` / ``attachment`` / ``other``.

    Returns:
        Absolute URL.

    Raises:
        Exception: Neither the backend nor ``callback_api_base`` is configured.
    """
    url = await public_media_url(path, source=source)
    if url is None:
        raise Exception(
            "未配置文件服务：既没有后端接入（LKM_SERVICE_BASE_URL/LKM_INTERNAL_TOKEN），"
            "也没有配置 callback_api_base"
        )
    return url
