import logging
import ssl
import threading
from typing import TYPE_CHECKING, Any

import certifi

if TYPE_CHECKING:
    import aiohttp

logger = logging.getLogger("astrbot")

_SHARED_TLS_CONTEXT: ssl.SSLContext | None = None
_SHARED_TLS_CONTEXT_LOCK = threading.Lock()


def _create_ssl_context(log_obj: Any | None = None) -> ssl.SSLContext:
    """Build an SSL context from the system trust store and add certifi CAs."""
    log = log_obj or logger

    ssl_context = ssl.create_default_context()
    try:
        ssl_context.load_verify_locations(cafile=certifi.where())
    except Exception as exc:
        if log and hasattr(log, "warning"):
            log.warning(
                "Failed to load certifi CA bundle into SSL context; "
                "falling back to system trust store only: %s",
                exc,
            )

    return ssl_context


def build_ssl_context_with_certifi(log_obj: Any | None = None) -> ssl.SSLContext:
    """Return the process-wide SSL context built from system CA + certifi."""
    global _SHARED_TLS_CONTEXT

    if _SHARED_TLS_CONTEXT is not None:
        return _SHARED_TLS_CONTEXT

    with _SHARED_TLS_CONTEXT_LOCK:
        if _SHARED_TLS_CONTEXT is not None:
            return _SHARED_TLS_CONTEXT

        _SHARED_TLS_CONTEXT = _create_ssl_context(log_obj)
        return _SHARED_TLS_CONTEXT


def build_tls_connector() -> "aiohttp.TCPConnector":
    import aiohttp

    return aiohttp.TCPConnector(ssl=build_ssl_context_with_certifi())
