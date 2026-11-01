"""Tests for dashboard password helpers."""

from bot.core.utils.auth_password import (
    DEFAULT_DASHBOARD_PASSWORD,
    LEGACY_DEFAULT_DASHBOARD_PASSWORDS,
    hash_md5_dashboard_password,
    is_default_dashboard_password,
)


def test_current_default_password_is_flagged_as_default() -> None:
    """A config still using the current built-in default must be flagged."""
    stored = hash_md5_dashboard_password(DEFAULT_DASHBOARD_PASSWORD)

    assert is_default_dashboard_password(stored)


def test_legacy_default_password_is_still_flagged_as_default() -> None:
    """A config still using a pre-rename default must stay flagged after upgrade."""
    for legacy_password in LEGACY_DEFAULT_DASHBOARD_PASSWORDS:
        stored = hash_md5_dashboard_password(legacy_password)

        assert is_default_dashboard_password(stored)


def test_custom_password_is_not_flagged_as_default() -> None:
    """A changed password must never be reported as the built-in default."""
    stored = hash_md5_dashboard_password("P@ssw0rd-changed-1")

    assert not is_default_dashboard_password(stored)
