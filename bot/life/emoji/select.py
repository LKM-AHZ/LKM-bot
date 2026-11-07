"""Pick a sticker for a piece of context.

Selection is similarity (vector when available, tags otherwise) plus popularity,
then two rate limits decide whether anything may be sent at all: a per-session
cooldown and a rolling one-hour cap. The limits live in memory on purpose - they
protect against a chatty loop, not against process restarts.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Sequence

import numpy as np

from bot.life.config import EmojiConfig
from bot.life.emoji.store import EmojiStore
from bot.life.memory.store import tokenize, unpack_vector


class SendBudget:
    """Per-session cooldown and hourly cap."""

    def __init__(self, config: EmojiConfig | None = None) -> None:
        """Initialize the budget.

        Args:
            config: Limits; defaults are used when omitted.
        """
        self.config = config or EmojiConfig()
        self._last_sent: dict[str, float] = {}
        self._window: dict[str, deque[float]] = {}

    def can_send(self, session_key: str, now: float | None = None) -> bool:
        """Return whether one more sticker may be sent.

        Args:
            session_key: Session scope.
            now: Current monotonic time; defaults to ``time.monotonic()``.

        Returns:
            True when both the cooldown and the hourly cap allow a send.
        """
        moment = time.monotonic() if now is None else now
        last = self._last_sent.get(session_key)
        if last is not None and moment - last < max(self.config.cooldown_seconds, 0):
            return False
        window = self._window.get(session_key)
        if window is None:
            return True
        cutoff = moment - 3600
        while window and window[0] < cutoff:
            window.popleft()
        return len(window) < max(self.config.max_per_hour, 1)

    def mark_sent(self, session_key: str, now: float | None = None) -> None:
        """Record one send.

        Args:
            session_key: Session scope.
            now: Current monotonic time; defaults to ``time.monotonic()``.
        """
        moment = time.monotonic() if now is None else now
        self._last_sent[session_key] = moment
        self._window.setdefault(session_key, deque()).append(moment)

    def snapshot(self) -> dict[str, int]:
        """Return the tracked session count.

        Returns:
            Mapping with the number of sessions that have sent stickers.
        """
        return {"sessions": len(self._last_sent)}


class EmojiSelector:
    """Choose a sticker for one context."""

    def __init__(
        self,
        store: EmojiStore,
        config: EmojiConfig,
        embedding: Any = None,
    ) -> None:
        """Initialize the selector.

        Args:
            store: Sticker store.
            config: Selection and send settings.
            embedding: Optional embedding client used for vector similarity.
        """
        self.store = store
        self.config = config
        self.embedding = embedding
        self.budget = SendBudget(config)

    async def select(
        self,
        session_key: str,
        context: str,
        require_budget: bool = True,
    ) -> dict[str, Any] | None:
        """Return the best sticker for a context.

        Args:
            session_key: Session scope.
            context: Recent conversation text.
            require_budget: When True, respect the cooldown and hourly cap.

        Returns:
            Sticker row with ``score`` and ``similarity``, or None.
        """
        if require_budget and not self.budget.can_send(session_key):
            return None
        rows = await self.store.list_emojis(session_key, status="active", limit=500)
        if not rows:
            return None
        query_vector = None
        if self.embedding is not None and getattr(self.embedding, "available", False):
            query_vector = await self.embedding.embed_one(context[:200])
        context_tokens = set(tokenize(context))
        recent = int(time.time()) - max(self.config.cooldown_seconds, 0)
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            if int(row.get("last_used_ts") or 0) > recent:
                continue
            similarity = 0.0
            if query_vector and row.get("embedding"):
                similarity = _cosine(unpack_vector(row["embedding"]), query_vector)
            if similarity <= 0.0:
                similarity = self._tag_similarity(row, context_tokens)
            if similarity <= 0.0:
                continue
            popularity = min(float(row.get("usage_count") or 0) / 10.0, 1.0)
            score = 0.8 * similarity + 0.2 * popularity
            if score < self.config.min_score:
                continue
            scored.append(
                (
                    score,
                    {
                        **row,
                        "score": round(score, 4),
                        "similarity": round(similarity, 4),
                    },
                )
            )
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]

    def _tag_similarity(self, row: dict[str, Any], context_tokens: set[str]) -> float:
        """Score a sticker by tag/caption overlap.

        Args:
            row: Sticker row.
            context_tokens: Tokens of the current context.

        Returns:
            Similarity in ``[0, 1]``.
        """
        if not context_tokens:
            return 0.0
        text = " ".join(
            [str(row.get("caption") or "")]
            + [str(tag) for tag in (row.get("tags") or [])]
        )
        tokens = set(tokenize(text))
        if not tokens:
            return 0.0
        overlap = len(tokens & context_tokens)
        if not overlap:
            return 0.0
        return min(overlap / max(len(context_tokens), 1), 1.0)


def _cosine(left: np.ndarray, right: Sequence[float]) -> float:
    """Return the cosine similarity of two vectors.

    Args:
        left: Stored vector.
        right: Query vector.

    Returns:
        Similarity in ``[0, 1]``; 0 when sizes differ or a vector is empty.
    """
    other = np.asarray(right, dtype=np.float32)
    if not left.size or left.size != other.size:
        return 0.0
    denominator = float(np.linalg.norm(left) * np.linalg.norm(other)) or 1e-9
    return max(0.0, min(1.0, float(np.dot(left, other)) / denominator))
