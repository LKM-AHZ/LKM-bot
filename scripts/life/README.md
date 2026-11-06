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
floor), `always` (always reply, the ceiling). To compare against MaiBot, import
its decisions in the same shape instead of re-running a decider.

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

