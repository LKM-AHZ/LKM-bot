"""Bounded record of planner decisions and delivered replies.

Records never carry the message text: a decision keeps the gate numbers and the
reason string, a delivery keeps length, part count and a capped reasoning
excerpt. That is enough for the panel to explain a turn without turning the ring
into a chat log.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from bot.life.config import MonitorConfig


@dataclass(slots=True)
class PlannerRecord:
    """One observed planner moment."""

    kind: str
    """``decision`` (heart-flow gate) or ``delivery`` (outbound reply)."""

    session_id: str
    created_at: float = field(default_factory=time.time)
    action: str = ""
    reply: bool = False
    likelihood: float = 0.0
    threshold: float = 0.0
    reason: str = ""
    chars: int = 0
    parts: int = 0
    reasoning: str = ""

    def to_dict(self) -> dict:
        """Return a serializable view.

        Returns:
            Record fields rounded for the panel.
        """
        return {
            "kind": self.kind,
            "session_id": self.session_id,
            "created_at": round(self.created_at, 3),
            "action": self.action,
            "reply": self.reply,
            "likelihood": round(self.likelihood, 3),
            "threshold": round(self.threshold, 3),
            "reason": self.reason,
            "chars": self.chars,
            "parts": self.parts,
            "reasoning": self.reasoning,
        }


class PlannerMonitor:
    """Ring buffer plus counters for planner decisions and deliveries."""

    def __init__(self, config: MonitorConfig) -> None:
        """Initialize an empty monitor.

        Args:
            config: Ring size and reasoning capture settings.
        """
        self.config = config
        self._records: list[PlannerRecord] = []
        self._counters = {
            "decisions": 0,
            "replies": 0,
            "waits": 0,
            "deliveries": 0,
            "dropped": 0,
        }

    def record_decision(
        self,
        *,
        session_id: str,
        action: str,
        reply: bool,
        likelihood: float,
        threshold: float,
        reason: str,
        now: float | None = None,
    ) -> None:
        """Record one heart-flow decision.

        Args:
            session_id: Unified message origin of the session.
            action: ``continue`` or ``hold``.
            reply: Whether the gate decided to answer.
            likelihood: Reply likelihood from the gate.
            threshold: Threshold in force for this evaluation.
            reason: Gate reason string.
            now: Wall-clock timestamp; tests inject one.
        """
        self._counters["decisions"] += 1
        if reply:
            self._counters["replies"] += 1
        else:
            self._counters["waits"] += 1
        self._append(
            PlannerRecord(
                kind="decision",
                session_id=session_id,
                created_at=now if now is not None else time.time(),
                action=action,
                reply=reply,
                likelihood=likelihood,
                threshold=threshold,
                reason=reason,
            )
        )

    def record_delivery(self, event: Any, result: Any) -> None:
        """Record one outbound reply, including a capped reasoning excerpt.

        Args:
            event: Bot message event being answered.
            result: Message result about to be sent.
        """
        text = ""
        getter = getattr(result, "get_plain_text", None)
        if callable(getter):
            try:
                text = getter() or ""
            except Exception:
                text = ""
        parts = len(getattr(result, "chain", []) or [])
        reasoning = ""
        if self.config.capture_reasoning and self.config.reasoning_max_chars > 0:
            raw = ""
            get_extra = getattr(event, "get_extra", None)
            if callable(get_extra):
                raw = get_extra("_llm_reasoning_content", "") or ""
            reasoning = str(raw).strip()[: self.config.reasoning_max_chars]
        self._counters["deliveries"] += 1
        self._append(
            PlannerRecord(
                kind="delivery",
                session_id=str(getattr(event, "unified_msg_origin", "") or ""),
                action="reply",
                reply=True,
                chars=len(text),
                parts=parts,
                reasoning=reasoning,
            )
        )

    def recent(self, session_id: str = "", limit: int = 0) -> list[dict]:
        """Return records newest first, optionally filtered by session.

        Args:
            session_id: Only return records of this session when non-empty.
            limit: Maximum records; 0 returns the whole ring.

        Returns:
            Serialized records.
        """
        records = [
            record
            for record in self._records
            if not session_id or record.session_id == session_id
        ]
        if limit > 0:
            records = records[-limit:]
        return [record.to_dict() for record in reversed(records)]

    def status(self) -> dict:
        """Return counters and ring size for the status endpoint.

        Returns:
            Monitor counters and configuration bounds.
        """
        return {
            "enabled": True,
            "counters": dict(self._counters),
            "records": len(self._records),
            "recent_size": self.config.recent_size,
            "capture_reasoning": self.config.capture_reasoning,
            "last": self._records[-1].to_dict() if self._records else None,
        }

    def _append(self, record: PlannerRecord) -> None:
        """Append one record and drop the oldest beyond the ring size.

        Args:
            record: Record to append.
        """
        self._records.append(record)
        limit = max(1, self.config.recent_size)
        if len(self._records) > limit:
            del self._records[: len(self._records) - limit]
            self._counters["dropped"] += 1
