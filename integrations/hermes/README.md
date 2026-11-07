# Perseus Vault — Hermes Native Memory Provider Plugin

> **This is a native Hermes MemoryProvider plugin**, not an MCP tool integration.
> It implements the `MemoryProvider` ABC from Hermes core, so the agent calls it
> automatically via lifecycle hooks — no manual tool invocation required.
>
> **Origin:** The native Hermes MemoryProvider integration was originally
> contributed by [sowerkoku](https://github.com/sowerkoku) in
> [#908](https://github.com/Perseus-Computing-LLC/perseus-vault/pull/908);
> the integration is maintained by Perseus Computing.

## Repository Structure

This integration lives in the Perseus Vault repo at:
```
perseus-vault/
├── integrations/
│   └── hermes/                    # ← THIS INTEGRATION
│       ├── install-perseus-vault.py   # One-file installer
│       └── README.md                # This file
```

## Overview

| Aspect | Old (MCP Tools) | New (Native Plugin) |
|--------|-----------------|---------------------|
| **Integration type** | Manual MCP tool calls | Native `MemoryProvider` plugin |
| **Memory injection** | Manual `perseus_vault_context` | Automatic — `prefetch()` recall block injected before each turn |
| **Capture** | Manual `perseus_vault_remember` | Scoped session-end capture + built-in memory mirroring |
| **Tool surface** | All MCP tools (incl. admin) | Curated 9-tool allowlist (read + scoped writes only) |
| **Setup** | `hermes mcp add perseus-vault` | One installer: plugin + `.env` token + `memory.provider` |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        HERMES AGENT                         │
├─────────────────────────────────────────────────────────────┤
│  MemoryManager                                              │
│    ├── prefetch(query) ──▶ return value INJECTED per turn   │
│    ├── queue_prefetch  ──▶ background warm for next turn    │
│    ├── on_turn_start   ──▶ turn-1 warm (results discarded)  │
│    ├── sync_turn       ──▶ non-blocking local buffer        │
│    ├── on_memory_write ──▶ mirrors built-in memory writes   │
│    ├── on_pre_compress ──▶ DURABLE checkpoint (API v2)      │
│    └── on_session_end  ──▶ FINAL distill + ckpt replay      │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTP/JSON-RPC (MCP protocol)
┌─────────────────────────────────────────────────────────────┐
│              PERSEUS VAULT MCP SERVER                       │
│  (Your server: host:port)                                   │
│  • MCP tools — provider exposes a curated 9-tool subset  │
│  • SQLite + FTS5 + vector embeddings                        │
│  • Bi-temporal queries, decay, consolidation (nightly cron) │
└─────────────────────────────────────────────────────────────┘
```

## Lifecycle Hooks

### 1. Per-turn memory injection — `prefetch(query) -> str`
**The return value of `prefetch()` is what Hermes injects into the agent
context** (via `MemoryManager.prefetch_all`, which runs it inside a timed
thread with an ~8s budget). The provider returns a deduplicated markdown
block built from:

```python
1. perseus_vault_recall_when(context=query, limit=4)   # trigger matching
2. perseus_vault_recall(query=query, limit=6, preview_cap=400)  # keyword
```

`queue_prefetch()` warms the block in the background after each turn;
`on_turn_start(turn=1)` pre-warms for the first turn. On a cold start,
`prefetch()` falls back to a bounded synchronous recall (3s per call, so
both calls fit inside the host's prefetch budget). A recall hiccup never
breaks the turn — failures degrade to an empty string.

> Note: `on_turn_start`'s return value is discarded by Hermes; only
> `prefetch()`'s return string is injected. That is why the injection
> logic lives in `prefetch()`.

### 2. Insight mirroring — `on_memory_write(action, target, content)`
Mirrors built-in memory writes (MEMORY.md/USER.md) to the Vault under
`hermes-memory` entities, so a hybrid setup never loses a write.

### 3. Pre-compress checkpoints — `on_pre_compress(messages)`

Implements the **Hermes checkpoint contract (API v2)** — see the public
docs, "Pre-Compress Checkpoints (fail-closed)". The provider declares
`pre_compress_checkpoint_api_version = 2`:

- Evidence is archived **durable, never distilled**: user/assistant
  prose rows (host-normalized by v2; re-filtered locally as defense)
  are stored as a Vault entity `category=checkpoint`, `status=draft`,
  `importance=0.3`, tagged `external_refs[hermes_session]`.
- **Identity** is content- and scope-addressed:
  `ckpt-<sha1(gateway_session_key|profile|context)[:16]>-<sha1(evidence)[:12]>`
  — identical text in different sessions/profiles never collides, and
  retries or overlapping transcripts upsert (idempotent by contract).
- **Failure semantics**: a successful return (`checkpoint: <key>`,
  forwarded into the summarizer prompt) means the write committed; any
  failure raises — never partial success. The fail-closed gate
  (`compression.checkpoint_required`) stays **OFF** in this deployment;
  the raise-on-failure behavior is in place so it can be armed later
  without touching provider code. Arming it forces `micro_compact` off
  and makes subagent forks load the provider — decide that separately.
- Known limitation (contract-defined): facts that appear **only** in
  tool output are not preserved by the checkpoint path — the v2
  normalization excludes tool rows. The final distillation remains the
  semantic layer.
- **Sub-agent retention/recovery policy (explicit):** forks write
  checkpoints (scope-tagged `agent_context`) but never distill. All
  checkpoints of a chat carry `external_refs[hermes_session] =
  gateway_session_key`, so a fork's evidence is **recovered by the
  parent session's final distillation** (the parent's replay filter is
  the session ref, not the context). If the parent never runs, drafts
  decay (importance 0.3) and are archived by the server lifecycle —
  subagents are never autonomous sources of definitive memory.
- Concurrency: sequential replay is idempotent via the session-scoped
  receipt; two *simultaneous* session-ends of the same chat could both
  distill (no distributed lease exists in the 9-tool allowlist). The
  host serializes session-end per session in current deployments —
  revisit if that ever changes.

### 4. SessionStop — `on_session_end(messages)`

The **only memory-distilling path**. Safe-consumption order:

```
read draft checkpoints ─▶ use as evidence ─▶ distill (capture ≤5)
  ─▶ VERIFY persistence ─▶ only then mark checkpoints consumed
```

- The transcript (≤ 8k chars; turn buffer ≤ 40 **plus any unconsumed
  checkpoints of the session** — this is also the post-restart recovery
  path, since `gateway_session_key` survives restarts) is distilled via
  `perseus_vault_capture(max_entities=5)`.
- Per-checkpoint **consume-receipts** (`hermes-capture-receipt`, body
  `consumed_checkpoint`, tagged with the session ref so lookup is
  bounded) provide **item-level** idempotent recovery: a receipt is
  written only after verified capture persistence, and a replayed
  session-end skips already-receipted checkpoints (no re-distillation)
  while still retrying the consumption marking. A receipt keyed on the
  whole-transcript digest would never survive a restart (new
  `session_id`, lost turn buffer), which is why receipts are keyed per
  checkpoint instead.
- Runs only for `primary` agent contexts (cron/subagent sessions are
  skipped to avoid polluting shared memory). It does **not** run a
  global `consolidate` — that is the server's nightly `maintain` cron
  job, and running it on every session end would be wasteful and race
  the cron.

### Tool surface

The provider exposes a **curated 9-tool allowlist** — read tools plus
scoped writes — and rejects everything else, so the agent never sees
admin/destructive tools (`purge`, `consolidate`, `state_*`, `authority_*`,
`action_*`, …):

`perseus_vault_recall`, `perseus_vault_recall_when`,
`perseus_vault_context`, `perseus_vault_semantic_search`,
`perseus_vault_stats`, `perseus_vault_remember`, `perseus_vault_forget`,
`perseus_vault_journal`, `perseus_vault_capture`

Tool schemas are **static** (fetched at install time and embedded) because
Hermes snapshots provider tool schemas *before* `initialize()` runs —
dynamically discovered schemas would be missing on turn 1.

## Quick Install

### One-liner (non-interactive)

```bash
PERSEUS_VAULT_MCP_TOKEN=your-token-here \
PERSEUS_VAULT_URL=http://ip-server:port/message \
  curl -fsSL https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/integrations/hermes/install-perseus-vault.py | python3
```

The token is **required** in non-interactive mode — the installer refuses
to run with a placeholder. It persists the token to `$HERMES_HOME/.env`
(backed up first) and sets `memory.provider: perseus-vault`.

### Interactive

```bash
curl -fsSL https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/integrations/hermes/install-perseus-vault.py > install-perseus-vault.py
python3 install-perseus-vault.py
# Prompts: local/remote → IP:port → token (required)
```

### Environment Variables (for automation)

| Variable | Purpose | Default |
|----------|---------|---------|
| `PERSEUS_VAULT_MCP_TOKEN` | MCP bearer token (required, non-interactive) | *(required)* |
| `PERSEUS_VAULT_URL` | Vault MCP endpoint | `http://localhost:8767/message` |
| `PERSEUS_VAULT_WORKSPACE` | Workspace scope hash (blank = global) | *(blank)* |
| `MCP_PERSEUS_VAULT_API_KEY` | Legacy alias for the token | — |
| `MCP_HOST_PORT` | Legacy alias: `host:port` → `http://host:port/message` | — |
| `HERMES_HOME` | Hermes config directory | `~/.hermes` |

The provider resolves config in this order: **env vars → config.yaml
`memory.perseus-vault:` → `$HERMES_HOME/perseus-vault.json` → defaults**.

## What the installer does

1. Writes the plugin to `$HERMES_HOME/plugins/perseus-vault/`
   (`plugin.yaml`, `__init__.py`, `provider.py`, `cli.py`)
2. `hermes plugins enable perseus-vault`
3. Writes `PERSEUS_VAULT_MCP_TOKEN=…` to `$HERMES_HOME/.env` (mode 600,
   existing `.env` backed up; skipped if already set)
4. Writes non-secret config to `$HERMES_HOME/perseus-vault.json` (mode 600)
5. `hermes config set memory.provider perseus-vault`
6. Verifies with `hermes memory status`

## Post-Install Verification

```bash
# Memory provider status
hermes memory status
# → … perseus-vault (API key / local) ← active

# Plugin connection
hermes perseus-vault status
# → Connected: True | URL: http://ip-server:port/message | Tools: 81

# List available tools
hermes perseus-vault tools
```

Restart Hermes (or the gateway) after installing so the new `.env` token
is picked up.

## Built-in Memory (optional)

The Vault provider works in **hybrid mode**: built-in memory
(MEMORY.md/USER.md) stays active and every built-in write is mirrored to
the Vault via `on_memory_write`. For a vault-only setup, disable the
built-in stores in `$HERMES_HOME/config.yaml`:

```yaml
memory:
  memory_enabled: false
  user_profile_enabled: false
```

## CLI Commands (optional, for debugging)

```bash
hermes perseus-vault status          # Connection health
hermes perseus-vault tools           # List MCP tools
hermes perseus-vault recall "query"  # FTS5 keyword search
hermes perseus-vault semantic "query" # Vector search
hermes perseus-vault remember        # Manual store
hermes perseus-vault stats           # Vault statistics
hermes perseus-vault config          # Show config (non-secret)
```

## Cron (Server-side Hygiene)

The **server** runs nightly maintenance (separate from the agent's
session-end capture):

```bash
# Nightly: cohere → decay → compact → consolidate → dedup/orphans/reindex
15 3 * * *  perseus-vault maintain --db /abs/path/perseus-vault.db

# Weekly: + vacuum
30 3 * * 0  perseus-vault maintain --db /abs/path/perseus-vault.db --vacuum
```

## Requirements

- **Hermes Agent** ≥ 0.20.0 (native plugin support; `prefetch` injection,
  `on_turn_start`, `on_delegation`, `backup_paths` hooks)
- **Perseus Vault MCP Server** ≥ 2.23.1 running at `host:port`
- **Python** 3.10+ (for installer)

## Plugin Files

| File | Purpose |
|------|---------|
| `plugin.yaml` | Plugin manifest (name, version, description, cli_commands) |
| `__init__.py` | `register(ctx)` → registers `create_provider()` |
| `provider.py` | `PerseusVaultProvider` implementing the `MemoryProvider` ABC |
| `cli.py` | `hermes perseus-vault <cmd>` subcommands |

## References

- **Hermes MemoryProvider Plugin Spec**: https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin
- **Perseus Vault Lifecycle Hooks**: https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/docs/lifecycle-hooks.md

## License

MIT — Same as Perseus Vault.
