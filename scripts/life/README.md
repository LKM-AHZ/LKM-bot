# Life migration tooling (S0–S2)

Tools and runbook for the MaiBot → LKMBot migration. The plan lives in
`docs/MaiBot能力迁移到LKMBot方案.md` at the repository root.

## S0 — baseline corpus, replay and metrics

Four commands take a database to a baseline report. The corpus is **not
committed** (`data/` is gitignored).

```bash
# 1. corpus: 300 sessions x 50-200 messages, sanitized before sharing
python scripts/life/collect_replay.py --db data/data_v4.db --out data/life/replay \
  --sessions 300 --min-messages 50 --max-messages 200

# 2. corpus statistics (message volume, idle gaps, role and part mix)
python scripts/life/replay_stats.py data/life/replay/replay.jsonl \
  --json-out data/life/replay/corpus.json

# 3. one-click replay: corpus -> decision stream
python scripts/life/replay_runner.py data/life/replay/replay.jsonl \
  --decider keyword --out data/life/replay/decisions.jsonl

# 4. the five metrics (trigger, timing, memory, cost, P95)
python scripts/life/life_metrics.py \
  --corpus data/life/replay/replay.jsonl \
  --decisions data/life/replay/decisions.jsonl \
  --labels data/life/replay/labels.jsonl \
  --out data/life/replay/baseline.json
```

Run step 4 once per side of the comparison: write the MaiBot run to
`baseline_maibot.json`, the LKM-bot run to `baseline_lkm.json`, then check the
candidate against the reference (exit code 2 means a target failed):

```bash
python scripts/life/life_metrics.py --corpus data/life/replay/replay.jsonl \
  --decisions data/life/replay/decisions_lkm.jsonl \
  --labels data/life/replay/labels.jsonl \
  --reference data/life/replay/baseline_maibot.json \
  --out data/life/replay/baseline_lkm.json
```

### Decision stream

`replay_runner.py` emits one record per message. `message_id` is `m<index>`
inside its session, which is also what labels must use.

```json
{"session_key": "aiocqhttp:GroupMessage:123", "message_id": "m7", "ts": 1793947200.0,
 "decide": "reply", "reason": "wake_word", "latency_ms": 12.5,
 "tokens_in": 0, "tokens_out": 0, "memory_hits": 0, "cited": false}
```

Deciders: `keyword` (wake word, question mark), `silent` (never reply, the
floor), `always` (always reply, the ceiling) and `heart_flow` (the real S4
debounce + gate). To compare against MaiBot, import its decisions in the same
shape instead of re-running a decider.

### Labels

Human labels mark the reference answer for the same messages. Without this file
the trigger and timing blocks report zero coverage instead of a number.

```json
{"session_key": "aiocqhttp:GroupMessage:123", "message_id": "m7",
 "reply": true, "reply_ts": 1793947203.0, "cited": false}
```

### Exit conditions

| Condition | Status |
| --- | --- |
| Replay set replays with one command | done (`replay_runner.py`) |
| Metrics script produces numbers | done (`life_metrics.py`, 5 metrics) |
| 300 sessions x 50-200 messages | blocked: needs a production `data_v4.db`; the local dev DB has 57 messages in 1 session |
| MaiBot reference run | blocked: needs a MaiBot instance on the same model and prompt config |
| Human labels | blocked: annotation work, schema above |
| Metrics dashboard | deferred to the S6 panel; S0 delivers CLI + JSON output |
| Scope sign-off | blocked: D5 (MaiBot retention) and D6 (first platform) still open |

The local dev database validates the pipeline end to end but is not a baseline:
one webchat session, 57 messages, all `bot` role.

### Replay format

One JSON object per line:

```json
{
  "platform_id": "aiocqhttp",
  "session_key": "aiocqhttp:GroupMessage:123",
  "messages": [
    {"role": "user", "sender_id": "1", "sender_name": "A", "parts": [], "created_at": "..."}
  ]
}
```

## S1 — Life layer skeleton

Implemented in `bot/life/`:

| Module | Responsibility |
| --- | --- |
| `config.py` | `data/life/config.json`, defaults disabled, allowlist rollout |
| `ingest.py` | Normalize `BotMessageEvent` into an archive record |
| `models.py` | `life_messages`, `life_outbound` tables |
| `store.py` | Idempotent batch insert, stats, retention cleanup |
| `sender.py` | Proactive send through the platform API / plugin context |
| `session/registry.py` | Bounded per-session counters |
| `service.py` | Queue, worker, lifecycle, status |
| `pipeline.py` | `LifeIngestStage`, first in `STAGES_ORDER` |

`LifeIngestStage` runs before `WakingCheckStage`, so it sees messages that never
wake the bot. It never stops event propagation.

### Enable

Start once with defaults to generate `data/life/config.json`, then edit:

```json
{
  "enabled": true,
  "scope": {
    "mode": "allowlist",
    "enabled_umos": ["aiocqhttp:GroupMessage:123456"],
    "enabled_group_ids": [],
    "disabled_umos": []
  }
}
```

Restart the bot after changing `enabled`. Other fields (`ingest`, `sender`) are
hot-read at startup only in S1.

### Verify

```bash
pytest tests/life -q
curl -H "Authorization: Bearer <token>" http://127.0.0.1:6185/api/v1/life/status
curl -H "Authorization: Bearer <token>" "http://127.0.0.1:6185/api/v1/life/messages?limit=5"
```

Expected with the switch off: `enabled=false`, no writes to `life_messages`, and
pipeline behavior identical to upstream.

## S2 — long-term memory

Implemented in `bot/life/memory/`:

| Module | Responsibility |
| --- | --- |
| `models.py` | `life_memory`, `life_memory_cursor` tables |
| `store.py` | Dedupe insert, keyword/vector scoring, five search modes, export |
| `embedding.py` | LKM `ProviderType.EMBEDDING` bridge, timeout, breaker, health |
| `digest.py` | Messages → summaries, facts, episodes; per-session cursor |
| `adapter.py` | Stable facade: write, search, inject, digest, status |
| `inject.py` | `OnLLMRequestEvent` seam in `internal.py` |
| `tools.py` | `search_memory`, `remember`, `get_person_profile` |
| `upstream.py` | Optional in-process `a_memorix` engine bridge |
| `importer.py` | MaiBot JSONL / JSON / SQLite import with dry run |

Memory stays off unless both switches are on:

```json
{
  "enabled": true,
  "memory": { "enabled": true }
}
```

`scope` still decides which sessions get memory written, injected and exposed
through tools, so memory can be rolled out per session like the archive.

### Engine choice

`memory.upstream.engine` selects the storage engine:

- `local` (default) — built-in SQLite engine, no extra dependency.
- `a_memorix` — upstream `A-Dawn/A_memorix` 2.x package, wired through
  `NamespaceHostPorts(embedding_provider=...)`; falls back to `local` when the
  package is missing or fails to start.

Install the optional upstream engine (pinned to the audited commit) with:

```bash
pip install "a-memorix @ git+https://github.com/A-Dawn/A_memorix@28db65dd96f546f736d65c043a39254cbae2c4e9"
```

The bridge is lazy: without the package, `bot/life/memory/upstream.py` never
imports it and nothing changes at runtime.

### Maintenance CLI

Run against a stopped bot or a database copy:

```bash
python scripts/life/memory_tool.py stats
python scripts/life/memory_tool.py search --umo aiocqhttp:GroupMessage:1 --q 迁移
python scripts/life/memory_tool.py import --path mai_memory/ \
  --umo aiocqhttp:GroupMessage:1            # dry run
python scripts/life/memory_tool.py import --path mai_memory/ \
  --umo aiocqhttp:GroupMessage:1 --apply    # write
python scripts/life/memory_tool.py export --umo aiocqhttp:GroupMessage:1 \
  --out data/life/memory/export.json
python scripts/life/memory_tool.py selfcheck
python scripts/life/memory_tool.py bench --rows 1000   # local search latency
```

`import` understands JSONL, JSON arrays and SQLite tables (columns are matched by
name). LPMM Parquet exports are not parsed by this CLI: convert them with the
upstream `a-memorix[lpmm]` tooling first, then import the resulting JSONL.

### Verify

```bash
pytest tests/life -q
curl -H "Authorization: Bearer <token>" http://127.0.0.1:6185/api/v1/life/memory
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:6185/api/v1/life/memory/search?umo=<umo>&q=迁移"
```

## S3a — people, facts and profiles

Implemented in `bot/life/person/`:

| Module | Responsibility |
| --- | --- |
| `names.py` | Name cleaning, alias normalization, person id derivation |
| `models.py` | `life_person`, `life_person_fact`, `life_person_profile` |
| `store.py` | Observe/upsert, relationship decay, fact conflicts, decay, delete |
| `service.py` | Batch observation, fact ingest, profile build/cache, review, forget |

Enable it together with the Life layer:

```json
{
  "enabled": true,
  "person": { "enabled": true }
}
```

Facts arrive from the memory digest (`memory.enabled` + `memory.digest.enabled`):
each extracted fact is attached to a person, classified as `preference` /
`identity` / `relation` / `other`, and stored with its confidence.

### Lifecycle

- **学习**: archive flush → `observe` (aliases, relationship score, interaction
  count); digest → facts per person.
- **冲突**: two facts of the same category whose token overlap reaches
  `conflict_similarity` (default 0.6) are both marked `conflicted` and share a
  group id, waiting for review.
- **复审**: `PATCH /api/v1/life/people/fact` with `{"fact_id": N, "keep": true}`
  keeps one fact and supersedes its rivals; `keep: false` rejects it.
- **可解释**: every profile returns `fact_ids`, and each fact carries its
  category, effective confidence and status.
- **可删除**: `DELETE /api/v1/life/people?umo=...&person=...` removes the person
  with facts and profile; `&fact_id=N` removes a single fact.
- **衰减**: effective confidence halves every `confidence_half_life_days`
  (21 by default); the 6-hour maintenance task marks facts below
  `min_confidence` as `expired`.

### Verify

```bash
pytest tests/life -q
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:6185/api/v1/life/people?umo=<umo>"
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:6185/api/v1/life/people/profile?umo=<umo>&person=Alice"
```
## S3b — learned expressions

Implemented in `bot/life/expression/`:

| Module | Responsibility |
| --- | --- |
| `models.py` | `life_expression`, `life_expression_cursor` |
| `store.py` | Dedupe upsert, weight reinforcement, scene filter, delete |
| `learn.py` | Heuristic mining + LLM extraction, cursor per session |
| `index.py` | FAISS index over expressions, reusing `vec_db/faiss_impl` |
| `select.py` | Similarity + decayed weight + scene scoring, prompt block |
| `service.py` | Learn round, index sync, injection, status |

```json
{
  "enabled": true,
  "expression": { "enabled": true, "use_llm": false }
}
```

`use_llm=false` learns from message punctuation and repeated emphatic phrases
only, so it works with no model budget. `use_llm=true` adds a batched extraction
call that produces richer `(situation, style)` pairs. `use_vector=true` keeps a
FAISS index under `data/life/expression/`; when embeddings are unavailable the
selector falls back to keyword scoring and nothing breaks.

### Lifecycle

- **学习**: every `learn_interval_minutes` the learner reads the archive from its
  cursor. Heuristic candidates need 2–16 characters, a tone marker and
  `min_frequency` repetition; the surrounding sentence becomes the situation.
- **入库**: `(session_key, checksum)` dedupes; a repeated sighting adds weight
  instead of a new row.
- **命中**: `0.7 * similarity + 0.3 * decayed_weight`, filtered by `min_score`
  and scene, capped at `top_k`, appended to the prompt as `<life_expression>`.
- **可解释 / 可删除**: `GET /api/v1/life/expressions?umo=...` lists every learned
  phrase with weight, usage count and source; `DELETE` removes by session or id,
  and drops the rows from the index.

### Verify

```bash
pytest tests/life -q
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:6185/api/v1/life/expressions?umo=<umo>"
```
## S3c — group slang

Implemented in `bot/life/jargon/`:

| Module | Responsibility |
| --- | --- |
| `models.py` | `life_jargon`, `life_jargon_cursor` |
| `store.py` | Term dedupe, conflict → `pending`, review, usage, delete |
| `learn.py` | Definition-pattern mining + LLM extraction, cursor per session |
| `service.py` | Learn round, contextual explanation, review, status |

```json
{
  "enabled": true,
  "jargon": { "enabled": true, "use_llm": false }
}
```

### Rules

- **只学有释义的词**：`X 就是 Y`、`X 指的是 Y`、`Y 简称 X`，引号内的单字词也算
  （`「典」就是很典型的例子`）。裸词不入库。
- **同词异义不覆盖**：第二种释义会让整条转 `pending` 并降置信度，等人工
  `PATCH /api/v1/life/jargon` 复审（可同时改写释义），避免学错就到处用。
- **命中文才解释**：只有当前 prompt 里真的出现该词，才注入一行释义，并明确写
  “不要主动使用没把握的黑话”。因此不会把词表硬塞进每次回复。
- **可删除**：`DELETE /api/v1/life/jargon`（按 umo 或 ids）。

### Verify

```bash
pytest tests/life -q
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:6185/api/v1/life/jargon?umo=<umo>&status=pending"
```
## S3d — stickers

Implemented in `bot/life/emoji/`:

| Module | Responsibility |
| --- | --- |
| `models.py` | `life_emoji`, `life_emoji_cursor` |
| `store.py` | Content-hash dedupe, usage, tags, prune, delete |
| `collect.py` | Archive images → files on disk (http(s) / local path) |
| `select.py` | Tag or vector similarity + popularity + send budget |
| `service.py` | Collection round, pick, send via `LifeSender`, status |
| `tools.py` | `send_meme` agent tool |

```json
{
  "enabled": true,
  "emoji": { "enabled": true, "max_per_hour": 3, "cooldown_seconds": 300 }
}
```

- **采集**：从归档图片组件取 http(s) 或本地路径，SHA-256 去重后存到
  `data/life/emoji/<session>/<hash>.<ext>`；`base64://` 形式的图片归档时已被折叠，无法恢复，直接跳过。
- **标签**：取自图片所在消息的文本（可 `PATCH` 改），向量可选（与记忆共用 embedding 配置）。
- **发送**：模型调用 `send_meme` 工具；服务端按 `0.8×相似度 + 0.2×热度` 选图，再受
  `cooldown_seconds` 与 `max_per_hour` 两道进程内闸门限制，经 `LifeSender` 发出并记录 `life_outbound`。
- **可删除**：`DELETE /api/v1/life/emojis` 会同时删库行与磁盘文件。

### Verify

```bash
pytest tests/life -q
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:6185/api/v1/life/emojis?umo=<umo>"
```

## S4a — heart-flow decision path

Implemented in `bot/life/heartflow/`:

| Module | Responsibility |
| --- | --- |
| `debounce.py` | Merge a burst into one turn: three tiers, 8s hard cap, @ acceleration, same-sender rule |
| `gates.py` | Reply likelihood over nine features + dynamic threshold and vetoes |
| `policy.py` | Debounce + gate over one session or a whole corpus |

```json
{
  "enabled": true,
  "heartflow": {
    "enabled": true,
    "debounce": { "quick_seconds": 1.2, "natural_seconds": 2.5, "calm_seconds": 4.0 },
    "gate": { "base_threshold": 0.55 }
  }
}
```

`heartflow.enabled` is the master switch: S4c turns the takeover on, S4b adds
the focus timeline (`heartflow.focus.enabled`), and S4a provides the decision
path the replay tooling uses.

### Rules

- **防抖**：窗口按三档取（快速 1.2s / 自然 2.5s / 沉稳 4.0s），从首条消息起 8 秒硬上限；
  被 @ / 唤醒 / 回复机器人时走快速档；同一个人连发时窗口只加一次 1 秒。
- **门控**：九个特征加权求和过 sigmoid，`at_bot`/`reply_to_bot`/`wake_word` 直接放行；
  命令不接；刚回过 3 秒内不接；`idle_silence_seconds`（默认 900s）内没有机器人活动的会话
  一律沉默，保证“空闲会话 0 误触发”。
- **动态阈值**：空闲越久阈值越高（封顶 +0.25），连续沉默次数越多阈值越高（封顶 +0.32）。
- **可解释**：决策流的每条记录都带 `reason`、`tier`、`likelihood`、`threshold`，
  可以直接定位是哪条规则做的决定。

### Verify

```bash
pytest tests/life -q

# the real decision path produces the decision stream
python scripts/life/replay_runner.py data/life/replay/replay.jsonl \
  --decider heart_flow --gate-threshold 0.55 \
  --out data/life/replay/decisions_heart_flow.jsonl

# then score it like any other run
python scripts/life/life_metrics.py --corpus data/life/replay/replay.jsonl \
  --decisions data/life/replay/decisions_heart_flow.jsonl \
  --labels data/life/replay/labels.jsonl \
  --out data/life/replay/baseline_lkm.json
```

Tuning knobs on the runner: `--gate-threshold` (likelihood bar), `--no-debounce`
(decide every message immediately), `--bot-ids` (which `at` targets are the bot;
empty treats every `at` as addressing the bot) and `--wake-words`.

## S4b — focus timeline, compression and checkpoints

Implemented in `bot/life/heartflow/focus/`:

| Module | Responsibility |
| --- | --- |
| `timeline.py` | Pure model: turns by idle gap, reply closes a turn, folding into a digest, checkpoint/restore |
| `models.py` | `life_focus` (digest + counters) and `life_focus_turn` (one row per turn) |
| `store.py` | Upsert turns, save the digest, prune, list, delete, stats |
| `service.py` | Archive hook, reply hook, checkpoint loop, prompt block, status |

```json
{
  "heartflow": {
    "enabled": true,
    "focus": {
      "enabled": true,
      "keep_turns": 12,
      "gap_seconds": 300,
      "digest_max_chars": 1500
    }
  }
}
```

### Rules

- **回合化**：连续消息按 `gap_seconds`（默认 300 秒）归为一个回合；机器人回复（`send_text` 成功后）
  立即收尾回合。归档里没有机器人消息，所以普通回复要等 S4c 通过 `note_reply()` 回写。
- **有界**：内存里只留最后 `keep_turns` 个回合原文；更旧的折进摘要。摘要按行累积，超过
  `digest_max_chars` 先丢最旧的行并加“更早的内容已省略”标记，长度永远有界。
- **可恢复**：`(session_key, turn_index)` 唯一，回合按索引 upsert；`_focus_loop` 每
  `checkpoint_interval_minutes` 把脏时间线写回库，重启后直接重建，不重放归档。
- **注入只给折叠摘要**：最近的对话已经在模型上下文里，`<life_focus>` 只补充更早的压缩摘要，
  不重复占 token；没有折叠过就完全不注入。
- **可删除**：`DELETE /api/v1/life/focus?umo=...` 清掉一个会话的时间线与摘要。

### Verify

```bash
pytest tests/life -q
curl -H "Authorization: Bearer <token>" http://127.0.0.1:6185/api/v1/life/focus
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:6185/api/v1/life/focus?umo=<umo>"
```

## S4c — runtime takeover

Implemented in `bot/life/heartflow/runtime.py` and `LifeTurnStage`:

| Piece | Responsibility |
| --- | --- |
| `runtime.py` | Turn leader, merging, debounce wait, gate decision, shadow mode, capacity |
| `LifeTurnStage` | Holds ambient group messages, passes the leader through with a forced wake |
| `WakingCheckStage` seam | Honours `_life_force_wake` so an unaddressed group turn can reach the agent |

```json
{
  "heartflow": {
    "enabled": true,
    "bot_names": ["小助手", "bot"],
    "runtime": { "enabled": true, "shadow": false, "max_pending_turns": 32 }
  }
}
```

### What it takes over

Only **ambient group messages** — a group message that is not `@`, not a reply to
the bot, not wake-prefixed, not a `/` command and not a button interaction. Every
addressed message keeps the upstream path, so plugins, commands, session control
and private chats behave exactly as before.

### How a turn is answered

- The first message of a session becomes the **turn leader** and waits for the
  debounce window (three tiers, 8s hard cap, plus `max_wait_seconds` as a guard
  against odd timestamps).
- New messages join the same turn and extend the window — that is the
  "steering/interrupt" layer.
- When the window closes the leader scores the merged turn with the **same gate
  code the replay set uses**. `wait` stops its own event (the message is already
  archived, nothing is sent). `reply` writes the merged text back to
  `event.message_str`, sets `_life_force_wake`, and the pipeline continues into
  the normal agent path.
- Every other message of the turn is stopped: its text is already in the merged
  message.

### Shadow rollout

`runtime.shadow=true` still merges turns, decides and records, but never forces a
wake. Use it to compare `would_reply` against what the bot does today:

```bash
curl -H "Authorization: Bearer <token>" http://127.0.0.1:6185/api/v1/life/status
# heartflow.counters + heartflow.recent[].decide/reason/likelihood/threshold
```

### Verify

```bash
pytest tests/life -q

# the stage itself, plus the forced-wake seam in both directions
pytest tests/life/test_life_heartflow_stage.py -q
```

With `heartflow.runtime.enabled=false` or `heartflow.enabled=false` the stage
returns immediately and the pipeline is byte-for-byte the upstream one.

## S4d — attention, proactive talk and fast reactions

Implemented in `bot/life/heartflow/{attention,idle,proactive}.py` plus the
`life_talk` table:

| Module | Responsibility |
| --- | --- |
| `attention.py` | Focus decay, drift band, `<life_attention>` back-reference hint |
| `idle.py` | Talk value, exponential backoff, quiet hours, candidate vetting |
| `proactive.py` | Idle scan, LLM opener, send through `LifeService.send_text`, bookkeeping |
| `models.py` / `store.py` | `life_talk`: talk value, attempts, daily budget, pending answer |

```json
{
  "heartflow": {
    "enabled": true,
    "attention": { "enabled": true, "half_life_minutes": 40 },
    "idle": { "enabled": false, "min_idle_minutes": 45, "daily_cap": 2 }
  }
}
```

### Attention drift and back-references

Every answered turn sets the session focus to 1, decaying with
`half_life_minutes`. Inside the drifted band (`hook_focus_min..hook_focus_max`)
and after `hook_min_gap_minutes` of silence, the prompt gets one line: what the
conversation was about, how long ago, and an explicit "only hook back if it
actually fits". Below `min_focus` the topic is forgotten — that is the drift.
The state is in memory only; a hint that survived a restart would be stale.

### Proactive talk (off by default)

All of these must hold before the bot speaks first:

| Gate | Default |
| --- | --- |
| Quiet session | 45–720 minutes |
| Talk value | ≥ 0.55 (starts at 0.5, +0.1 per answered turn, −0.2 per ignored opener) |
| Daily budget | 2 per session |
| Quiet hours | local 00:00–08:00 |
| Backoff after ignoring | 120 min × 2^(n-1), capped at 1440 min |
| Awaiting an answer | no follow-up until `answer_window_minutes` (30) passes |

The opener is always generated by the chat provider from the focus digest and
the last few messages; with no provider the whole round is skipped, so the bot
never sends a canned "在吗".

### Fast reactions

- `runtime.hot_window_seconds` (60): a session the bot just answered uses the
  quick debounce tier (1.2s) instead of the natural one (2.5s).
- `runtime.fast_max_chars` (8): a very short turn ("在吗", "?") uses the quick
  tier and is released early.
- Token-level interruption of an in-flight answer stays with the upstream
  follow-up machinery; the heart-flow interrupts at the debounce layer.

### Verify

```bash
pytest tests/life -q
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:6185/api/v1/life/attention"
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:6185/api/v1/life/attention?umo=<umo>"
```

`/api/v1/life/attention` returns the attention snapshot, the proactive status
and, with `umo`, that session's talk value.

## S5a — persona profiles

Implemented in `bot/life/persona/`:

| Module | Responsibility |
| --- | --- |
| `models.py` | `life_persona_profile`: one row per upstream `persona_id`, empty id = default profile |
| `store.py` | Upsert with rate clamping, read, delete, list, stats |
| `service.py` | Profile resolution + cache, `<life_persona>` rendering, prompt injection, panel CRUD |

```json
{
  "persona": {
    "enabled": true,
    "default_quote_rate": 0.2,
    "default_multi_segment_prob": 0.15,
    "default_typo_rate": 0.02,
    "reply_max_chars": 120,
    "block_max_chars": 600,
    "cache_ttl_seconds": 300
  }
}
```

The profile itself lives in the Life database, keyed by the same `persona_id` the
upstream persona manager resolves; the upstream `Persona` table is never read or
written. With no row for the active persona the service falls back to the row
with an empty `persona_id`, so a deployment that never configured a persona still
gets one.

### Block shapes

The block is **prepended** to the system prompt (persona is the base stance;
memory, expressions, slang, focus and attention then read as supporting material):

```text
<life_persona>
你的人设补充（优先于一般风格要求，但不要复述这份说明）：
<base_prompt>
<group_prompt | private_prompt>
- 长度：单条回复不超过 N 字，能一句话说完就别展开。
- 分段：约 X% 的回复拆成两三条短消息，其余一次说完。
- 引用：约 Y% 的回复引用你要回的那条消息，其余直接说话。
- 口吻：口语，允许偶尔打错字（约 Z%），但不影响理解。
- 格式：不要输出 Markdown（标题、列表、表格、代码块、加粗）。
</life_persona>
```

Rules with a zero rate are dropped (an explicit `0.0` means "never", it does not
fall back to the default); `allow_markdown=true` drops the format line; the three
prompt sections share `block_max_chars`.

### Rules

- Scene picks the prompt: a group message uses `group_prompt`, a private message
  uses `private_prompt`; `base_prompt` always applies.
- Resolution is cached for `cache_ttl_seconds`; any write or delete clears the
  whole cache, so a panel edit takes effect on the next turn.
- The block is skipped for sessions outside the Life allowlist and when the tag
  is already present in the prompt.
- S5a only *tells* the model how to talk. Honouring the quote/segment/typo rates
  in the actual outbound text is S5c's post-processing; Markdown stripping is
  S5c too.

### Verify

```bash
pytest tests/life -q

# list, read, upsert and delete one profile (``default`` = empty persona_id)
curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:6185/api/v1/life/persona
curl -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -X PUT http://127.0.0.1:6185/api/v1/life/persona/default \
  -d '{"base_prompt":"说话短一点。","quote_rate":0.3,"reply_max_chars":60}'
curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:6185/api/v1/life/persona/default
curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:6185/api/v1/life/status  # persona block
```

## S5b — prompt assembly

Implemented in `bot/life/prompt.py`:

| Piece | Responsibility |
| --- | --- |
| `PromptAssembler` | Orders blocks, dedupes repeated lines, caps each block and the total, records the result |
| `INJECTOR_ORDER` (in `inject.py`) | Persona, memory, expression, jargon, focus, attention — the order blocks are appended |
| `/life/status` `prompt` | Budgets, counters and the last assembly (block names, sizes, what was dropped) |

```json
{
  "prompt": {
    "enabled": true,
    "max_total_chars": 2400,
    "max_block_chars": 800,
    "dedupe": true
  }
}
```

The injectors keep deciding whether their block applies; the assembler runs once
after all of them and owns size and order.

### Rules

- **顺序固定**：persona 在前，其后 memory、expression、jargon、focus、attention。非块文本
  （上游自己的系统提示）保持原位，不重排。
- **去重只去整行**：与更早块完全相同、且 ≥ 8 字的行会被删掉；短行（“- 晴”）保留。
  宁可多留一行，也不猜哪一行“意思重复”。
- **单块按行截断**：`max_block_chars` 超了从行边界切，不切半句；persona 的规则顺序是
  「长度 → 禁 Markdown → 分段 → 引用 → 口吻」，所以截断先丢概率，硬约束还在。
- **总预算整块丢弃**：超过 `max_total_chars` 从 attention 往上整块丢，persona 永不丢；全丢完
  还超就截断 persona。丢块后把连续空行折成一行。
- **可观测**：每次组装更新 `prompt.last`（`umo`、总字数、每块字数、保留与丢弃清单），
  计数器记 `assemblies` / `kept_blocks` / `dropped_blocks` / `trimmed_blocks` / `deduped_lines`。

### Verify

```bash
pytest tests/life -q

curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:6185/api/v1/life/status  # prompt.counters + prompt.last
```

## S5c — reply post-processing

Implemented in `bot/life/reply/postprocess.py` plus a 3-line call at the end of
`ResultDecorateStage`:

| Step | What it does | Switch |
| --- | --- | --- |
| Markdown | Fences, inline code, links, headings, bold/italic, bullets, tables → plain text | `strip_markdown` (skipped when the profile allows Markdown) |
| Typo | At most one swap/doubling between adjacent Chinese characters | `nudge_typos` (rate from the profile) |
| Length | Cut at the last sentence end inside `reply_max_chars` | `cap_length` (budget from the profile) |
| Split | At most `max_parts` messages by `multi_segment_prob` | `split_segments`; skipped when upstream segmented reply is on |
| Quote | Insert `Reply` to the triggering message with `quote_rate` | `quote_replies` + `quote_platforms` |

```json
{
  "reply": {
    "enabled": false,
    "strip_markdown": true,
    "nudge_typos": true,
    "cap_length": true,
    "split_segments": true,
    "quote_replies": true,
    "quote_platforms": ["aiocqhttp"],
    "min_split_chars": 24,
    "max_parts": 3,
    "segment_delay_seconds": 1.2
  }
}
```

`reply.enabled` is off by default and does nothing without the persona layer
(`life.persona.enabled`), because the rates and the length budget come from the
persona profile. With `life.reply.enabled=false` the seam returns immediately and
the outbound message is byte-for-byte the upstream one.

### Rules

- **只改已有文字**：不动模型没说的内容。剥掉的 Markdown 不补回来，截断的尾巴不加省略号，
  错字只换/叠相邻汉字。写不出“补充一句”或“文档回复”，那需要一个模型回合，不在这一步。
- **只处理非流式 LLM 结果**：命令、插件输出、流式增量、runner 错误一律跳过。
- **分段优先级**：上游 `platform_settings.segmented_reply.enable=true` 时，S5c 只做文本变换、
  不再拆段（否则两层切分会把一句话切碎）。上游关闭时，首段随管线发送，尾段由后台任务按
  `segment_delay_seconds` 逐条发送。
- **引用只放到支持平台**：默认 `aiocqhttp`；`reply_with_quote` 已经开启时不会再加一条。
- **失败不阻断**：任何一步抛异常都只记日志，回复照常发出。

### Verify

```bash
pytest tests/life -q
pytest tests/life/test_life_reply.py -q   # 17 cases: transforms + processor + counters

curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:6185/api/v1/life/status  # reply.counters
```

The counters (`processed`, `markdown_stripped`, `typos`, `capped`, `split`,
`quoted`, `skipped`) are the acceptance evidence for the S5d distribution check:
a Markdown leak would show up as a non-zero `markdown_stripped` after the fact,
and a split/quote rate far from the profile value means the profile or the seed
is wrong.

## S5d — style metrics and acceptance gates

Implemented in `bot/life/reply/style.py`; the CLI is
[`style_metrics.py`](style_metrics.py).

```bash
# 1. from a sample of outbound messages (one JSON object per line)
python scripts/life/style_metrics.py --sample data/life/style/sample.jsonl \
  --out data/life/style/report.json

# 2. from a saved /api/v1/life/status payload (reply.recent ring, no text stored)
python scripts/life/style_metrics.py --status data/life/style/status.json

# 3. against the reference corpus, with the quote-rate gate
python scripts/life/style_metrics.py --sample data/life/style/sample.jsonl \
  --reference data/life/style/mai_bot.jsonl \
  --expected-quote-rate 0.2 --quote-tolerance 0.1 \
  --out data/life/style/report.json
```

Sample line: `{"text": "...", "segments": 1, "quoted": false, "umo": "..."}`.
The length is taken from `text` when present, otherwise from `chars` (the ring
only keeps `chars`).

### What it reports

- **长度**：均值 / 中位 / P90 / 最大（字符数）。
- **段数**：均值 / P90 / 多段占比（`segments > 1`）。
- **引用率**：`quoted` 为真的比例。
- **Markdown 泄漏**：没有 `leak` 标记时按正文重新扫一遍（标题、`**`、围栏、表格、链接）。
- **KS**：`--reference` 存在时对长度做两样本 Kolmogorov-Smirnov（纯 Python，含
  `sqrt(ne) + 0.12 + 0.11/sqrt(ne)` 小样本修正），输出 `D`、`p` 与是否显著。

### Gates (exit codes)

| 退出码 | 含义 |
| --- | --- |
| 0 | 通过 |
| 2 | 长度分布与对照显著不同（`p < --alpha`，默认 0.05） |
| 3 | 存在 Markdown 泄漏（一条也算失败） |
| 4 | 引用率超出 `--expected-quote-rate ± --quote-tolerance` |

优先级 2 > 3 > 4：先看分布，再看泄漏，最后看引用率。报告同时写进 `--out` 指定的 JSON。

### Generate a sample from the live ring

```bash
curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:6185/api/v1/life/status > data/life/style/status.json
python scripts/life/style_metrics.py --status data/life/style/status.json
```

The ring keeps the last 50 replies with `chars`, `segments`, `quoted`,
`markdown`, `leak`, `typo` and `capped` - no reply text, so the check can run
against production without turning the status endpoint into a chat log.

### Status of the reference side

The gate is only as honest as the reference file. Until the S0 corpus and a
MaiBot same-model run exist, `--reference` is fed a hand-built sample; that
proves the test catches a shifted distribution, not that S5 matches MaiBot.
Once the corpus lands, the command line does not change.

## S6a — tool-result condensation

Implemented in `bot/life/toolresult/`:

| Module | Responsibility |
| --- | --- |
| `condense.py` | Threshold / exclusion rules, self-contained task text, the utility call, the condensed wrapper |
| `store.py` | Bounded per-session stash of raw results (`r1`, `r2`, ...) |
| `read.py` | `read_tool_result` tool plus the pure `render_read_view` formatter |

```json
{
  "tool_result": {
    "enabled": false,
    "threshold_chars": 4000,
    "max_input_chars": 20000,
    "intent_max_chars": 800,
    "keep_per_session": 20,
    "max_sessions": 200,
    "max_chars_per_session": 200000,
    "read_max_chars": 4000,
    "llm_timeout_seconds": 30.0,
    "excluded_tool_names": ["read_tool_result", "send_message_to_user", "send_meme"]
  }
}
```

The seam sits at the end of `ToolLoopAgentRunner._materialize_large_tool_result`:
when the Life layer is off the method behaves exactly as before, and when it is
on but declines (short result, excluded tool, out-of-scope session, failed
utility call) the upstream overflow-to-file path still applies.

### What gets condensed

- Only results at or above `threshold_chars`; shorter ones cost no model call.
- The `send_*` control tools, `read_tool_result` itself and anything listed in
  `excluded_tool_names` are never rewritten.
- The utility call sees the head of the result (`max_input_chars`) and the tail
  of the model's own reasoning before the call (`intent_max_chars`), nothing
  else - it has no parent context.
- A summary that is empty, not shorter than the raw text, or returned after
  `llm_timeout_seconds` is discarded and the raw text is kept.

### What the model sees

```text
<压缩后信息；原始结果已保存为 r3。需要上面没有的细节时，调用 read_tool_result(result_id="r3") 分段读取原文，或加 keyword 参数直接查找。>
<summary>
```

### Raw stash and `read_tool_result`

- Kept per session, in memory only: a restart clears it.
- Bounds: `keep_per_session` results, `max_chars_per_session` characters, and
  `max_sessions` sessions (least recently used session is dropped first).
- `read_tool_result(result_id, keyword?, offset?, limit?)`:
  - `keyword` returns matching lines as `L12: ...`;
  - `offset` (1-based) + `limit` page through the original;
  - unknown id returns the kept ids, and another session's id is invisible.

### Verify

```bash
pytest tests/life/test_life_tool_result.py -q
pytest tests/test_tool_loop_agent_runner.py -q -k large_tool_result

# status block (counters + stash size)
curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:6185/api/v1/life/status | jq .data.tool_result
```

### Costs and limits

- Condensing blocks the tool turn for one utility completion; a provider outage
  turns it into `failed` counters with the raw text kept, not an error.
- The stash lives in process memory, so a multi-process deployment condenses in
  one worker and can only read that worker's stash.

## S6b — background sub-agents

Implemented in `bot/life/subagent/`:

| Module | Responsibility |
| --- | --- |
| `models.py` | `SubAgentTask` / `SubAgentOutcome` |
| `tasks.py` | Bounded registry: capacity, timeout, stop, finished-task retention |
| `runner.py` | One bounded tool loop with no parent context; per-call tool timeout |
| `inbox.py` | Per-session inbox of undelivered conclusions |
| `notify.py` | `<life_subagent>` prompt block |
| `tools.py` | `subagent`, `subagent_status`, `subagent_stop` |

```json
{
  "subagent": {
    "enabled": false,
    "max_running": 3,
    "max_running_per_session": 2,
    "finished_keep": 20,
    "timeout_seconds": 120.0,
    "max_turns": 6,
    "tool_timeout_seconds": 30.0,
    "max_granted_tools": 6,
    "conclusion_max_chars": 1500,
    "allowed_tools": [],
    "inject_max_items": 3,
    "inject_max_chars": 900,
    "notice_max_age_minutes": 120,
    "inbox_max_per_session": 10
  }
}
```

### What a sub-agent is

- A **separate** agent run: it sees only the task text and its own system prompt,
  never the parent conversation, and it cannot ask the user anything.
- With `allowed_tools` empty it is text-only. Otherwise the model may pass a
  comma-separated subset; it is intersected with `allowed_tools`, capped by
  `max_granted_tools`, and resolved from the live tool manager. Granting is
  explicit: a tool not in `allowed_tools` is never available.
- The sub-agent uses a minimal executor: it calls the granted tool and sees its
  text result, but it never sends messages, registers background tasks or writes
  chat history.

### Lifecycle

```bash
# model view
subagent(description="查一下 pixiv 插件的最新版本", tools="web_search")
# -> started: subagent-1 (background) ...
subagent_status(task_id="subagent-1")
# -> [subagent-1] completed (12.4s) ... tools=web_search conclusion=...
subagent_stop(task_id="subagent-1")
```

- Capacity: `max_running` process-wide and `max_running_per_session` per chat;
  over the limit the tool returns `error: too many sub-agent tasks are running`.
- `timeout_seconds` bounds the whole run; `tool_timeout_seconds` bounds one tool
  call. A failed run records `error` instead of raising into the chat.
- Finished tasks stay queryable per session (`finished_keep`), oldest dropped.

### Delivery

A finished conclusion is **not** sent as a message. It is delivered the way the
bot's event-driven agent allows:

1. If the session still has an active agent run, the conclusion is appended as a
   follow-up (`agent_runner.follow_up`), a real wake: the model sees it at its
   next tool result.
2. Otherwise it lands in the session inbox, and the next prompt carries:

```text
<life_subagent>
后台子任务已经完成，结论可以直接使用，不必再派发同一件事：
- [subagent-1] 查一下 pixiv 插件的最新版本：结论：pixiv 插件最新版本 3.2.1。
</life_subagent>
```

3. If the active run never consumed the follow-up, a background check moves the
   conclusion into the inbox instead, so it is not lost.
4. Undelivered conclusions expire after `notice_max_age_minutes`.

### Verify

```bash
pytest tests/life/test_life_subagent.py -q
pytest tests/life -q

curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:6185/api/v1/life/status | jq .data.subagent
```

### Limits

- The agent is message-driven: a background task cannot start a new turn, so
  "wake" only exists while a run is active. Otherwise the conclusion waits for
  the next user message.
- Inbox delivery is consumed on injection; if the provider call then fails, the
  conclusion is not retried.

## S6c — planner monitoring, reasoning display and keyword reactions

Two independent pieces, both default off:

| Module | Responsibility |
| --- | --- |
| `bot/life/monitor/recorder.py` | Bounded ring of heart-flow decisions and delivered replies |
| `bot/life/reaction/service.py` | Keyword matching, per-session cooldowns, fixed reply |
| `bot/life/pipeline.py` (`LifeReactionStage`) | Answers a matched rule and stops the event |
| `bot/dashboard/api/life.py` (`GET /life/monitor`) | Recent records for the panel |

```json
{
  "monitor": {
    "enabled": false,
    "recent_size": 100,
    "capture_reasoning": true,
    "reasoning_max_chars": 200
  },
  "reaction": {
    "enabled": false,
    "rules": [
      {"keywords": ["典"], "reply": "确实典", "exact": false, "cooldown_seconds": 300}
    ],
    "max_reply_chars": 200
  }
}
```

### What monitoring records

The host is event-driven, so "the planner" is observed at two moments:

- **decision** (`LifeTurnStage`): action (`continue` / `hold`), whether the gate
  decided to answer, `likelihood`, `threshold`, reason string.
- **delivery** (`postprocess_life_reply`): character count, part count and a
  capped excerpt of `_llm_reasoning_content` (the model's own reasoning, when
  the provider returns it). Recording happens even when reply post-processing is
  disabled.

No message text is stored. The ring keeps `recent_size` records in memory and
counters (`decisions`, `replies`, `waits`, `deliveries`, `dropped`).

### Read the records

```bash
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:6185/api/v1/life/monitor?limit=20"
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:6185/api/v1/life/monitor?session=aiocqhttp:GroupMessage:1"
```

`/life/status` also carries a compact `monitor` block (counters + last record).

### Keyword reactions

- Rules are evaluated in order and the first match wins; matching is
  case-insensitive, substring by default, whole-message with `exact: true`.
- A match sends the fixed reply through the normal sender (so it is recorded as
  an outbound message) and **stops the event**: the model never sees that
  message. Commands (`/`) are never matched.
- Cooldown is per rule per session; a failed send does not start the cooldown,
  so the next message can retry.
- Only plain text is supported; stickers/images are S6e (file service).

### Verify

```bash
pytest tests/life/test_life_monitor.py tests/life/test_life_reaction.py -q
pytest tests/life -q
```

### Limits

- The monitor ring and the cooldown table are process memory: restart clears
  both, and a multi-worker deployment shows each worker only its own records.
- Reasoning excerpts may still contain user text; they are capped but not
  redacted, so the panel must stay behind the system-scope auth.

## S6d — ACP external agent

Implemented in `bot/life/acp/`:

| Module | Responsibility |
| --- | --- |
| `protocol.py` | NDJSON JSON-RPC framing, request/response matching, peer requests |
| `client.py` | Subprocess transport, `initialize` handshake, sessions, prompts |
| `service.py` | One agent process, one ACP session per chat, counters, reset on failure |
| `tools.py` | `ask_external_agent` tool for the local model |
| `pipeline.py` (`LifeAcpStage`) | `takeover` mode: the external agent answers instead of the model |

```json
{
  "acp": {
    "enabled": false,
    "command": "",
    "args": [],
    "env": {},
    "cwd": "",
    "timeout_seconds": 120.0,
    "max_output_chars": 4000,
    "takeover": false
  }
}
```

`command` empty keeps the adapter inert even with `enabled: true`.

### Protocol surface

- ACP v1 over newline-delimited JSON-RPC on the child's stdio.
- Handshake: `initialize` (client capabilities: no fs, no terminal) → the peer
  must answer `protocolVersion: 1`, otherwise the client refuses to use it.
- One `session/new` per chat (`cwd` = `<life>/acp` by default); the text comes
  from `session/update` → `agent_message_chunk` notifications.
- `session/request_permission` is answered `cancelled`; any other peer request
  is answered with `-32601`. The bot never lets the external agent call back
  into its tools.
- Failures (timeout, crash, empty answer) close the process and return None, so
  the caller can fall back to the local pipeline.

### Two usage modes

1. **Tool** — when `takeover` is false, the local model can call
   `ask_external_agent(question)`: one prompt to the external agent, its answer
   comes back as a tool result. The local persona, memory and post-processing
   still apply to the final reply.
2. **Takeover** — when `takeover` is true, `LifeAcpStage` sends every in-scope
   plain-text message to the external agent (commands `/...` excluded), sends
   its answer through the normal sender and stops the event. A failure falls
   through to the local model instead of silencing the chat.

### Verify

```bash
pytest tests/life/test_life_acp.py -q
pytest tests/life -q

curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:6185/api/v1/life/status | jq .data.acp
```

The test suite uses `tests/life/acp_fake_server.py`, a 60-line fake ACP server,
so the handshake, chunk collection, timeout and restart paths run without any
real external agent.

### Limits

- Prompts are serialized: one process, one prompt at a time. A slow agent
  delays other chats until its `timeout_seconds` expires.
- The session id is kept in memory; a timeout or crash resets the process and
  every chat starts a fresh ACP session on the next prompt.
- Takeover bypasses the Life persona, memory injection, reply post-processing
  and the heart-flow gate: the external agent owns the conversation.

## S6e — group file service

Implemented in `bot/life/files/`:

| Module | Responsibility |
| --- | --- |
| `store.py` | Sandboxed `files/out` + `files/tmp`: sanitize, extension allowlist, size caps, TTL cleanup |
| `service.py` | Write-and-send, send existing file, http(s) download, periodic cleanup |
| `tools.py` | `send_document`, `send_file`, `download_file` |

```json
{
  "files": {
    "enabled": false,
    "root": "",
    "max_file_bytes": 2097152,
    "max_send_bytes": 5242880,
    "allowed_extensions": [".txt", ".md", ".json", ".csv", ".log", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".zip"],
    "max_files": 500,
    "keep_days": 7,
    "tmp_ttl_hours": 24,
    "cleanup_interval_minutes": 60,
    "download_timeout_seconds": 15.0
  }
}
```

Files live under `<life>/files`: generated documents in `out/`, scratch files in
`tmp/`. Nothing goes into the database; the cleanup loop removes expired files
every `cleanup_interval_minutes` and keeps at most `max_files` documents.

### Tools

- `send_document(name, content)` — write a text/Markdown document into `out/`
  and send it as a file message. This is the deferred "document reply" from S5c:
  the model produces the content, the tool turns it into an attachment.
- `send_file(name)` — send a file that already exists in `out/` (for example the
  result of `download_file`).
- `download_file(url, name="")` — fetch an http(s) URL into `out/` with
  `download_timeout_seconds` and `max_file_bytes`; it is **not** sent
  automatically.

All three re-check the Life scope, so a gray release never exposes files to a
session outside the allowlist.

### Safety rules

- Names are reduced to a base name, unsafe characters become `_`, reserved
  Windows device names are rejected, and the resolved path must stay under the
  sandbox.
- Only `allowed_extensions` may be written or downloaded; anything else gets an
  `error:` string instead of a silent failure.
- Writes are capped by `max_file_bytes`; sends are capped again by
  `max_send_bytes` (platform limit).
- Downloads only accept `http`/`https`, follow redirects, and abort as soon as
  the body exceeds `max_file_bytes`.

### Verify

```bash
pytest tests/life/test_life_files.py -q
pytest tests/life -q

curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:6185/api/v1/life/status | jq .data.files
```

### Limits

- Sending relies on the platform's `File` message segment (aiocqhttp converts a
  local path to a `file://` segment). A protocol end that only implements
  `upload_group_file` will reject it; the tool then returns an `error:` line.
- Only plain text/Markdown is generated. PDF/Word conversion is out of scope.
- The store is a plain directory: no quota accounting beyond `max_files`, and
  deleting a file while it is queued for sending makes that send fail.

## S6g — statistics and diagnostics

Implemented in `bot/life/diagnostics.py`; the CLI is
[`diagnostics.py`](diagnostics.py), the endpoint is
`GET /api/v1/life/diagnostics`.

The report reuses the status snapshot every service already exposes, derives the
ratios the runbook needs, and turns them into checks. It never changes config,
sends a message or runs a learning round.

```json
{
  "overall": "pass | warn | fail",
  "metrics": {"heartflow": {...}, "memory": {...}, "review": {...}, ...},
  "checks": [{"name": "...", "status": "pass|warn|fail", "detail": "..."}],
  "generated_at": 1730000000.0
}
```

### Derived metrics

- **Heart-flow**: turns / replied / waited / merged / capacity drops and the
  `reply_rate`, `wait_rate`, `merge_rate` ratios.
- **Memory**: total, embedded, vector coverage, newest-memory age, per-kind
  counts.
- **Review backlog**: pending facts, conflicted facts, pending slang.
- Per-feature counters: tool-result condense, sub-agents, monitor ring, files,
  ACP, keyword reactions.

### Checks

| Check | Warns when |
| --- | --- |
| `heartflow_reply_rate` | ≥ 20 turns and reply rate < 2% or > 80% |
| `heartflow_capacity` | any capacity drop |
| `memory_vector_coverage` | embedding on, ≥ 10 memories, coverage < 50% |
| `memory_freshness` | newest memory older than 7 days |
| `memory_review_backlog` | pending + conflicted + slang > 20 |
| `tool_result_condense` | ≥ 10 calls and > 30% failed |
| `subagent_health` | ≥ 5 runs and > 30% failed |
| `monitor_ring` | the record ring overflowed |
| `files_store` | the store reached `max_files` |
| `acp_health` | ≥ 5 asks and > 30% failed or timed out |
| `reaction_health` | more than half of the reaction sends failed |

`overall` is `fail` only when the report itself could not be built; every
threshold violation is a `warn`, because a quiet group is not an outage.

### Run it

```bash
# from a saved dashboard payload
curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:6185/api/v1/life/status > data/life/status.json
python scripts/life/diagnostics.py --status data/life/status.json \
  --out data/life/diagnostics.json
```

Exit codes: **0** pass, **1** warn, **2** fail — usable directly in a cron or
runbook check. The endpoint returns the same report wrapped in `{"status": "ok"}`.

### Limits

- The thresholds are hand-set heuristics, not measured baselines; tune them once
  the S0 corpus exists.
- `memory_freshness` compares against wall-clock time, so a session that has
  been quiet for a week will warn even when nothing is wrong.
- There is no dedicated panel page yet; the eight S6f pages cover the data, and
  the diagnostics endpoint is ready for a small health card later.

## S7a — gray rollout and shadow observation

The rollout has two halves: a stable percentage bucket decides *which*
conversations the layer takes over, and a durable decision log records what
each released turn decided. Both are off by default.

### Ramp it

```json
{
  "enabled": true,
  "scope": {
    "mode": "allowlist",
    "enabled_group_ids": ["ops-test-group"],
    "rollout_percent": 1,
    "rollout_salt": "2026-11-w1"
  },
  "heartflow": {
    "enabled": true,
    "runtime": {"shadow": true}
  }
}
```

- `rollout_percent` is a share of **groups** (a private chat is a session of its
  own). The bucket is `sha256(salt + key) % 100`, so a group never flips between
  messages, restarts or workers.
- Explicit `enabled_umos` / `enabled_group_ids` entries bypass the percentage;
  `disabled_umos` always wins.
- `heartflow.runtime.shadow=true` keeps the runtime deciding and recording but
  never wakes the pipeline: nothing is sent. Use it for the first window, then
  turn it off one percent at a time.

Suggested ladder: shadow at `percent=100` for 3–7 days → `percent=1` → 10 → 50 →
100, at least 48 h per step. Rollback is config-only: set `rollout_percent` to 0
and/or `shadow` back to true; no data is touched.

### What is recorded

One row per released turn in `life_turn_decision`: timestamp, session, merged
message count, `decision` (reply/wait), `action` (continue/hold), `shadow`,
gate `reason` / `likelihood` / `threshold`, `latency_ms` and `text_chars`.

The row has **no message and no reply text**. `latency_ms` is the debounce wait
from the first merged message to the decision, not model generation time.

Retention is `heartflow.runtime.decision_retention_days` (default 90, 0 keeps
everything); the maintenance loop prunes it every 6 hours.

### Read it

```bash
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:6185/api/v1/life/rollout?days=14"
```

```json
{
  "window_days": 14,
  "turns": 812,
  "reply": 301,
  "wait": 511,
  "shadowed": 301,
  "live": 0,
  "reply_rate": 0.371,
  "latency_ms": {"p50": 1200, "p90": 2500, "max": 8000},
  "sessions": [{"session_key": "...", "turns": 120, "reply": 44, "last_ts": 0}],
  "recent": [{"decision": "reply", "shadow": true, "reason": "direct_address"}]
}
```

`/life/status` embeds the same aggregates in its `rollout` block, cached for a
minute. The diagnostics report adds two checks:

| Check | Warns when |
| --- | --- |
| `rollout_observation` | ≥ 10 observation attempts and > 10% failed |
| `rollout_activity` | shadow is on or `percent > 0`, but the 14-day window is empty |

### Limits

- Shadow is **decision-level**: a shadowed turn never calls the model, so there
  is no draft to compare against MaiBot. Content quality still comes from the
  S0 replay set.
- Shadow advances the gate state as if the reply had been sent: cooldown and
  wait backoff behave like a live turn, which is the right counterfactual but
  means the would-reply rate is not "what would happen if nothing were sent".
- The log stores no text on purpose; reviewing a specific reply needs the
  platform-side logs.
- Percent bucketing is stable but arbitrary: with few groups, "1%" may cover
  zero groups. Check `turns` before treating a step as live.
- Changing `rollout_salt` reshuffles the assignment; keep it fixed for the whole
  window.
- There is no panel page for the rollout yet (planned with S7c).
