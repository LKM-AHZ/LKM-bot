"""Name normalization used by the person layer.

Group members rename themselves, add decorations such as ``（管理）``, use
full-width punctuation and zero-width characters, or appear under a nickname in
one group and a real name in another. Everything that decides "is this the same
person" goes through these helpers so the store and the tools agree.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_DECORATION = re.compile(r"[（(\[【〔][^）)\]】〕]{1,12}[）)\]】〕]")
_TRIM = re.compile(r"^[\s~～*·•\-_=+]+|[\s~～*·•\-_=+]+$")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")


def clean_name(name: str | None) -> str:
    """Return a display-ready name.

    Args:
        name: Raw sender or group card name.

    Returns:
        Name without decorations, zero-width characters or surrounding noise, or
        an empty string when nothing usable remains.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", str(name))
    text = _ZERO_WIDTH.sub("", text)
    text = _DECORATION.sub("", text)
    text = _TRIM.sub("", text)
    return " ".join(text.split())


def normalize_name(name: str | None) -> str:
    """Return the comparison form of a name.

    Args:
        name: Raw name.

    Returns:
        Lowercased, whitespace-free form used for alias matching.
    """
    return clean_name(name).casefold().replace(" ", "")


def derive_person_id(platform_id: str, sender_id: str | None, name: str = "") -> str:
    """Build a stable person id.

    Args:
        platform_id: Platform the message came from.
        sender_id: Platform-scoped user id, when available.
        name: Cleaned display name used when no id exists.

    Returns:
        ``<platform>:<sender_id>`` when an id exists, otherwise a short hash of
        the normalized name.
    """
    platform = (platform_id or "unknown").strip()
    if sender_id:
        return f"{platform}:{sender_id}"
    digest = hashlib.sha1(normalize_name(name).encode("utf-8")).hexdigest()[:16]
    return f"{platform}:name:{digest}"


def name_tokens(text: str) -> set[str]:
    """Return comparison tokens of a fact or name.

    Args:
        text: Fact content or name.

    Returns:
        Token set used for conflict detection.
    """
    normalized = normalize_name(text)
    tokens = {
        normalized[index : index + 2] for index in range(max(len(normalized) - 1, 1))
    }
    return {token for token in tokens if token}
