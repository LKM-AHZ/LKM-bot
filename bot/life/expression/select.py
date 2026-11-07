"""Pick a few expressions for the current context.

Selection combines three signals: similarity to the conversation, the learned
weight (decayed by idleness so stale catchphrases fade), and a scene match. The
result is a short prompt block the model may use *or ignore*, never a script it
has to follow.
"""

from __future__ import annotations

import math
import time
from typing import Any, Sequence

from bot.life.config import ExpressionConfig
from bot.life.expression.index import ExpressionIndex
from bot.life.expression.store import ExpressionStore
from bot.life.memory.store import tokenize

_BLOCK_TAG = "life_expression"
_MAX_SITUATION_CHARS = 80


def weight_decay(weight: float, last_used_ts: int, half_life_days: float) -> float:
    """Return the decayed weight of an expression.

    Args:
        weight: Stored weight.
        last_used_ts: Last time the expression was used, or 0 when never.
        half_life_days: Days after which the weight halves.

    Returns:
        Decayed weight.
    """
    if half_life_days <= 0 or not last_used_ts:
        return max(weight, 0.0)
    age_days = max((time.time() - last_used_ts) / 86400.0, 0.0)
    return max(weight, 0.0) * math.pow(0.5, age_days / half_life_days)


class ExpressionSelector:
    """Choose expressions for one reply."""

    def __init__(
        self,
        store: ExpressionStore,
        config: ExpressionConfig,
        index: ExpressionIndex | None = None,
    ) -> None:
        """Initialize the selector.

        Args:
            store: Expression store.
            config: Selection settings.
            index: Optional FAISS index for similarity search.
        """
        self.store = store
        self.config = config
        self.index = index

    async def select(
        self,
        session_key: str,
        context: str,
        scene: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return expressions worth using for a context.

        Args:
            session_key: Session scope.
            context: Recent conversation text.
            scene: ``group`` or ``private``; None detects nothing and keeps all.
            limit: Maximum expressions; defaults to ``config.top_k``.

        Returns:
            Expression rows with ``score`` and ``similarity``, best first.
        """
        top_k = max(int(limit or self.config.top_k), 1)
        rows = await self.store.list_expressions(
            session_key,
            status="active",
            scene=scene,
            limit=max(top_k * 20, 60),
        )
        if not rows:
            return []
        by_id = {int(row["id"]): row for row in rows}
        similarities: dict[int, float] = {}
        if self.index is not None and self.index.available and self.config.use_vector:
            hits = await self.index.search(
                context,
                session_key,
                limit=max(top_k * 5, 10),
            )
            for hit in hits:
                if hit["expression_id"] in by_id:
                    similarities[hit["expression_id"]] = hit["similarity"]
        if not similarities:
            similarities = self._keyword_similarities(rows, context)

        max_weight = max(
            (
                weight_decay(
                    float(row["weight"] or 0.0),
                    int(row["last_used_ts"] or 0),
                    self.config.weight_half_life_days,
                )
                for row in rows
            ),
            default=1.0,
        )
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            expression_id = int(row["id"])
            similarity = max(similarities.get(expression_id, 0.0), 0.0)
            decayed = weight_decay(
                float(row["weight"] or 0.0),
                int(row["last_used_ts"] or 0),
                self.config.weight_half_life_days,
            )
            weight_score = min(decayed / max_weight, 1.0) if max_weight else 0.0
            score = 0.7 * similarity + 0.3 * weight_score
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
        scored.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in scored[:top_k]]

    def _keyword_similarities(
        self,
        rows: Sequence[dict[str, Any]],
        context: str,
    ) -> dict[int, float]:
        """Score expressions by token overlap with the context.

        Args:
            rows: Candidate expression rows.
            context: Recent conversation text.

        Returns:
            Mapping of expression id to similarity in ``[0, 1]``.
        """
        context_tokens = set(tokenize(context))
        if not context_tokens:
            return {}
        scores: dict[int, float] = {}
        for row in rows:
            text = f"{row.get('situation') or ''} {row.get('style') or ''}"
            tokens = set(tokenize(text))
            if not tokens:
                continue
            overlap = len(context_tokens & tokens)
            if overlap:
                scores[int(row["id"])] = overlap / len(context_tokens)
        return scores

    def build_block(self, selected: Sequence[dict[str, Any]]) -> str:
        """Render the prompt block for the selected expressions.

        Args:
            selected: Rows produced by :meth:`select`.

        Returns:
            Prompt block, or an empty string when nothing was selected.
        """
        if not selected:
            return ""
        lines = []
        for row in selected:
            situation = str(row.get("situation") or "").strip()[:_MAX_SITUATION_CHARS]
            style = str(row.get("style") or "").strip()
            if not style:
                continue
            if situation:
                lines.append(f"- 当{situation}时，可以说：{style}")
            else:
                lines.append(f"- 可以说：{style}")
        if not lines:
            return ""
        return (
            f"\n\n<{_BLOCK_TAG}>\n"
            "这些是你在这个群里的表达习惯，合适的时候可以自然地用一点，"
            "不要硬套、不要照抄整个列表、不要解释这份列表。\n"
            + "\n".join(lines)
            + f"\n</{_BLOCK_TAG}>"
        )
