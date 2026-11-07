"""Reply post-processing package (S5c).

The only stage that rewrites the text actually delivered to the user. Every
transform is a plain text function behind an explicit switch, so a bad one can
be turned off without touching the pipeline.
"""

from __future__ import annotations

__all__: list[str] = []
