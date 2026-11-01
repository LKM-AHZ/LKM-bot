"""Community-account verification for the dashboard login (AUTH delegation).

Why: the panel kept its own md5 password store, so every operator had two credentials and the bot
had a second password policy to defend. With delegation the credential check moves to LKM-service
AUTH (``POST /auth/internal/verify-password``) while the panel keeps its own session machinery —
only *who may log in* changes, not how the session works.

Modes (``dashboard.auth_mode`` in the bot config):

- ``local`` (default): panel password only — unchanged behaviour, no backend dependency.
- ``both``: panel password **or** a community account whose ``account_level`` is ``admin``.
- ``backend``: community account only. AUTH unreachable fails closed (clear error), so a broken
  link cannot silently downgrade to the local password.

Only ``admin`` community accounts are accepted: the panel is a control plane (it can reconfigure
platforms, install plugins, read logs), and the panel has no read-only role to grant others.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

AUTH_BASE_URL_ENV = "LKM_AUTH_BASE_URL"
AUTH_TOKEN_ENV = "LKM_AUTH_INTERNAL_TOKEN"

DEFAULT_TIMEOUT_S = 8.0
#: Community account level allowed into the control plane.
REQUIRED_ACCOUNT_LEVEL = "admin"


class AccountAuthUnavailable(Exception):
    """AUTH is unconfigured, unreachable, or answered unexpectedly (not a credential rejection)."""


def auth_mode(config: Any) -> str:
    """Return the configured mode: ``local``, ``both``, or ``backend``.

    Args:
        config: Bot config; the value lives at ``dashboard.auth_mode``.

    Returns:
        One of the three modes. Unknown values fall back to ``local`` so a typo never widens
        the accepted credential set.
    """
    dashboard = config.get("dashboard") or {}
    mode = str(dashboard.get("auth_mode", "local") or "local").strip().lower()
    return mode if mode in {"local", "both", "backend"} else "local"


def _config() -> tuple[str, str]:
    base = (os.environ.get(AUTH_BASE_URL_ENV, "") or "").strip().rstrip("/")
    token = (os.environ.get(AUTH_TOKEN_ENV, "") or "").strip()
    return base, token


async def verify_community_account(
    account: str, password: str, *, ip_address: str = ""
) -> dict[str, Any] | None:
    """Verify a pair of community credentials and return the minimal identity.

    Args:
        account: Username, email, or phone as typed in the panel login form.
        password: Plaintext password (internal network hop only).
        ip_address: Real client IP, forwarded for AUTH's failure audit.

    Returns:
        ``{"user_id", "username", "account_level"}`` on success; ``None`` when AUTH rejected the
        credentials (wrong password, locked, or admission pending).

    Raises:
        AccountAuthUnavailable: AUTH is unconfigured, unreachable, or returned a malformed body.
    """
    base, token = _config()
    if not base or not token:
        raise AccountAuthUnavailable("AUTH delegation is not configured")
    try:
        async with httpx.AsyncClient(
            base_url=base,
            headers={"Authorization": f"Bearer {token}"},
            timeout=DEFAULT_TIMEOUT_S,
        ) as client:
            resp = await client.post(
                "/api/v1/auth/internal/verify-password",
                json={
                    "account": account,
                    "password": password,
                    "ip_address": ip_address,
                },
            )
    except httpx.HTTPError as exc:
        raise AccountAuthUnavailable(f"AUTH request failed: {exc}") from exc

    if resp.status_code in (401, 403, 423):
        # 401 wrong credentials, 403 admission not allowed, 423 locked: all "not this person"
        return None
    if resp.status_code >= 400:
        raise AccountAuthUnavailable(
            f"AUTH returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise AccountAuthUnavailable("AUTH returned non-JSON response") from exc
    if not isinstance(data, dict) or "user_id" not in data:
        raise AccountAuthUnavailable("AUTH returned an unexpected payload")
    return data
