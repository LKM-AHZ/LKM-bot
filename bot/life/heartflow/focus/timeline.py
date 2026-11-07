"""Session timeline with turn compression.

The timeline is the heart-flow's working memory of one conversation: consecutive
user messages form a turn, an idle gap or a bot reply closes it, and old turns
are folded into a compact digest so a long session stays inside a fixed budget.

Everything here is pure and clock-free: callers pass timestamps and serialized
data, so the same code runs in the runtime, in tests and in the migration tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ROLE_USER = "user"
ROLE_BOT = "bot"

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUS_COMPRESSED = "compressed"

_DIGEST_HEADER = "更早的对话摘要（压缩而来，可能不完整，别当原文引用）："
_DIGEST_OMITTED = "…（更早的内容已省略）"


@dataclass(slots=True)
class FocusMessage:
    """One message inside a turn."""

    role: str
    text: str
    ts: float = 0.0
    sender_name: str | None = None
    sender_id: str | None = None
    message_id: str = ""

    def line(self) -> str:
        """Return the digest form of this message.

        Returns:
            ``speaker: text`` with a bot prefix for own messages.
        """
        speaker = self.sender_name or ("我" if self.role == ROLE_BOT else "某人")
        return f"{speaker}: {self.text.strip()}"

    def to_dict(self) -> dict:
        """Return the serializable form used by checkpoints.

        Returns:
            Plain dictionary.
        """
        return {
            "role": self.role,
            "text": self.text,
            "ts": self.ts,
            "sender_name": self.sender_name,
            "sender_id": self.sender_id,
            "message_id": self.message_id,
        }

    @classmethod
    def from_dict(cls, data: object) -> FocusMessage | None:
        """Rebuild a message from a checkpoint payload.

        Args:
            data: Mapping produced by :meth:`to_dict`.

        Returns:
            Message, or None when the payload is not usable.
        """
        if not isinstance(data, dict):
            return None
        text = str(data.get("text") or "")
        if not text:
            return None
        return cls(
            role=str(data.get("role") or ROLE_USER),
            text=text,
            ts=float(data.get("ts") or 0.0),
            sender_name=data.get("sender_name"),
            sender_id=data.get("sender_id"),
            message_id=str(data.get("message_id") or ""),
        )


@dataclass(slots=True)
class FocusTurn:
    """One conversational turn: what one or more people said, then the reply."""

    index: int
    started_ts: float
    ended_ts: float = 0.0
    status: str = STATUS_OPEN
    messages: list[FocusMessage] = field(default_factory=list)
    summary: str = ""

    @property
    def open(self) -> bool:
        """Return whether the turn is still collecting messages."""
        return self.status == STATUS_OPEN

    def text(self, max_chars: int) -> str:
        """Return the flattened text of the turn.

        Args:
            max_chars: Cap of the returned text.

        Returns:
            Messages joined by `` / ``, truncated with an ellipsis.
        """
        joined = " / ".join(message.line() for message in self.messages)
        if len(joined) <= max_chars:
            return joined
        return f"{joined[:max_chars]}…"

    def to_dict(self) -> dict:
        """Return the serializable form used by checkpoints.

        Returns:
            Plain dictionary.
        """
        return {
            "index": self.index,
            "started_ts": self.started_ts,
            "ended_ts": self.ended_ts,
            "status": self.status,
            "summary": self.summary,
            "messages": [message.to_dict() for message in self.messages],
        }

    @classmethod
    def from_dict(cls, data: object) -> FocusTurn | None:
        """Rebuild a turn from a checkpoint payload.

        Args:
            data: Mapping produced by :meth:`to_dict`.

        Returns:
            Turn, or None when the payload has no usable index.
        """
        if not isinstance(data, dict):
            return None
        try:
            index = int(data.get("index") or 0)
        except (TypeError, ValueError):
            return None
        messages = [
            message
            for message in (
                FocusMessage.from_dict(item) for item in data.get("messages") or []
            )
            if message is not None
        ]
        return cls(
            index=index,
            started_ts=float(data.get("started_ts") or 0.0),
            ended_ts=float(data.get("ended_ts") or 0.0),
            status=str(data.get("status") or STATUS_CLOSED),
            messages=messages,
            summary=str(data.get("summary") or ""),
        )


@dataclass(slots=True)
class FocusTimeline:
    """Bounded timeline of one session."""

    session_key: str
    turns: list[FocusTurn] = field(default_factory=list)
    digest: str = ""
    next_index: int = 1
    keep_turns: int = 12
    turn_max_chars: int = 400
    digest_max_chars: int = 1500
    block_max_chars: int = 1200
    total_messages: int = 0
    compressed_turns: int = 0

    @property
    def open_turn(self) -> FocusTurn | None:
        """Return the turn currently collecting messages, if any."""
        if self.turns and self.turns[-1].status == STATUS_OPEN:
            return self.turns[-1]
        return None

    def add_message(
        self,
        message: FocusMessage,
        *,
        gap_seconds: float = 300.0,
    ) -> FocusTurn:
        """Append a message, closing the previous turn when the gap is too big.

        Args:
            message: Message to record.
            gap_seconds: Idle gap above which a new turn starts.

        Returns:
            Turn that received the message.
        """
        turn = self.open_turn
        if turn is not None and message.ts - self._last_ts(turn) > gap_seconds:
            self.close_turn(self._last_ts(turn))
            turn = None
        if turn is None:
            turn = FocusTurn(index=self.next_index, started_ts=message.ts)
            self.next_index += 1
            self.turns.append(turn)
        turn.messages.append(message)
        self.total_messages += 1
        return turn

    def add_reply(self, message: FocusMessage) -> FocusTurn:
        """Append a bot reply to the open turn and close it.

        A reply never starts a new turn on its own: it belongs to whatever was
        being said, even after a long silence.

        Args:
            message: Reply message.

        Returns:
            Turn that received the reply and was closed.
        """
        turn = self.open_turn
        if turn is None:
            turn = FocusTurn(index=self.next_index, started_ts=message.ts)
            self.next_index += 1
            self.turns.append(turn)
        turn.messages.append(message)
        self.total_messages += 1
        self.close_turn(message.ts)
        return turn

    def close_turn(self, ended_ts: float | None = None) -> FocusTurn | None:
        """Close the open turn.

        Args:
            ended_ts: Closing timestamp; defaults to the last message timestamp.

        Returns:
            The closed turn, or None when no turn was open.
        """
        turn = self.open_turn
        if turn is None:
            return None
        turn.status = STATUS_CLOSED
        turn.ended_ts = ended_ts if ended_ts is not None else self._last_ts(turn)
        return turn

    def compress(self, keep_turns: int | None = None) -> list[FocusTurn]:
        """Fold the oldest turns into the digest.

        The open turn is never folded: it is the conversation in progress. A
        turn that is folded keeps its summary in memory and drops its messages,
        which is what bounds the timeline.

        Args:
            keep_turns: Turns to keep verbatim; defaults to the instance value.

        Returns:
            Turns folded by this call, in chronological order; empty when the
            timeline still fits.
        """
        keep = max(int(keep_turns if keep_turns is not None else self.keep_turns), 1)
        # The open turn is always the last one, and keep >= 1 keeps it out of
        # the fold, so the slice below never contains it.
        foldable = len(self.turns) - keep
        if foldable <= 0:
            return []
        folded = self.turns[:foldable]
        lines = [self._summary_line(turn) for turn in folded]
        for turn in folded:
            turn.status = STATUS_COMPRESSED
            turn.summary = self._summary_line(turn)
            turn.messages = []
        self.digest = self._merge_digest(lines)
        self.compressed_turns += len(folded)
        self.turns = self.turns[foldable:]
        return folded

    def render_digest_block(self) -> str | None:
        """Return the prompt block for the folded digest.

        Returns:
            ``<life_focus>`` block, or None when nothing was folded yet.
        """
        if not self.digest:
            return None
        body = self.digest[: self.block_max_chars]
        return f"\n\n<life_focus>\n{_DIGEST_HEADER}\n{body}\n</life_focus>"

    def checkpoint(self) -> dict:
        """Return the serializable state of this timeline.

        Returns:
            Plain dictionary that :meth:`from_checkpoint` restores.
        """
        return {
            "session_key": self.session_key,
            "digest": self.digest,
            "next_index": self.next_index,
            "total_messages": self.total_messages,
            "compressed_turns": self.compressed_turns,
            "turns": [turn.to_dict() for turn in self.turns],
        }

    @classmethod
    def from_checkpoint(
        cls,
        data: object,
        *,
        keep_turns: int = 12,
        turn_max_chars: int = 400,
        digest_max_chars: int = 1500,
        block_max_chars: int = 1200,
    ) -> FocusTimeline | None:
        """Restore a timeline from a checkpoint payload.

        Args:
            data: Mapping produced by :meth:`checkpoint`.
            keep_turns: Turns to keep verbatim.
            turn_max_chars: Cap of one folded turn.
            digest_max_chars: Cap of the digest.
            block_max_chars: Cap of the injected block.

        Returns:
            Restored timeline, or None when the payload is unusable.
        """
        if not isinstance(data, dict):
            return None
        session_key = str(data.get("session_key") or "")
        if not session_key:
            return None
        turns = [
            turn
            for turn in (FocusTurn.from_dict(item) for item in data.get("turns") or [])
            if turn is not None
        ]
        next_index = int(data.get("next_index") or 0) or (
            max((turn.index for turn in turns), default=0) + 1
        )
        return cls(
            session_key=session_key,
            turns=turns,
            digest=str(data.get("digest") or ""),
            next_index=next_index,
            keep_turns=keep_turns,
            turn_max_chars=turn_max_chars,
            digest_max_chars=digest_max_chars,
            block_max_chars=block_max_chars,
            total_messages=int(data.get("total_messages") or 0),
            compressed_turns=int(data.get("compressed_turns") or 0),
        )

    def _summary_line(self, turn: FocusTurn) -> str:
        """Return the digest line of one turn.

        Args:
            turn: Turn to summarize.

        Returns:
            Single line capped at ``turn_max_chars``.
        """
        if turn.summary:
            return turn.summary
        return turn.text(self.turn_max_chars)

    def _merge_digest(self, lines: list[str]) -> str:
        """Append folded lines to the digest and drop the oldest overflow.

        Args:
            lines: New digest lines, oldest first.

        Returns:
            Digest capped at ``digest_max_chars``, oldest lines dropped first.
        """
        merged = [line for line in self.digest.split("\n") if line.strip()]
        merged.extend(line for line in lines if line.strip())
        dropped = 0
        while len(merged) > 1 and sum(len(item) + 1 for item in merged) > (
            self.digest_max_chars
        ):
            merged.pop(0)
            dropped += 1
        text = "\n".join(merged)
        if len(text) > self.digest_max_chars:
            text = text[-self.digest_max_chars :]
        if dropped:
            text = f"{_DIGEST_OMITTED}\n{text}"
        return text

    @staticmethod
    def _last_ts(turn: FocusTurn) -> float:
        """Return the timestamp of the last message of a turn.

        Args:
            turn: Turn to inspect.

        Returns:
            Timestamp, or the turn start when it has no message.
        """
        if not turn.messages:
            return turn.started_ts
        return turn.messages[-1].ts
