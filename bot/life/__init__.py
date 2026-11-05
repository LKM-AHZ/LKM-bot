"""Life layer package.

The package hosts the sidecar runtime that migrates MaiBot capabilities into
LKMBot. It is disabled by default and must not change upstream behavior when
``LifeConfig.enabled`` is false.

Submodules are imported explicitly on purpose: the Life layer is optional and a
top-level import of the runtime would pull in the database and platform stack
during package discovery.
"""

from __future__ import annotations

__all__: list[str] = []
