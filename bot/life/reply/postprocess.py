"""Reply post-processing (S5c + S5d).

This is the first place where the Life layer changes what actually leaves the
bot. The transforms are ordered on purpose:

1. strip Markdown (unless the profile allows it);
2. nudge a typo, at most one per reply, only between Chinese characters;
3. cap the length at the profile budget, cutting on a sentence boundary;
4. split into several messages by probability - but only when the upstream
   segmented-reply setting is off, otherwise the two splitters would fight;
5. quote the triggering message by probability, on platforms that support it.

None of this invents content: every step only removes characters, swaps a pair,
cuts a tail or moves the same text into another message. The recent ring keeps
the last replies' sizes and flags so S5d can check the distribution without
storing text.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections import deque
from typing import Any

from bot import logger
from bot.core.message.components import Plain, Reply
from bot.life.config import LifeConfig, ReplyConfig
from bot.life.scenes import policy_for as scene_policy_for
from bot.life.scenes import quote_rate as scale_quote_rate

_SENTENCE_END = "。！？!?…~"
_CHINESE = re.compile(r"[\u4e00-\u9fff]")
_MARKDOWN = (
    re.compile(r"^\s*#{1,6}\s", re.M),
    re.compile(r"\*\*"),
    re.compile(r"```"),
    re.compile(r"^\s*\|.*\|\s*$", re.M),
    re.compile(r"\[[^\]]+\]\([^)]+\)"),
)


class ReplyPostProcessor:
    """Rewrite outbound replies according to the session persona profile."""

    def __init__(
        self,
        config: ReplyConfig,
        life_config: LifeConfig | None = None,
        persona: Any = None,
        rng: random.Random | None = None,
        creative: Any = None,
        translator: Any = None,
        scenes: Any = None,
    ) -> None:
        """Initialize the processor.

        Args:
            config: Switches, platform list and split parameters.
            life_config: Root Life configuration used for the allowlist.
            persona: Owner of the per-persona style profile, or None.
            rng: Random source; tests pass a seeded instance.
            creative: Super creative mode config, or None.
            translator: Async callable rewriting one reply body back to Chinese.
            scenes: Scene policy config, or None.
        """
        self.config = config
        self.life_config = life_config
        self.persona = persona
        self.creative = creative
        self.translator = translator
        self.scenes = scenes
        self.rng = rng or random.Random()
        self._tasks: set[asyncio.Task] = set()
        self._recent: deque[dict[str, Any]] = deque(maxlen=50)
        self._counters = {
            "processed": 0,
            "markdown_stripped": 0,
            "typos": 0,
            "capped": 0,
            "split": 0,
            "quoted": 0,
            "supplemented": 0,
            "translated": 0,
            "skipped": 0,
        }

    def session_enabled(self, umo: str, group_id: str | None = None) -> bool:
        """Return whether a session is inside the Life allowlist.

        Args:
            umo: Session key.
            group_id: Group id, or None.

        Returns:
            True when the session may be post-processed.
        """
        if self.life_config is None:
            return True
        return bool(self.life_config.session_enabled(umo, group_id))

    def strip_markdown(self, text: str) -> str:
        """Turn Markdown into plain sentences.

        Args:
            text: Reply text as produced by the model.

        Returns:
            The same text without fences, emphasis, headings, bullets or tables.
        """
        result = re.sub(r"```[a-zA-Z0-9_-]*\n?(.*?)```", r"\1", text, flags=re.S)
        result = re.sub(r"`([^`\n]+)`", r"\1", result)
        result = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", result)
        result = re.sub(r"^\s*#{1,6}\s*", "", result, flags=re.M)
        result = re.sub(r"\*\*([^*]+)\*\*", r"\1", result)
        result = re.sub(r"__([^_]+)__", r"\1", result)
        result = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", result)
        result = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", result)
        result = re.sub(r"^\s*[-*+]\s+", "", result, flags=re.M)
        result = re.sub(r"^\s*\d+[.)]\s+", "", result, flags=re.M)
        result = re.sub(r"^\s*>\s?", "", result, flags=re.M)
        result = re.sub(r"^\s*\|?[\s:\-|]+\|\s*$", "", result, flags=re.M)
        result = re.sub(r"\s*\|\s*", "，", result)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()

    def nudge_typos(self, text: str, rate: float) -> str:
        """Swap or double one Chinese character pair, with the given probability.

        Args:
            text: Reply text.
            rate: Probability of touching this reply at all.

        Returns:
            The possibly changed text; at most one typo per reply.
        """
        if rate <= 0 or len(text) < 6 or self.rng.random() >= rate:
            return text
        chars = list(text)
        positions = [
            index
            for index in range(1, len(chars) - 1)
            if _CHINESE.match(chars[index]) and _CHINESE.match(chars[index - 1])
        ]
        if not positions:
            return text
        index = self.rng.choice(positions)
        if self.rng.random() < 0.5:
            chars[index - 1], chars[index] = chars[index], chars[index - 1]
        else:
            chars.insert(index, chars[index])
        return "".join(chars)

    def cap_length(self, text: str, max_chars: int) -> str:
        """Cut an over-long reply on a sentence boundary.

        Args:
            text: Reply text.
            max_chars: Character budget; non-positive means no limit.

        Returns:
            The capped text.
        """
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        cut = max(text.rfind(char, 0, max_chars) for char in _SENTENCE_END) + 1
        if cut <= 0:
            return text[:max_chars].rstrip()
        return text[:cut].rstrip()

    def split_segments(self, text: str, probability: float) -> list[str]:
        """Split a reply into at most ``max_parts`` sentences by probability.

        Args:
            text: Reply text.
            probability: Chance of splitting this reply.

        Returns:
            One or more message texts.
        """
        if (
            probability <= 0
            or len(text) < self.config.min_split_chars
            or self.rng.random() >= probability
        ):
            return [text]
        parts = [
            part.strip()
            for part in re.split(r"(?<=[。！？!?…~])\s*", text)
            if part.strip()
        ]
        if len(parts) < 2:
            return [text]
        max_parts = max(self.config.max_parts, 1)
        if len(parts) > max_parts:
            chunk = (len(parts) + max_parts - 1) // max_parts
            parts = [
                "".join(parts[index : index + chunk])
                for index in range(0, len(parts), chunk)
            ]
        return parts

    async def process(
        self, event: Any, result: Any, upstream_segmented: bool = False
    ) -> None:
        """Rewrite the plain text of an outbound result in place.

        Args:
            event: Bot message event carrying the session identity.
            result: Message result whose ``chain`` is about to be sent.
            upstream_segmented: Whether the upstream segmented reply is enabled.
        """
        if not self.config.enabled:
            return
        checker = getattr(result, "is_llm_result", None)
        if callable(checker) and not checker():
            # Command and plugin output is not the bot's own wording.
            self._counters["skipped"] += 1
            return
        chain: list[Any] = list(getattr(result, "chain", []) or [])
        if not chain:
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return
        getter = getattr(event, "get_group_id", None)
        group_id = getter() if callable(getter) else None
        if not self.session_enabled(umo, group_id):
            self._counters["skipped"] += 1
            return
        profile = await self._profile(event)
        if profile is None:
            self._counters["skipped"] += 1
            return
        self._counters["processed"] += 1
        allow_markdown = bool(profile.get("allow_markdown"))
        max_chars = int(profile.get("reply_max_chars") or 0)
        typo_rate = float(profile.get("typo_rate") or 0.0)
        segment_prob = float(profile.get("multi_segment_prob") or 0.0)
        quote_rate = float(profile.get("quote_rate") or 0.0)
        if self.scenes is not None:
            scene = scene_policy_for(self.scenes, umo, group_id)
            if scene is not None:
                quote_rate = (
                    scale_quote_rate(quote_rate, scene.quote_frequency)
                    if scene.allow_quote
                    else 0.0
                )

        plain_texts = [
            comp for comp in chain if isinstance(comp, Plain) and comp.text.strip()
        ]
        stripped_any = typo_any = capped_any = False
        for comp in plain_texts:
            text = comp.text
            if self.translator is not None:
                translated = await self.translator(text)
                if translated:
                    text = translated
                    self._counters["translated"] += 1
            if not allow_markdown and self.config.strip_markdown:
                stripped = self.strip_markdown(text)
                if stripped != text:
                    self._counters["markdown_stripped"] += 1
                    stripped_any = True
                text = stripped or text
            if self.config.nudge_typos and typo_rate > 0:
                nudged = self.nudge_typos(text, typo_rate)
                if nudged != text:
                    self._counters["typos"] += 1
                    typo_any = True
                text = nudged
            if self.config.cap_length and max_chars > 0:
                capped = self.cap_length(text, max_chars)
                if capped != text:
                    self._counters["capped"] += 1
                    capped_any = True
                text = capped
            comp.text = text

        delivered = 0
        if (
            self.config.split_segments
            and not upstream_segmented
            and len(chain) == 1
            and len(plain_texts) == 1
            and segment_prob > 0
        ):
            segments = self.split_segments(plain_texts[0].text, segment_prob)
            if len(segments) > 1:
                self._counters["split"] += 1
                delivered = len(segments) - 1
                # The responder sends the first segment; the rest follow with a
                # delay, because component boundaries alone never become separate
                # messages without the upstream segmented-reply switch.
                result.chain = [Plain(segments[0])]
                chain = list(result.chain)
                self._deliver_rest(event, result, segments[1:])

        platform = str(getattr(event, "get_platform_name", lambda: "")() or "")
        message_obj = getattr(event, "message_obj", None)
        message_id = getattr(message_obj, "message_id", None)
        already_quoted = any(isinstance(comp, Reply) for comp in chain)
        if (
            self.config.quote_replies
            and not already_quoted
            and message_id is not None
            and platform in self.config.quote_platforms
            and quote_rate > 0
            and self.rng.random() < quote_rate
        ):
            chain.insert(0, Reply(id=message_id))
            result.chain = chain
            self._counters["quoted"] += 1

        supplement_payload = None
        extra_getter = getattr(event, "get_extra", None)
        if callable(extra_getter):
            supplement_payload = extra_getter("_life_reply_supplement")
            pending = extra_getter("_life_reply_supplement_task")
            if (
                supplement_payload is None
                and self.config.supplement_enabled
                and isinstance(pending, asyncio.Task)
            ):
                try:
                    supplement_payload = await asyncio.wait_for(
                        pending,
                        timeout=max(self.config.supplement_timeout_seconds, 0.0)
                        or None,
                    )
                except Exception:
                    supplement_payload = None
        if self.config.supplement_enabled and supplement_payload:
            from bot.life.reply.supplement import (
                ResolvedSupplement,
                compose_supplement,
            )

            resolved = ResolvedSupplement.from_extra(supplement_payload)
            if resolved is not None and resolved.parts:
                try:
                    chains = compose_supplement(resolved, result)
                except ValueError:
                    logger.warning("[life] dropped invalid reply supplement")
                else:
                    result.chain = list(chains[0].chain)
                    if len(chains) > 1:
                        self._deliver_chains(event, result, chains[1:])
                    self._counters["supplemented"] += 1

        texts = [comp.text for comp in result.chain if isinstance(comp, Plain)]
        final = "".join(texts)
        self._recent.append(
            {
                "umo": umo,
                "chars": len(final),
                "segments": 1 + delivered,
                "quoted": any(isinstance(comp, Reply) for comp in result.chain),
                "markdown": stripped_any,
                "leak": any(pattern.search(final) for pattern in _MARKDOWN),
                "typo": typo_any,
                "capped": capped_any,
            }
        )

    def _deliver_rest(self, event: Any, result: Any, segments: list[str]) -> None:
        """Send the trailing reply segments after a short pause each.

        Args:
            event: Bot message event used as the send channel.
            result: Decorated result; only its metadata is inherited.
            segments: Texts to deliver after the first one.
        """

        async def deliver() -> None:
            for text in segments:
                await asyncio.sleep(self.config.segment_delay_seconds)
                try:
                    await event.send(result.derive([Plain(text)]))
                except Exception:
                    logger.exception("[life] failed to send a follow-up segment")

        task = asyncio.create_task(deliver())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _deliver_chains(self, event: Any, result: Any, chains: list[Any]) -> None:
        """Send supplement messages after the main reply.

        Args:
            event: Bot message event used as the send channel.
            result: Decorated result; only its metadata is inherited.
            chains: Message chains produced by the supplement plan.
        """

        async def deliver() -> None:
            for chain in chains:
                await asyncio.sleep(self.config.segment_delay_seconds)
                try:
                    await event.send(result.derive(list(chain.chain)))
                except Exception:
                    logger.exception("[life] failed to send a supplement message")

        task = asyncio.create_task(deliver())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _profile(self, event: Any) -> dict[str, Any] | None:
        """Resolve the style profile of the session's persona.

        Args:
            event: Bot message event carrying ``_trace_persona_id``.

        Returns:
            Profile mapping, or None when no persona layer is available.
        """
        if self.persona is None:
            return None
        extra_getter = getattr(event, "get_extra", None)
        persona_id = ""
        if callable(extra_getter):
            persona_id = str(extra_getter("_trace_persona_id") or "")
        return await self.persona.profile_for(persona_id)

    def status(self) -> dict[str, Any]:
        """Return switches, counters and the recent reply ring.

        Returns:
            Status mapping used by the dashboard API.
        """
        return {
            "enabled": self.config.enabled,
            "strip_markdown": self.config.strip_markdown,
            "nudge_typos": self.config.nudge_typos,
            "cap_length": self.config.cap_length,
            "split_segments": self.config.split_segments,
            "quote_replies": self.config.quote_replies,
            "quote_platforms": list(self.config.quote_platforms),
            "segment_delay_seconds": self.config.segment_delay_seconds,
            "counters": dict(self._counters),
            "recent": [dict(entry) for entry in self._recent],
        }


async def postprocess_life_reply(
    event: Any,
    result: Any,
    upstream_segmented: bool = False,
) -> None:
    """Rewrite one outbound result, if the Life reply layer is active.

    Args:
        event: Bot message event being answered.
        result: Message result about to be decorated and sent.
        upstream_segmented: Whether the upstream segmented reply is enabled.
    """
    # Imported here: ``service`` imports this module while building the runtime.
    from bot.life.service import get_life_service

    service = get_life_service()
    if service is None:
        return
    processor = getattr(service, "reply", None)
    if processor is not None:
        try:
            await processor.process(
                event,
                result,
                upstream_segmented=upstream_segmented,
            )
        except Exception:
            # Post-processing is an enhancement: a failure must never block a reply.
            logger.exception("[life] reply post-processing failed")
    monitor = getattr(service, "monitor", None)
    if monitor is not None:
        try:
            monitor.record_delivery(event, result)
        except Exception:
            logger.exception("[life] monitor delivery record failed")
