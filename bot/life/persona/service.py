"""Persona service: keep reply style in one profile and render it into the prompt.

The block is prepended, not appended: persona is the base stance, so the other
Life blocks (memory, expressions, jargon, focus) read as material that supports
it instead of overriding it.
"""

from __future__ import annotations

import time
from typing import Any

from bot import logger
from bot.life.config import LifeConfig, PersonaConfig
from bot.life.persona.store import PersonaStore

_BLOCK_TAG = "life_persona"


class PersonaService:
    """Runtime owner of persona profiles."""

    def __init__(
        self,
        config: PersonaConfig,
        db_helper: Any,
        life_config: LifeConfig | None = None,
    ) -> None:
        """Initialize the service without opening resources.

        Args:
            config: Persona settings.
            db_helper: Core async database helper.
            life_config: Root Life configuration used for the allowlist.
        """
        self.config = config
        self.life_config = life_config
        self.store = PersonaStore(db_helper, config)
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._started = False
        self._counters = {"injections": 0, "cache_hits": 0, "resolved": 0}

    @property
    def enabled(self) -> bool:
        """Return whether the persona runtime is active."""
        return self._started

    def session_enabled(self, umo: str, group_id: str | None = None) -> bool:
        """Return whether a session is inside the Life allowlist.

        Args:
            umo: Session key.
            group_id: Group id, or None.

        Returns:
            True when the session may use the profile.
        """
        if self.life_config is None:
            return True
        return bool(self.life_config.session_enabled(umo, group_id))

    async def initialize(self) -> None:
        """Create the table and mark the service ready."""
        await self.store.ensure_schema()
        self._started = True
        logger.info("[life] persona layer ready")

    async def close(self) -> None:
        """Stop the runtime and drop the cache."""
        self._started = False
        self._cache.clear()

    async def profile_for(self, persona_id: str) -> dict[str, Any] | None:
        """Resolve the profile of a persona, falling back to the default row.

        Args:
            persona_id: Upstream persona id; empty for the default profile.

        Returns:
            Profile row, or None when neither the persona nor the fallback has one.
        """
        now = time.time()
        cached = self._cache.get(persona_id)
        if cached is not None and cached[0] > now:
            self._counters["cache_hits"] += 1
            return cached[1]
        row = await self.store.get(persona_id)
        if row is None and persona_id:
            row = await self.store.get("")
        self._cache[persona_id] = (now + self.config.cache_ttl_seconds, row)
        if row is not None:
            self._counters["resolved"] += 1
        return row

    def render_block(self, profile: dict[str, Any], scene: str) -> str:
        """Render the persona block for one scene.

        Args:
            profile: Stored profile row.
            scene: ``group`` or ``private``.

        Returns:
            ``<life_persona>`` block.
        """
        prompt_cap = max(self.config.block_max_chars // 3, 120)
        parts: list[str] = []
        base = str(profile.get("base_prompt") or "").strip()
        if base:
            parts.append(base[:prompt_cap])
        scene_prompt = str(
            profile.get(f"{scene}_prompt") or ""  # group_prompt / private_prompt
        ).strip()
        if scene_prompt:
            parts.append(scene_prompt[:prompt_cap])
        if not parts:
            parts.append("你是这个会话里的普通成员，不是客服，也不是助手。")
        max_chars = int(profile.get("reply_max_chars") or self.config.reply_max_chars)

        def rate(key: str, fallback: float) -> float:
            """Read a 0..1 profile rate, keeping an explicit 0.0."""
            value = profile.get(key)
            return fallback if value is None else float(value)

        rules = [f"- 长度：单条回复不超过 {max_chars} 字，能一句话说完就别展开。"]
        # The format ban goes before the optional rates: when the block is
        # trimmed, a style probability may be lost, the Markdown ban may not.
        if not bool(profile.get("allow_markdown")):
            rules.append(
                "- 格式：不要输出 Markdown（标题、列表、表格、代码块、加粗）。"
            )
        segment_prob = rate(
            "multi_segment_prob", self.config.default_multi_segment_prob
        )
        if segment_prob > 0:
            rules.append(
                f"- 分段：约 {segment_prob:.0%} 的回复拆成两三条短消息，其余一次说完。"
            )
        quote_rate = rate("quote_rate", self.config.default_quote_rate)
        if quote_rate > 0:
            rules.append(
                f"- 引用：约 {quote_rate:.0%} 的回复引用你要回的那条消息，其余直接说话。"
            )
        typo_rate = rate("typo_rate", self.config.default_typo_rate)
        if typo_rate > 0:
            rules.append(
                f"- 口吻：口语，允许偶尔打错字（约 {typo_rate:.0%}），但不影响理解。"
            )
        block = (
            f"<{_BLOCK_TAG}>\n"
            "你的人设补充（优先于一般风格要求，但不要复述这份说明）：\n"
            + "\n".join(parts)
            + "\n"
            + "\n".join(rules)
            + f"\n</{_BLOCK_TAG}>"
        )
        return block[: self.config.block_max_chars]

    async def inject(self, event: Any, req: Any) -> bool:
        """Prepend the persona block to the provider request.

        Args:
            event: Bot message event carrying the session identity.
            req: ``ProviderRequest`` about to be sent to the model.

        Returns:
            True when the prompt was modified.
        """
        if not self._started or not self.config.enabled:
            return False
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return False
        getter = getattr(event, "get_group_id", None)
        group_id = getter() if callable(getter) else None
        if not self.session_enabled(umo, group_id):
            return False
        system_prompt = getattr(req, "system_prompt", "") or ""
        if f"<{_BLOCK_TAG}" in system_prompt:
            return False
        extra_getter = getattr(event, "get_extra", None)
        persona_id = ""
        if callable(extra_getter):
            persona_id = str(extra_getter("_trace_persona_id") or "")
        profile = await self.profile_for(persona_id)
        if profile is None:
            return False
        block = self.render_block(profile, "group" if group_id else "private")
        req.system_prompt = f"{block}\n\n{system_prompt}" if system_prompt else block
        self._counters["injections"] += 1
        return True

    async def upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create or replace one profile and drop the cache.

        Args:
            payload: Profile fields; ``persona_id`` is required.

        Returns:
            The stored row.
        """
        row = await self.store.upsert(payload)
        self._cache.clear()
        return row

    async def delete(self, persona_id: str) -> bool:
        """Delete one profile and drop the cache.

        Args:
            persona_id: Upstream persona id; empty for the default profile.

        Returns:
            True when a row was removed.
        """
        self._cache.clear()
        return await self.store.delete(persona_id)

    async def list_profiles(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return stored profiles.

        Args:
            limit: Maximum rows.

        Returns:
            Profile rows.
        """
        return await self.store.list_profiles(limit)

    async def status(self) -> dict[str, Any]:
        """Return persona counters.

        Returns:
            Status mapping used by the dashboard API.
        """
        stats = await self.store.stats()
        return {
            "enabled": self._started,
            "configured": self.config.enabled,
            "profiles": stats["total"],
            "cache_entries": len(self._cache),
            "counters": dict(self._counters),
        }
