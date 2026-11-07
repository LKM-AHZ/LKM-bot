"""Configuration for the Life layer.

The Life layer is the sidecar subsystem used to migrate MaiBot capabilities. It
stays disabled by default and keeps its own ``data/life/config.json`` instead of
writing into ``cmd_config.json``, so the upstream configuration schema is not
polluted.

Gray release: ``enabled`` is the master switch and ``scope.mode`` controls the
rollout. The default ``allowlist`` mode only takes over sessions explicitly
listed in ``enabled_umos`` or ``enabled_group_ids``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from bot import logger
from bot.core.utils.bot_path import get_bot_data_path, get_bot_root
from bot.life.rollout.buckets import enabled_by_percent

LIFE_DIR_NAME = "life"
LIFE_CONFIG_FILE_NAME = "config.json"


class IngestConfig(BaseModel):
    """Message archive settings."""

    enabled: bool = True
    """Whether incoming messages are archived; disabled means a no-op stage."""

    store_components: bool = True
    """Whether full message components are stored; otherwise text only."""

    max_text_chars: int = 4000
    """Maximum stored plain-text length per message."""

    max_component_chars: int = 512
    """Maximum stored component field length; base64 is folded to a marker."""

    queue_size: int = 10000
    """In-memory queue capacity; overflow drops the oldest batch and counts."""

    batch_size: int = 50
    """Maximum rows written per flush."""

    retention_days: int = 0
    """Retention in days; 0 keeps messages forever."""


class ScopeConfig(BaseModel):
    """Gray-release scope."""

    mode: Literal["allowlist", "all"] = "allowlist"
    """``allowlist`` only takes over listed sessions; ``all`` takes over all."""

    enabled_umos: list[str] = Field(default_factory=list)
    """Sessions enabled by unified_msg_origin."""

    enabled_group_ids: list[str] = Field(default_factory=list)
    """Group ids enabled for the rollout."""

    disabled_umos: list[str] = Field(default_factory=list)
    """Sessions explicitly excluded; takes precedence over enabled lists."""

    rollout_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    """Share of sessions taken over by the percentage rollout, 0 to 100.

    The bucket is a stable hash of the group (or the private session), so a
    conversation never flips on random messages. Explicit ``enabled_umos`` and
    ``enabled_group_ids`` entries always bypass the percentage.
    """

    rollout_salt: str = ""
    """Salt of the bucket hash; change it to reshuffle the assignment."""


class SenderConfig(BaseModel):
    """Proactive sending settings."""

    enabled: bool = True
    timeout_seconds: float = 30.0


class MemoryEmbeddingConfig(BaseModel):
    """Embedding bridge to the LKM provider layer."""

    enabled: bool = True
    """Whether vectors are produced; disabled keeps keyword-only retrieval."""

    provider_id: str = ""
    """Explicit embedding provider id; empty uses the active embedding provider."""

    batch_size: int = 16
    timeout_seconds: float = 8.0
    failure_threshold: int = 3
    """Consecutive failures tolerated before the circuit breaker opens."""

    cooldown_seconds: float = 60.0
    """How long the breaker stays open before one half-open trial is allowed."""

    max_concurrency: int = 2


class MemoryInjectConfig(BaseModel):
    """Prompt injection of retrieved memories."""

    enabled: bool = True
    top_k: int = 5
    max_chars: int = 1200
    """Character budget of the injected block."""

    min_score: float = 0.12
    kinds: list[str] = Field(
        default_factory=lambda: ["summary", "fact", "episode", "note"]
    )
    max_age_days: int = 0
    """Ignore memories older than this; 0 disables the age filter."""


class MemoryDigestConfig(BaseModel):
    """Background digest pipeline: messages -> summaries, facts, episodes."""

    enabled: bool = True
    interval_minutes: int = 30
    min_messages: int = 8
    max_messages: int = 120
    gap_minutes: int = 10
    """Idle gap that closes a conversation window."""

    use_llm: bool = True
    llm_timeout_seconds: float = 30.0
    max_sessions_per_round: int = 20


class MemoryUpstreamConfig(BaseModel):
    """Optional A_memorix engine bridge."""

    engine: Literal["local", "a_memorix"] = "local"
    """``local`` uses the built-in SQLite engine; ``a_memorix`` prefers upstream."""

    data_dir: str = ""
    """Engine data directory; empty means ``<life>/memory/upstream``."""

    namespace_prefix: str = "lkm"


class MemoryConfig(BaseModel):
    """Long-term memory settings."""

    enabled: bool = False
    """Master switch; also requires the Life layer itself to be enabled."""

    retention_days: int = 0
    embedding: MemoryEmbeddingConfig = Field(default_factory=MemoryEmbeddingConfig)
    inject: MemoryInjectConfig = Field(default_factory=MemoryInjectConfig)
    digest: MemoryDigestConfig = Field(default_factory=MemoryDigestConfig)
    upstream: MemoryUpstreamConfig = Field(default_factory=MemoryUpstreamConfig)


class PersonConfig(BaseModel):
    """People, facts and relationship tracking (S3)."""

    enabled: bool = False
    """Master switch; also requires the Life layer itself to be enabled."""

    relation_increment: float = 1.0
    """Relationship score added per observed message."""

    relation_max: float = 100.0
    """Upper bound of the relationship score."""

    relation_half_life_days: float = 30.0
    """Idle days after which the relationship score halves."""

    confidence_half_life_days: float = 21.0
    """Days after which a fact's confidence halves."""

    min_confidence: float = 0.2
    """Facts whose effective confidence falls below this stop being used."""

    conflict_similarity: float = 0.6
    """Token overlap above which a new fact conflicts with an existing one."""

    profile_max_facts: int = 12
    profile_ttl_minutes: int = 360
    use_llm_profile: bool = True


class PersonaConfig(BaseModel):
    """Persona extensions and reply style (S5a)."""

    enabled: bool = False
    """Master switch; also requires the Life layer itself to be enabled."""

    default_quote_rate: float = 0.2
    """Fallback quote-reply probability for profiles that do not set one."""

    default_multi_segment_prob: float = 0.15
    """Fallback probability of splitting a reply into several messages."""

    default_typo_rate: float = 0.02
    """Fallback probability of nudging a character into a typo."""

    reply_max_chars: int = 120
    """Default hard length hint handed to the model."""

    block_max_chars: int = 600
    """Upper bound of the rendered persona block."""

    cache_ttl_seconds: int = 300
    """How long a resolved profile is cached before it is re-read."""


class PromptConfig(BaseModel):
    """Prompt assembly budgets (S5b)."""

    enabled: bool = True
    """Runs after every injector; disabling restores plain appending."""

    max_total_chars: int = 2400
    """Budget for all Life blocks together."""

    max_block_chars: int = 800
    """Budget for one Life block."""

    dedupe: bool = True
    """Drop an exact line that an earlier block already carries."""


class ReplyConfig(BaseModel):
    """Reply post-processing switches (S5c)."""

    enabled: bool = False
    """Master switch: the only layer that rewrites outbound text."""

    strip_markdown: bool = True
    nudge_typos: bool = True
    cap_length: bool = True
    split_segments: bool = True
    quote_replies: bool = True
    quote_platforms: list[str] = Field(default_factory=lambda: ["aiocqhttp"])
    min_split_chars: int = 24
    """Short replies are never split, whatever the profile probability says."""

    max_parts: int = 3
    """Upper bound of messages one reply may become."""

    segment_delay_seconds: float = 1.2
    """Pause before each follow-up segment; 0 sends them immediately."""

    supplement_enabled: bool = False
    """Plan-driven send order: ``@`` / pictures / stickers around one reply."""

    supplement_max_parts: int = 8
    """Ceiling of actions one supplement plan may contain."""

    supplement_allow_at: bool = True
    supplement_allow_quote: bool = True
    supplement_timeout_seconds: float = 8.0
    """Hard timeout for one supplement planning call."""

    supplement_retry_once: bool = True
    """Retry the planning call once with the validation error appended."""


class ExpressionConfig(BaseModel):
    """Learned expressions (S3b)."""

    enabled: bool = False
    """Master switch; also requires the Life layer itself to be enabled."""

    learn_interval_minutes: int = 60
    max_learn_messages: int = 200
    min_frequency: int = 2
    """How often a heuristic candidate must appear before it is learned."""

    min_style_chars: int = 2
    max_style_chars: int = 16
    use_llm: bool = True
    """Ask the chat model for (situation, style) pairs when available."""

    use_vector: bool = True
    """Reuse the FAISS index for candidate retrieval; keyword scoring otherwise."""

    top_k: int = 3
    min_score: float = 0.2
    weight_half_life_days: float = 30.0
    """Days after which an expression's weight halves without reuse."""

    scene: Literal["any", "group", "private"] = "any"
    """Restrict injection to one kind of session."""


class JargonConfig(BaseModel):
    """Group slang (S3c)."""

    enabled: bool = False
    """Master switch; also requires the Life layer itself to be enabled."""

    learn_interval_minutes: int = 60
    max_learn_messages: int = 200
    use_llm: bool = True
    """Ask the chat model to explain mined terms when available."""

    top_k: int = 4
    min_confidence: float = 0.5
    min_term_chars: int = 2
    max_term_chars: int = 8
    max_meaning_chars: int = 80


class EmojiConfig(BaseModel):
    """Sticker library and send policy (S3d)."""

    enabled: bool = False
    """Master switch; also requires the Life layer itself to be enabled."""

    collect_interval_minutes: int = 60
    max_collect_messages: int = 300
    allow_remote: bool = True
    """Download http(s) images; ``False`` keeps only local file paths."""

    download_timeout_seconds: float = 10.0
    max_bytes: int = 4 * 1024 * 1024
    max_tags: int = 6
    max_label_chars: int = 40
    use_llm_tags: bool = True
    """Ask the chat model to label an image when a reference is available."""

    min_score: float = 0.25
    cooldown_seconds: int = 300
    max_per_hour: int = 3
    max_storage_files: int = 2000

    semantic_enabled: bool = False
    """Match stickers by intent through per-axis tag vectors (MaiBot 1.4)."""

    semantic_min_score: float = 0.30
    """Below this the semantic match is ignored and the classic pick is used."""

    semantic_timeout_seconds: float = 15.0
    """Hard timeout for one vision labelling call."""

    semantic_backfill_limit: int = 8
    """Stickers labelled per collection round."""


DEFAULT_GATE_WEIGHTS: dict[str, float] = {
    "bias": -1.2,
    "at_bot": 2.4,
    "reply_to_bot": 1.6,
    "wake_word": 1.6,
    "bot_name": 1.2,
    "question": 0.7,
    "mentions_other": -0.9,
    "sticker_only": -0.4,
    "burst_length": 0.2,
    "relation": 0.01,
}
"""Linear weights of the reply-likelihood model.

Every feature contributes ``weight * value`` to the logit; ``burst_length`` is
the number of messages merged into the batch and ``relation`` the 0-100
relationship score of the last sender. They stay plain numbers so the replay set
can re-tune them offline without touching code.
"""


class DebounceConfig(BaseModel):
    """Message debounce before a turn is released to the planner (S4)."""

    enabled: bool = True
    """When false every message is released immediately."""

    quick_seconds: float = 1.2
    """Quiet window after a direct address (@, wake word, reply to the bot)."""

    natural_seconds: float = 2.5
    """Default quiet window for a turn that is not still growing."""

    calm_seconds: float = 4.0
    """Quiet window for a busy turn of three or more messages."""

    hard_cap_seconds: float = 8.0
    """Upper bound measured from the first merged message; never waits longer."""

    same_sender_extra_seconds: float = 1.0
    """Extra wait when one person sends several messages in a row."""

    same_sender_gap_seconds: float = 6.0
    """Gap below which two messages from one sender count as one turn."""


class GateConfig(BaseModel):
    """Reply likelihood and the dynamic gate (S4)."""

    enabled: bool = True
    """When false every released turn is answered."""

    base_threshold: float = 0.55
    """Likelihood needed to answer when the bot is not directly addressed."""

    weights: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_GATE_WEIGHTS)
    )
    """Linear weights of the reply-likelihood model; tuned offline on replay."""

    idle_silence_seconds: float = 900.0
    """Sessions without bot activity this long stay silent unless addressed."""

    idle_actionable_seconds: float = 120.0
    """Idle time after which the threshold starts rising."""

    idle_rise_max: float = 0.25
    """Maximum threshold increase caused by idleness."""

    reply_cooldown_seconds: float = 3.0
    """Suppress an answer this soon after one was sent (echo / double reply)."""

    backoff_step: float = 0.08
    """Threshold increase per consecutive wait in the same session."""

    backoff_max: float = 0.32
    """Cap of the consecutive-wait penalty."""


class FocusConfig(BaseModel):
    """Session timeline, turn compression and checkpoints (S4)."""

    enabled: bool = False
    """Master switch; also requires ``heartflow.enabled``."""

    keep_turns: int = 12
    """Turns kept verbatim in memory before the oldest are folded."""

    max_turns: int = 60
    """Hard cap of turns kept per session in the database."""

    gap_seconds: int = 300
    """Idle gap that closes the current turn and starts a new one."""

    turn_max_chars: int = 400
    """Cap of one folded turn inside the digest."""

    digest_max_chars: int = 1500
    """Cap of the digest; the oldest lines are dropped first."""

    block_max_chars: int = 1200
    """Cap of the injected ``<life_focus>`` block."""

    inject: bool = True
    """Append the folded digest to the system prompt when it is non-empty."""

    max_sessions: int = 500
    """Timelines kept in memory; the rest are reloaded from the database."""

    checkpoint_interval_minutes: int = 5
    """How often dirty timelines are flushed back to the database."""


class RuntimeConfig(BaseModel):
    """Turn runtime that actually answers ambient messages (S4c)."""

    enabled: bool = True
    """Requires ``heartflow.enabled``; the stage is inert otherwise."""

    shadow: bool = False
    """Decide and record every turn but never force a wake (shadow rollout)."""

    max_pending_turns: int = 32
    """Sessions that may hold an event at the same time; beyond it a turn waits."""

    max_wait_seconds: float = 12.0
    """Hard ceiling of one debounce wait, independent of the message timestamps."""

    recent_size: int = 50
    """Decision records kept in memory for the dashboard and diagnostics."""

    hot_window_seconds: float = 60.0
    """After the bot answers, turns in that session use the quick debounce tier."""

    fast_release: bool = True
    """Answer a very short direct ping without waiting for the window."""

    fast_max_chars: int = 8
    """Merged text of at most this many characters counts as a fast ping."""

    decision_retention_days: int = 90
    """Days of released-turn decisions kept for the gray report; 0 keeps all."""


class AttentionConfig(BaseModel):
    """Attention drift and back-reference hints (S4d)."""

    enabled: bool = True
    """Requires ``heartflow.enabled``."""

    half_life_minutes: float = 40.0
    """Minutes after which the bot's focus on a session halves."""

    min_focus: float = 0.05
    """Below this the topic is forgotten instead of being hooked back to."""

    hook_focus_min: float = 0.05
    """Lower bound of the "drifted but still remembers" band."""

    hook_focus_max: float = 0.5
    """Above this the bot is still focused, so no hook hint is needed."""

    hook_min_gap_minutes: float = 10.0
    """A back reference only makes sense after at least this much silence."""

    hook_max_chars: int = 200
    """Cap of the quoted topic inside the hint."""

    keywords: int = 6
    """Salient segments kept per session."""


class IdleConfig(BaseModel):
    """Proactive talk, silence and idle backoff (S4d)."""

    enabled: bool = False
    """Off by default: talking first is the riskiest behaviour of the layer."""

    scan_interval_minutes: int = 10
    min_idle_minutes: int = 45
    """A session must be quiet this long before the bot may speak first."""

    max_idle_minutes: int = 720
    """Past this silence the thread is considered dead; do not revive it."""

    min_talk_value: float = 0.55
    """Talk value below this means the bot should stay silent."""

    daily_cap: int = 2
    """Maximum proactive messages per session per day."""

    max_per_round: int = 1
    """Maximum proactive messages per scan, across sessions."""

    answer_window_minutes: int = 30
    """A proactive message counts as ignored if nobody answers within this."""

    backoff_base_minutes: float = 120.0
    backoff_max_minutes: float = 1440.0
    """Exponential backoff after ignored proactive messages."""

    answer_bonus: float = 0.1
    """Talk value gained when people answer the bot."""

    ignore_penalty: float = 0.2
    """Talk value lost when a proactive message is ignored."""

    quiet_hours: list[int] = Field(default_factory=lambda: [0, 8])
    """Local hours ``[start, end)`` in which the bot never speaks first."""

    max_scan_sessions: int = 50
    """Sessions inspected per scan."""


class ToolResultConfig(BaseModel):
    """Condensing oversized tool results before they enter the history (S6a).

    The raw text is kept in a bounded in-memory stash and can be pulled back
    through the ``read_tool_result`` tool, so condensing never loses data.
    """

    enabled: bool = False
    """Master switch; also requires the Life layer itself to be enabled."""

    threshold_chars: int = 4000
    """Successful results shorter than this are left untouched."""

    max_input_chars: int = 20000
    """Only the head of a longer result is handed to the utility call."""

    intent_max_chars: int = 800
    """Tail of the model's own reasoning kept as the condensing intent."""

    keep_per_session: int = 20
    """Raw results kept per session; the oldest is dropped first."""

    max_sessions: int = 200
    """Sessions with a stash; the least recently used one is dropped first."""

    max_chars_per_session: int = 200_000
    """Character budget of one session's stash."""

    read_max_chars: int = 4000
    """Cap of one ``read_tool_result`` reply."""

    llm_timeout_seconds: float = 30.0
    """Hard timeout of the utility completion; on expiry the raw text is kept."""

    excluded_tool_names: list[str] = Field(
        default_factory=lambda: [
            "read_tool_result",
            "send_message_to_user",
            "send_meme",
        ]
    )
    """Control-signal tools whose result must never be rewritten."""


class SubAgentConfig(BaseModel):
    """Background sub-agent tasks (S6b).

    A sub-agent runs in the background with no parent context: its own system
    prompt, the task text and an optional tool subset. At most
    ``max_running`` tasks run at once, and a finished conclusion is delivered
    into the session instead of being sent as a message.
    """

    enabled: bool = False
    """Master switch; also requires the Life layer itself to be enabled."""

    max_running: int = 3
    """Tasks running at the same time, across all sessions."""

    max_running_per_session: int = 2
    """Tasks one session may run at the same time."""

    finished_keep: int = 20
    """Finished tasks kept in memory per session for ``subagent_status``."""

    timeout_seconds: float = 120.0
    """Hard timeout of one task; a timed-out task is marked failed."""

    max_turns: int = 6
    """Upper bound of model turns inside one sub-agent run."""

    tool_timeout_seconds: float = 30.0
    """Hard timeout of one tool call inside the sub-agent."""

    max_granted_tools: int = 6
    """Upper bound of tools the model may grant in one call."""

    conclusion_max_chars: int = 1500
    """Cap of the stored conclusion."""

    allowed_tools: list[str] = Field(default_factory=list)
    """Tools a sub-agent may use; empty means a text-only sub-agent."""

    inject_max_items: int = 3
    """Pending conclusions injected into one prompt."""

    inject_max_chars: int = 900
    """Cap of the injected ``<life_subagent>`` block."""

    notice_max_age_minutes: int = 120
    """Undelivered conclusions older than this are dropped."""

    inbox_max_per_session: int = 10
    """Undelivered conclusions kept per session; the oldest is dropped first."""


class MonitorConfig(BaseModel):
    """Planner monitoring and reasoning display (S6c).

    Records why a turn was answered or held, and what the model was thinking
    when it answered. Nothing is sent anywhere; the ring is read by the panel.
    """

    enabled: bool = False
    """Master switch; also requires the Life layer itself to be enabled."""

    recent_size: int = 100
    """Records kept in memory, oldest dropped first."""

    capture_reasoning: bool = True
    """Store a capped excerpt of the model's reasoning with each delivery."""

    reasoning_max_chars: int = 200
    """Cap of the stored reasoning excerpt; 0 stores none."""


class ReactionRule(BaseModel):
    """One keyword reaction: a fixed reply sent without calling the model."""

    keywords: list[str]
    """Texts to match; matched case-insensitively."""

    reply: str
    """Fixed reply sent when the rule matches."""

    exact: bool = False
    """Match the whole message instead of a substring."""

    cooldown_seconds: int = 300
    """Minimum gap between two replies of this rule in one session."""


class ReactionConfig(BaseModel):
    """Keyword reactions (S6c): deterministic replies to known triggers."""

    enabled: bool = False
    """Master switch; also requires the Life layer itself to be enabled."""

    rules: list[ReactionRule] = Field(default_factory=list)
    """Rules evaluated in order; the first match wins."""

    max_reply_chars: int = 200
    """Cap of the fixed reply."""


class AcpConfig(BaseModel):
    """ACP external agent (S6d).

    The bot launches one external agent process speaking ACP v1 over stdio.
    It can answer in-scope messages directly (``takeover``) or only be reachable
    through the ``ask_external_agent`` tool. Off by default; no command means no
    launch at all.
    """

    enabled: bool = False
    """Master switch; also requires the Life layer itself to be enabled."""

    command: str = ""
    """Executable of the ACP agent; empty keeps the adapter inert."""

    args: list[str] = Field(default_factory=list)
    """Command-line arguments."""

    env: dict[str, str] = Field(default_factory=dict)
    """Extra environment variables layered over the current environment."""

    cwd: str = ""
    """Working directory; empty means ``<life>/acp``."""

    timeout_seconds: float = 120.0
    """Hard timeout of one ACP request (handshake, session, prompt)."""

    max_output_chars: int = 4000
    """Cap of the text returned by one prompt."""

    takeover: bool = False
    """Answer in-scope messages directly instead of only exposing the tool."""


class FilesConfig(BaseModel):
    """Group file service: documents, downloads and temp cleanup (S6e).

    Files live under ``<life>/files``: ``out/`` holds generated documents the
    bot may send, ``tmp/`` holds scratch files that expire quickly. Only the
    extensions below may be written or downloaded.
    """

    enabled: bool = False
    """Master switch; also requires the Life layer itself to be enabled."""

    root: str = ""
    """Storage directory; empty means ``<life>/files``."""

    max_file_bytes: int = 2 * 1024 * 1024
    """Cap of one generated file."""

    max_send_bytes: int = 5 * 1024 * 1024
    """Cap of one file handed to the platform."""

    allowed_extensions: list[str] = Field(
        default_factory=lambda: [
            ".txt",
            ".md",
            ".json",
            ".csv",
            ".log",
            ".pdf",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".zip",
        ]
    )
    """Extensions allowed in the store."""

    max_files: int = 500
    """Files kept in ``out/``; the oldest are removed first."""

    keep_days: int = 7
    """Generated documents older than this are removed."""

    tmp_ttl_hours: int = 24
    """Scratch files older than this are removed."""

    cleanup_interval_minutes: int = 60
    """How often expired files are removed."""

    download_timeout_seconds: float = 15.0
    """Timeout of one ``download_file`` fetch."""


class HeartFlowConfig(BaseModel):
    """Heart-flow runtime (S4): debounce, gates, focus and planner loop."""

    enabled: bool = False
    """Master switch; also requires the Life layer itself to be enabled."""

    bot_names: list[str] = Field(default_factory=list)
    """Nicknames of the bot; a plain mention counts as addressing it."""

    debounce: DebounceConfig = Field(default_factory=DebounceConfig)
    gate: GateConfig = Field(default_factory=GateConfig)
    focus: FocusConfig = Field(default_factory=FocusConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    attention: AttentionConfig = Field(default_factory=AttentionConfig)
    idle: IdleConfig = Field(default_factory=IdleConfig)


class LifeConfig(BaseModel):
    """Root Life layer configuration."""

    enabled: bool = False

    data_dir: str = ""
    """Data directory; empty means ``data/life``. Relative paths use BOT_ROOT."""

    ingest: IngestConfig = Field(default_factory=IngestConfig)
    scope: ScopeConfig = Field(default_factory=ScopeConfig)
    sender: SenderConfig = Field(default_factory=SenderConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    person: PersonConfig = Field(default_factory=PersonConfig)
    expression: ExpressionConfig = Field(default_factory=ExpressionConfig)
    jargon: JargonConfig = Field(default_factory=JargonConfig)
    emoji: EmojiConfig = Field(default_factory=EmojiConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    reply: ReplyConfig = Field(default_factory=ReplyConfig)
    tool_result: ToolResultConfig = Field(default_factory=ToolResultConfig)
    subagent: SubAgentConfig = Field(default_factory=SubAgentConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    reaction: ReactionConfig = Field(default_factory=ReactionConfig)
    acp: AcpConfig = Field(default_factory=AcpConfig)
    files: FilesConfig = Field(default_factory=FilesConfig)
    heartflow: HeartFlowConfig = Field(default_factory=HeartFlowConfig)

    def resolved_data_dir(self, override: str | Path | None = None) -> Path:
        """Return the resolved Life data directory.

        Args:
            override: Explicit directory from the caller; highest priority.

        Returns:
            Absolute path of the data directory.
        """
        if override is not None:
            path = Path(override)
        elif self.data_dir:
            path = Path(self.data_dir)
        else:
            return Path(get_bot_data_path()) / LIFE_DIR_NAME
        if not path.is_absolute():
            path = Path(get_bot_root()) / path
        return path

    def session_enabled(self, umo: str, group_id: str | None = None) -> bool:
        """Return whether a session is taken over by the Life layer.

        Args:
            umo: unified_msg_origin of the session.
            group_id: Group id, or None for private sessions.

        Returns:
            True when the session is enabled.
        """
        if not self.enabled:
            return False
        if umo in self.scope.disabled_umos:
            return False
        if self.scope.mode == "all":
            return True
        if umo in self.scope.enabled_umos:
            return True
        if group_id and group_id in self.scope.enabled_group_ids:
            return True
        if self.scope.rollout_percent <= 0:
            return False
        return enabled_by_percent(
            self.scope.rollout_percent,
            umo,
            group_id,
            self.scope.rollout_salt,
        )


def default_config_path() -> Path:
    """Return the default configuration file path."""
    return Path(get_bot_data_path()) / LIFE_DIR_NAME / LIFE_CONFIG_FILE_NAME


def load_life_config(path: str | Path | None = None) -> LifeConfig:
    """Load the configuration, falling back to defaults on errors.

    Args:
        path: Configuration file path; defaults to ``data/life/config.json``.

    Returns:
        Loaded configuration, or defaults when the file is missing or invalid.
    """
    config_path = Path(path) if path is not None else default_config_path()
    if not config_path.exists():
        return LifeConfig()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error(f"[life] failed to read config, using defaults: {exc}")
        return LifeConfig()
    try:
        return LifeConfig.model_validate(raw)
    except ValidationError as exc:
        logger.error(f"[life] invalid config, using defaults: {exc}")
        return LifeConfig()


def save_life_config(config: LifeConfig, path: str | Path | None = None) -> Path:
    """Atomically write the configuration.

    Args:
        config: Configuration to persist.
        path: Target path; defaults to ``data/life/config.json``.

    Returns:
        The written path.

    Raises:
        OSError: When the file cannot be written or replaced.
    """
    config_path = Path(path) if path is not None else default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2)
    handle, tmp_name = tempfile.mkstemp(
        dir=str(config_path.parent),
        prefix=".life-config-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(payload)
            file.write("\n")
        os.replace(tmp_name, config_path)
    except BaseException:
        tmp_path = Path(tmp_name)
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    return config_path
