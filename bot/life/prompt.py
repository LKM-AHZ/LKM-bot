"""Prompt assembly for the Life layer (S5b).

S5a left every service appending its own ``<life_*>`` block. That works, but it
gives no answer to "how big is the prompt" and no protection against a chatty
block starving the persona. The assembler is the single place that:

* keeps the fixed order (persona, memory, expression, jargon, focus, attention);
* removes an identical line that a later block repeats;
* caps each block and the total, dropping whole blocks from the least important
  end instead of cutting a block in half;
* records what it did for ``/api/v1/life/status``.

It runs after the injectors, so each injector still owns its own "does this
apply" logic; the assembler owns size and order.
"""

from __future__ import annotations

import re
from typing import Any

from bot.life.config import PromptConfig

BLOCK_ORDER = (
    "persona",
    "memory",
    "expression",
    "jargon",
    "focus",
    "attention",
    "subagent",
)
"""Fixed importance order: dropping starts at the end of this tuple."""

_BLOCK_RE = re.compile(r"<life_([a-z_]+)>(.*?)</life_\1>", re.S)
_MIN_DEDUPE_CHARS = 8


def _trim_body(body: str, cap: int) -> str:
    """Cut a block body at a line boundary.

    Args:
        body: Text between the block tags.
        cap: Maximum number of characters to keep.

    Returns:
        The trimmed body; a single over-long line is hard-sliced.
    """
    if len(body) <= cap:
        return body
    kept: list[str] = []
    total = 0
    for line in body.split("\n"):
        if total + len(line) + 1 > cap:
            break
        kept.append(line)
        total += len(line) + 1
    return "\n".join(kept) if kept else body[:cap]


class PromptAssembler:
    """Budget keeper for the Life prompt blocks."""

    def __init__(self, config: PromptConfig) -> None:
        """Initialize the assembler.

        Args:
            config: Budget, per-block cap and dedupe switch.
        """
        self.config = config
        self._counters = {
            "assemblies": 0,
            "kept_blocks": 0,
            "dropped_blocks": 0,
            "trimmed_blocks": 0,
            "deduped_lines": 0,
        }
        self._last: dict[str, Any] = {}

    def assemble(self, system_prompt: str, umo: str = "") -> str:
        """Normalize, dedupe and trim every Life block in the prompt.

        Args:
            system_prompt: System prompt after all injectors ran.
            umo: Session key, recorded in the last-assembly snapshot.

        Returns:
            The assembled prompt (unchanged when there is nothing to do).
        """
        if not self.config.enabled:
            return system_prompt
        matches = list(_BLOCK_RE.finditer(system_prompt))
        if not matches:
            return system_prompt
        self._counters["assemblies"] += 1

        blocks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for match in matches:
            body = match.group(2)
            if self.config.dedupe:
                kept: list[str] = []
                for line in body.split("\n"):
                    key = " ".join(line.split())
                    if len(key) >= _MIN_DEDUPE_CHARS and key in seen:
                        self._counters["deduped_lines"] += 1
                        continue
                    if len(key) >= _MIN_DEDUPE_CHARS:
                        seen.add(key)
                    kept.append(line)
                body = "\n".join(kept)
            trimmed = _trim_body(body, self.config.max_block_chars)
            if len(trimmed) < len(body):
                self._counters["trimmed_blocks"] += 1
            blocks.append(
                {
                    "name": match.group(1),
                    "start": match.start(),
                    "end": match.end(),
                    "body": trimmed,
                }
            )

        order = {name: index for index, name in enumerate(BLOCK_ORDER)}
        blocks.sort(key=lambda block: order.get(block["name"], len(order)))

        # Drop from the least important end until the block budget is met.
        widths = {block["name"]: len(block["body"]) for block in blocks}
        total = sum(widths.values())
        dropped: list[str] = []
        if total > self.config.max_total_chars:
            for block in reversed(blocks):
                if total <= self.config.max_total_chars:
                    break
                if block["name"] == "persona":
                    continue
                block["body"] = ""
                dropped.append(block["name"])
                total -= widths[block["name"]]
        if total > self.config.max_total_chars:
            persona = next(
                (block for block in blocks if block["name"] == "persona"), None
            )
            if persona is not None:
                persona["body"] = _trim_body(
                    persona["body"], self.config.max_total_chars
                )

        # Rebuild from the end so earlier offsets stay valid.
        result = system_prompt
        original = {match.group(1): match for match in matches}
        for block in sorted(blocks, key=lambda item: item["start"], reverse=True):
            match = original[block["name"]]
            if block["name"] in dropped:
                result = result[: match.start()] + result[match.end() :]
                continue
            replacement = (
                f"<life_{block['name']}>{block['body']}</life_{block['name']}>"
            )
            result = result[: match.start()] + replacement + result[match.end() :]
        if dropped:
            # A removed block leaves a hole between two blank lines.
            result = re.sub(r"\n{3,}", "\n\n", result)

        kept = [block["name"] for block in blocks if block["body"]]
        self._counters["kept_blocks"] += len(kept)
        self._counters["dropped_blocks"] += len(dropped)
        self._last = {
            "umo": umo,
            "total_chars": sum(len(block["body"]) for block in blocks),
            "blocks": [
                {"name": block["name"], "chars": len(block["body"])} for block in blocks
            ],
            "kept": kept,
            "dropped": dropped,
        }
        return result

    def status(self) -> dict[str, Any]:
        """Return budgets, counters and the last assembly.

        Returns:
            Status mapping used by the dashboard API.
        """
        return {
            "enabled": self.config.enabled,
            "max_total_chars": self.config.max_total_chars,
            "max_block_chars": self.config.max_block_chars,
            "counters": dict(self._counters),
            "last": dict(self._last),
        }
