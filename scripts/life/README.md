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
