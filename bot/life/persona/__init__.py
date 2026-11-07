"""Persona profile package (S5a).

Profiles are keyed by the upstream ``persona_id`` but stored in the Life
database: the upstream ``Persona`` row is never touched, so a broken profile
can never stop the bot from talking.
"""

from __future__ import annotations

__all__: list[str] = []
