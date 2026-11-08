"""Structured reply supplements: ``@``, pictures and stickers around one reply.

MaiBot 1.4 lets the planner describe the *shape* of an answer as a small plan
(``reply`` / ``at`` / ``pic`` / ``emoji``) instead of dumping everything into a
single text. The plan only references resources the model has already seen, so
the executor can validate every id before anything is sent.

This module is the LKMBot port of that contract (S5e). It is deliberately
transport-agnostic:

* :class:`ReplySupplementPlan` is the model-facing schema.
* :class:`SupplementResources` captures the messages, pictures and context
  stickers of one turn.
* :func:`resolve_supplement` validates the plan against those resources and
  turns it into :class:`SupplementPart` objects.
* :func:`compose_supplement` folds the parts around the reply body.
* :func:`generate_reply_supplement` asks the chat provider for a plan and
  retries once, mirroring upstream's error-retry path.

Delivery lives in :mod:`bot.life.reply.postprocess`; nothing here sends.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bot.core.message.components import At, Image, Plain, Reply
from bot.core.message.message_event_result import MessageChain

_FENCE = re.compile(
    r"\A```(?:json)?[ \t]*\r?\n(.*?)\r?\n```[ \t]*\Z",
    re.IGNORECASE | re.DOTALL,
)

_MAX_PLAN_PARTS = 16
"""Hard ceiling from upstream; the config may lower it further."""


class SupplementNode(BaseModel):
    """Base class: strict, so a typo in the model output fails loudly."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ReplyNode(SupplementNode):
    """Placeholder marking where the reply body goes."""

    type: Literal["reply"]


class AtNode(SupplementNode):
    """Mention a participant the planner saw."""

    type: Literal["at"]
    msg_id: str = Field(min_length=1)


class PictureNode(SupplementNode):
    """Reuse one picture from a message (never fetch a new one)."""

    type: Literal["pic"]
    source: str = Field(min_length=1)
    index: int = Field(ge=0)


class EmojiNode(SupplementNode):
    """Send a sticker.

    Either reference a sticker that appeared in the conversation
    (``source`` + optional ``index``) or describe the mood and let the local
    sticker library pick one (``emotion`` / ``content`` / ``description``).
    The two forms are mutually exclusive.
    """

    type: Literal["emoji"]
    index: int | None = Field(default=None, ge=0)
    source: str | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, min_length=1)
    emotion: str | None = Field(default=None, min_length=1)
    content: str | None = Field(default=None, min_length=1)

    @property
    def is_semantic(self) -> bool:
        """Return whether the sticker must be picked by description."""
        return any(
            value is not None
            for value in (self.description, self.emotion, self.content)
        )

    @model_validator(mode="after")
    def validate_reference(self) -> EmojiNode:
        """Reject mixed or empty sticker references.

        Returns:
            The validated node.

        Raises:
            ValueError: When the reference mixes both forms or is empty.
        """
        if self.is_semantic:
            if self.source is not None or self.index is not None:
                raise ValueError("semantic emoji must not carry source or index")
            if self.description is not None and (
                self.emotion is not None or self.content is not None
            ):
                raise ValueError(
                    "description is legacy and cannot mix with emotion/content"
                )
            if any(
                value is not None and not value.strip()
                for value in (self.description, self.emotion, self.content)
            ):
                raise ValueError("semantic emoji query must not be blank")
        elif self.source is None or not self.source.strip():
            raise ValueError("context emoji requires a message source")
        return self


SupplementPartNode = ReplyNode | AtNode | PictureNode | EmojiNode


class ReplySupplementPlan(SupplementNode):
    """One send plan; it can never carry or rewrite the reply text."""

    msg_id: str | None = Field(default=None, min_length=1)
    """Message to quote; the executor resolves it against the resources."""

    parts: list[SupplementPartNode] = Field(
        default_factory=list, max_length=_MAX_PLAN_PARTS
    )

    @model_validator(mode="after")
    def require_one_reply(self) -> ReplySupplementPlan:
        """Require exactly one ``reply`` placeholder.

        Returns:
            The validated plan.

        Raises:
            ValueError: When the plan contains zero or several placeholders.
        """
        if sum(isinstance(part, ReplyNode) for part in self.parts) != 1:
            raise ValueError("plan must contain exactly one reply placeholder")
        return self


def parse_supplement_plan(response: str) -> ReplySupplementPlan:
    """Parse one model response into a validated plan.

    Args:
        response: Raw completion text, optionally wrapped in a JSON fence.

    Returns:
        The validated plan.

    Raises:
        ValueError: When the payload is not JSON or fails validation.
    """
    text = (response or "").strip()
    match = _FENCE.fullmatch(text)
    if match:
        text = match.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"supplement plan is not valid JSON: {exc.msg}") from exc
    return ReplySupplementPlan.model_validate(payload)


@dataclass(slots=True)
class SupplementMessage:
    """One message the planner may reference."""

    message_id: str
    sender_id: str | None = None
    sender_name: str | None = None
    plain_text: str = ""
    pictures: list[str] = field(default_factory=list)
    """File references of pictures carried by the message, in display order."""

    emojis: list[str] = field(default_factory=list)
    """File references of stickers carried by the message, in display order."""


def message_from_row(row: dict[str, Any]) -> SupplementMessage:
    """Build a referenceable message from one archive row.

    Image components are split into pictures and stickers by their ``sub_type``
    marker; only components that carry a usable local reference are kept.

    Args:
        row: Row payload with ``message_id``, ``components`` and friends.

    Returns:
        Message with pictures and stickers extracted from its components.
    """
    pictures: list[str] = []
    emojis: list[str] = []
    for component in row.get("components") or []:
        if not isinstance(component, dict) or component.get("type") != "image":
            continue
        data = component.get("data")
        if not isinstance(data, dict):
            continue
        reference = next(
            (
                str(data[key]).strip()
                for key in ("path", "file", "url")
                if isinstance(data.get(key), str) and str(data[key]).strip()
            ),
            "",
        )
        if not reference:
            continue
        if str(data.get("sub_type") or "") == "emoji":
            emojis.append(reference)
        else:
            pictures.append(reference)
    return SupplementMessage(
        message_id=str(row.get("message_id") or ""),
        sender_id=row.get("sender_id"),
        sender_name=row.get("sender_name"),
        plain_text=str(row.get("plain_text") or ""),
        pictures=pictures,
        emojis=emojis,
    )


@dataclass(slots=True)
class SupplementResources:
    """Everything one plan is allowed to reference."""

    messages: dict[str, SupplementMessage] = field(default_factory=dict)
    allow_at: bool = True
    allow_quote: bool = True
    session_key: str = ""

    @classmethod
    def from_rows(
        cls,
        rows: list[dict[str, Any]],
        *,
        allow_at: bool = True,
        allow_quote: bool = True,
        session_key: str = "",
    ) -> SupplementResources:
        """Capture resources from archived rows.

        Args:
            rows: Archive payloads of the current turn.
            allow_at: Whether the scene permits mentions.
            allow_quote: Whether the scene permits quoting.
            session_key: Session owning the resources.

        Returns:
            Resources keyed by message id.
        """
        messages: dict[str, SupplementMessage] = {}
        for row in rows:
            message = message_from_row(row)
            if message.message_id:
                messages[message.message_id] = message
        return cls(
            messages=messages,
            allow_at=allow_at,
            allow_quote=allow_quote,
            session_key=session_key,
        )

    def resolve_id(self, value: str, candidates: dict[str, Any]) -> str:
        """Resolve one reference, rejecting unknown or ambiguous ids.

        Args:
            value: Id from the plan.
            candidates: Mapping of known ids.

        Returns:
            The matching key.

        Raises:
            ValueError: When the reference is unknown or matches several ids.
        """
        text = (value or "").strip()
        if text in candidates:
            return text
        matches = [
            key
            for key in candidates
            if key.endswith(text) or text.endswith(key) or key.lstrip("0") == text
        ]
        if len(matches) != 1:
            raise ValueError(f"unknown or ambiguous supplement reference: {value}")
        return matches[0]

    def inventory(self, max_messages: int = 24) -> dict[str, Any]:
        """Return the resource summary handed to the model.

        Args:
            max_messages: Only the newest messages are listed.

        Returns:
            JSON-ready inventory with ids, senders and attachment counts.
        """
        return {
            "messages": [
                {
                    "msg_id": message.message_id,
                    "sender": message.sender_name or message.sender_id or "",
                    "text": message.plain_text[:120],
                    "pictures": len(message.pictures),
                    "emojis": len(message.emojis),
                }
                for message in list(self.messages.values())[-max_messages:]
            ],
            "allow_at": self.allow_at,
            "allow_quote": self.allow_quote,
        }


@dataclass(slots=True)
class SupplementPart:
    """One resolved action in send order."""

    kind: str
    """``reply`` / ``at`` / ``pic`` / ``emoji``."""

    payload: Any = None
    """``At`` component, or the local reference of a picture/sticker."""


@dataclass(slots=True)
class ResolvedSupplement:
    """A validated plan with every reference already resolved."""

    quote_id: str | None = None
    parts: list[SupplementPart] = field(default_factory=list)

    def to_extra(self) -> dict[str, Any]:
        """Serialize the plan so it can travel on a pipeline event.

        Returns:
            JSON-safe payload accepted by :meth:`from_extra`.
        """
        parts: list[dict[str, Any]] = []
        for part in self.parts:
            if part.kind == "at":
                parts.append(
                    {
                        "kind": "at",
                        "qq": str(getattr(part.payload, "qq", "")),
                        "name": str(getattr(part.payload, "name", "") or ""),
                    }
                )
            else:
                parts.append({"kind": part.kind, "path": str(part.payload or "")})
        return {"quote_id": self.quote_id, "parts": parts}

    @classmethod
    def from_extra(cls, payload: Any) -> ResolvedSupplement | None:
        """Rebuild a plan from an event extra.

        Args:
            payload: Value stored under ``_life_reply_supplement``.

        Returns:
            The resolved plan, or None when the payload is unusable.
        """
        if not isinstance(payload, dict):
            return None
        parts: list[SupplementPart] = []
        for item in payload.get("parts") or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            if kind == "reply":
                parts.append(SupplementPart("reply"))
            elif kind == "at":
                parts.append(
                    SupplementPart(
                        "at",
                        At(
                            qq=str(item.get("qq") or ""),
                            name=str(item.get("name") or ""),
                        ),
                    )
                )
            elif kind in {"pic", "emoji"} and item.get("path"):
                parts.append(SupplementPart(kind, str(item["path"])))
        quote_id = payload.get("quote_id")
        return cls(
            quote_id=str(quote_id) if quote_id else None,
            parts=parts,
        )


async def resolve_supplement(
    plan: ReplySupplementPlan,
    resources: SupplementResources,
    *,
    pick_emoji: Any = None,
    emoji_context: str = "",
    max_parts: int = 8,
) -> ResolvedSupplement:
    """Validate a plan against the captured resources.

    Args:
        plan: Model output to validate.
        resources: Messages and switches captured for this turn.
        pick_emoji: Async callable ``(session_key, context) -> row|None`` used
            for semantic sticker requests.
        emoji_context: Text used when describing the desired sticker.
        max_parts: Configured ceiling for this deployment.

    Returns:
        Resolved plan ready for composition.

    Raises:
        ValueError: When any reference is unusable.
    """
    if not plan.parts:
        raise ValueError("supplement plan is empty")
    if len(plan.parts) > max_parts:
        raise ValueError(f"supplement plan exceeds {max_parts} parts")
    quote_id = None
    if plan.msg_id is not None:
        if not resources.allow_quote:
            raise ValueError("quoting is disabled for this scene")
        quote_id = resources.resolve_id(plan.msg_id, resources.messages)

    parts: list[SupplementPart] = []
    for node in plan.parts:
        if isinstance(node, ReplyNode):
            parts.append(SupplementPart("reply"))
        elif isinstance(node, AtNode):
            if not resources.allow_at:
                raise ValueError("mentions are disabled for this scene")
            message = resources.messages[
                resources.resolve_id(node.msg_id, resources.messages)
            ]
            parts.append(
                SupplementPart(
                    "at",
                    At(
                        qq=str(message.sender_id or ""),
                        name=str(message.sender_name or ""),
                    ),
                )
            )
        elif isinstance(node, PictureNode):
            message = resources.messages[
                resources.resolve_id(node.source, resources.messages)
            ]
            if node.index >= len(message.pictures):
                raise ValueError(
                    f"picture index out of range: {node.source}:{node.index}"
                )
            parts.append(SupplementPart("pic", message.pictures[node.index]))
        else:
            if not node.is_semantic:
                message = resources.messages[
                    resources.resolve_id(node.source or "", resources.messages)
                ]
                index = node.index or 0
                if index >= len(message.emojis):
                    raise ValueError(
                        f"context sticker index out of range: {node.source}:{index}"
                    )
                parts.append(SupplementPart("emoji", message.emojis[index]))
                continue
            # A semantic request may legitimately resolve to nothing; the reply
            # still goes out, only this sticker is dropped.
            query = " ".join(
                item
                for item in (
                    node.emotion,
                    node.content,
                    node.description,
                    emoji_context,
                )
                if item
            ).strip()
            if pick_emoji is None or not query:
                continue
            try:
                picked = await pick_emoji(resources.session_key, query)
            except Exception:
                picked = None
            path = (picked or {}).get("file_path") if isinstance(picked, dict) else None
            if path:
                parts.append(SupplementPart("emoji", str(path)))
    return ResolvedSupplement(quote_id=quote_id, parts=parts)


def compose_supplement(
    resolved: ResolvedSupplement, body: MessageChain
) -> list[MessageChain]:
    """Fold resolved parts around the reply body.

    Upstream keeps mentions inside the reply message and sends pictures and
    stickers as their own messages in plan order. Metadata of the reply chain
    (T2I, markdown, type) is inherited by every produced chain.

    Args:
        resolved: Validated plan.
        body: The decorated reply chain, used as-is for the ``reply`` slot.

    Returns:
        Message chains in send order; never empty.

    Raises:
        ValueError: When the plan has no ``reply`` part.
    """
    chains: list[MessageChain] = []
    pending_at: list[At] = []
    body_used = False
    for part in resolved.parts:
        if part.kind == "reply":
            components: list[Any] = []
            for mention in pending_at:
                components.extend([mention, Plain(" ")])
            pending_at = []
            if resolved.quote_id and not any(
                isinstance(component, Reply) for component in body.chain
            ):
                components.insert(0, Reply(id=resolved.quote_id))
            chains.append(body.derive([*components, *body.chain]))
            body_used = True
        elif part.kind == "at":
            if body_used and chains:
                chains[-1].chain.extend([Plain(" "), part.payload])
            else:
                pending_at.append(part.payload)
        else:
            chains.append(
                body.derive([*pending_at, Image.fromFileSystem(part.payload)])
            )
            pending_at = []
    if not body_used:
        raise ValueError("supplement plan has no reply placeholder")
    if pending_at:
        chains.append(body.derive(list(pending_at)))
    return chains


_PROMPT = """你要为一次回复编排发送顺序。只输出 JSON 对象，不要解释。

可用资源（只能引用这里的 msg_id 与数量）：
{inventory}

正文你已经写好了，这里只决定 @ 谁、引用哪条消息、补发哪些图片或表情、以及顺序。
字段规则：
- parts：1~{max_parts} 个动作，必须且只能有一个 "reply" 占位符。
- "at"：{{"type":"at","msg_id":"<消息 id>"}}，只能 @ 上面出现的发言人。
- "pic"：{{"type":"pic","source":"<消息 id>","index":0}}，序号从 0 开始。
- "emoji"：引用上下文表情用 {{"type":"emoji","source":"<消息 id>","index":0}}；
  按情绪挑表情库用 {{"type":"emoji","emotion":"开心","content":"可选补充"}}。
- 引用消息用顶层 "msg_id"。

{guidance}
"""


async def generate_reply_supplement(
    provider: Any,
    resources: SupplementResources,
    *,
    guidance: str = "",
    timeout_seconds: float = 8.0,
    max_parts: int = 8,
    retry_once: bool = True,
) -> ReplySupplementPlan | None:
    """Ask the chat provider for one plan; retry once on failure.

    Args:
        provider: Chat provider exposing ``text_chat``.
        resources: Resources the plan may reference.
        guidance: Extra instructions (reply tone, scene limits).
        timeout_seconds: Hard timeout for one call.
        max_parts: Ceiling handed to the model.
        retry_once: Retry with the previous error appended.

    Returns:
        A validated plan, or None when the provider is unavailable or both
        attempts fail.
    """
    if provider is None:
        return None
    prompt = _PROMPT.format(
        inventory=json.dumps(resources.inventory(), ensure_ascii=False),
        max_parts=max_parts,
        guidance=guidance.strip(),
    )
    system_prompt = "你是回复发送编排器。严格遵守给定资源与字段，输出单个 JSON 对象。"
    retry_error = ""
    for _ in range(2 if retry_once else 1):
        question = prompt
        if retry_error:
            question = (
                f"{prompt}\n\n上次输出校验失败：{retry_error}\n请修正后重新输出。"
            )
        try:
            async with asyncio.timeout(max(timeout_seconds, 1.0)):
                response = await provider.text_chat(
                    prompt=question,
                    system_prompt=system_prompt,
                )
            text = (getattr(response, "completion_text", "") or "").strip()
            if not text:
                raise ValueError("provider returned empty content")
            return parse_supplement_plan(text)
        except Exception as exc:  # noqa: BLE001 - the retry path records it
            retry_error = f"{type(exc).__name__}: {exc}"
    return None
