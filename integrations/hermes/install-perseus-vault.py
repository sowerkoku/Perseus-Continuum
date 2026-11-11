#!/usr/bin/env python3
"""Perseus Vault Memory Provider Installer for Hermes Agent.

Installs the perseus-vault memory plugin on any machine with Hermes.
Run with: python3 install-perseus-vault.py

Non-interactive mode (token required; never a placeholder):
    PERSEUS_VAULT_MCP_TOKEN=<token> PERSEUS_VAULT_URL=http://host:port/message \
        python3 install-perseus-vault.py

Legacy aliases from the original installer are still accepted:
    MCP_PERSEUS_VAULT_API_KEY=<token> MCP_HOST_PORT=host:port

What the installer does:
  1. Writes the plugin to $HERMES_HOME/plugins/perseus-vault/
     (plugin.yaml, __init__.py, provider.py, cli.py)
  2. `hermes plugins enable perseus-vault`
  3. Persists the bearer token to $HERMES_HOME/.env as
     PERSEUS_VAULT_MCP_TOKEN (skipped if already present; .env backed up first)
  4. Writes non-secret config to $HERMES_HOME/perseus-vault.json (mode 600)
  5. `hermes config set memory.provider perseus-vault`
  6. Verifies with `hermes memory status`

Versioned line (contract with users — see integrations/hermes/README.md):
  Plugin: 0.3.0. Supported Hermes: >= 0.20.0 documented floor; VERIFIED
  through v0.23.1 only, which is the ceiling of this line. The next line
  (>= 0.4.0) is independent and NOT retro-compatible: it targets Hermes
  v0.23.1 exclusively and gates the host version and critical APIs before
  writing any file. Widening support requires an explicit contract review.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


class Colors:
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    NC = "\033[0m"


def log_info(msg: str) -> None:
    print(f"{Colors.BLUE}[INFO]{Colors.NC} {msg}")


def log_ok(msg: str) -> None:
    print(f"{Colors.GREEN}[OK]{Colors.NC}  {msg}")


def log_warn(msg: str) -> None:
    print(f"{Colors.YELLOW}[WARN]{Colors.NC} {msg}")


def log_error(msg: str) -> None:
    print(f"{Colors.RED}[ERROR]{Colors.NC} {msg}")


def run_cmd(cmd: list, capture: bool = True) -> subprocess.CompletedProcess:
    """Run command and return result."""
    return subprocess.run(cmd, capture_output=capture, text=True)


def check_hermes() -> Optional[str]:
    """Check if Hermes is installed and return version."""
    if not shutil.which("hermes"):
        return None
    result = run_cmd(["hermes", "--version"])
    return result.stdout.strip().split("\n")[0] if result.stdout else "unknown"


def prompt_with_default(prompt: str, default: str) -> str:
    """Prompt user with default value. Falls back to default in non-interactive mode."""
    if not sys.stdin.isatty():
        return default
    if default:
        response = input(f"{prompt} [{default}]: ").strip()
        return response if response else default
    while True:
        response = input(f"{prompt}: ").strip()
        if response:
            return response
        print("  This field is required.")


def prompt_required(prompt: str) -> str:
    """Prompt until non-empty (interactive only)."""
    while True:
        response = input(f"{prompt}: ").strip()
        if response:
            return response
        print("  This field is required.")


def get_hermes_home() -> Path:
    """Get Hermes home directory."""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


# ===========================================================================
# Version support declaration (line 0.3.0). These constants are a contract
# with users: changing any value requires re-reviewing the documented
# contracts in integrations/hermes/README.md against the host's exact code.
# The next line (0.4.x) is independent and NOT retro-compatible: it will
# target Hermes v0.23.1 exclusively, with a hard pre-write gate.
# ===========================================================================

PLUGIN_VERSION = "0.3.0"            # must match the embedded PLUGIN_YAML below
SUPPORTED_HERMES_MIN = "0.20.0"    # documented floor of the 0.3.0 line
VERIFIED_HERMES_CEILING = "0.23.1" # highest Hermes version verified for this line


def plugin_yaml_version() -> str:
    """Version declared by the embedded plugin.yaml (consistency check)."""
    for line in PLUGIN_YAML.splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


# ===========================================================================
# D1 host-contract gate — fail-closed, runs BEFORE any prompt or write
# ===========================================================================

GATE_REQUIRED_VERSION = "0.23.1"


class HostContractError(RuntimeError):
    """The installed Hermes does not satisfy the integration contract."""


def _gate_proves_key_passed(fn_node, ast_mod, key: str) -> bool:
    """Conservative, SEQUENTIAL, single-scope proof that some real
    .initialize(...) call inside fn_node receives `key`:

    - Statements are visited in execution (source) order; `proven`
      holds only the dicts known to carry the key AT THE PROGRAM
      POINT OF THE CALL. An assignment AFTER a call is not evidence
      for that call.
    - Only this function's own executable scope is scanned: nested
      def/async def/class bodies are skipped entirely - they may
      never execute, so assignments inside them are not evidence.
      Lambda expressions ARE scanned at their position: Hermes's
      real iteration shape passes `lambda p: p.initialize(...)` to
      a synchronous helper, and a lambda at this position executes
      here; a lambda stored and invoked later with assignments
      added in between is NOT provable and fails closed.
    - Accepted assignment forms (the ones Hermes actually uses):
      d[key] = ..., d = {...key...}, d = alias-of-proven,
      d.update({...key...}). Anything else is not evidence: when
      propagation cannot be demonstrated the call fails closed.
    No full Python analyzer; deliberately conservative.
    """
    proven = set()

    def note_assign(node):
        for t in node.targets:
            if (isinstance(t, ast_mod.Subscript)
                    and isinstance(t.slice, ast_mod.Constant)
                    and t.slice.value == key
                    and isinstance(t.value, ast_mod.Name)):
                proven.add(t.value.id)
            elif (isinstance(t, ast_mod.Name)
                    and isinstance(node.value, ast_mod.Dict)
                    and any(isinstance(k, ast_mod.Constant)
                            and k.value == key
                            for k in node.value.keys)):
                proven.add(t.id)
            elif (isinstance(t, ast_mod.Name)
                    and isinstance(node.value, ast_mod.Name)
                    and node.value.id in proven):
                proven.add(t.id)

    def note_call(node):
        if not isinstance(node.func, ast_mod.Attribute):
            return False
        if (node.func.attr == "update"
                and isinstance(node.func.value, ast_mod.Name)):
            tgt = node.func.value.id
            for a in node.args:
                if ((isinstance(a, ast_mod.Dict)
                        and any(isinstance(k, ast_mod.Constant)
                                and k.value == key for k in a.keys))
                        or (isinstance(a, ast_mod.Name)
                            and a.id in proven)):
                    proven.add(tgt)
        if node.func.attr == "initialize":
            for kw in node.keywords:
                if kw.arg == key:
                    return True
                if kw.arg is None:
                    v = kw.value
                    if isinstance(v, ast_mod.Name) and v.id in proven:
                        return True
                    if (isinstance(v, ast_mod.Dict)
                            and any(isinstance(k, ast_mod.Constant)
                                    and k.value == key
                                    for k in v.keys)):
                        return True
        return False

    def scan(node):
        if isinstance(node, (ast_mod.FunctionDef,
                             ast_mod.AsyncFunctionDef,
                             ast_mod.ClassDef)):
            return False
        if isinstance(node, ast_mod.Assign):
            note_assign(node)
        elif isinstance(node, ast_mod.Call):
            if note_call(node):
                return True
        for child in ast_mod.iter_child_nodes(node):
            if scan(child):
                return True
        return False

    for stmt in fn_node.body:
        if scan(stmt):
            return True
    return False


def _discover_host_root() -> Path:
    """Locate the installed host root through its OWN launcher.

    Deterministic by construction: exactly one PATH candidate (the
    first hit of which()), its exec chain followed up to 8 hops, the
    host root taken from the host's own sys.path insertion. No
    searching, no alternative candidates, no fallbacks. Relative exec
    targets resolve against the directory of the script being read
    (p.parent), never against the entry wrapper's directory; quoted
    exec targets are accepted."""
    exe = shutil.which("hermes")
    if not exe:
        raise HostContractError("hermes executable not found in PATH")
    p = Path(exe)
    root_text = None
    for _hop in range(8):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise HostContractError(f"cannot inspect launcher {p}: {e}")
        m = re.search(r"sys\.path\.insert\(\d+,\s*['\"]+([^'\"]+)['\"]+",
                      text)
        if m:
            root_text = m.group(1)
            break
        m2 = re.search(r"^exec\s+(?:'([^']+)'|\"([^\"]+)\"|(\S+))",
                       text, re.MULTILINE)
        if not m2:
            raise HostContractError(
                "installed host root not discoverable from its launcher")
        target = next(g for g in m2.groups() if g)
        nxt = Path(target)
        if not nxt.is_absolute():
            nxt = p.parent / nxt
        p = nxt
    if root_text is None:
        raise HostContractError("launcher chain too deep to resolve")
    root = Path(root_text)
    if not (root / "agent" / "memory_provider.py").is_file():
        raise HostContractError(
            f"discovered host root {root} lacks agent/memory_provider.py")
    return root


def check_host_contract(host_root=None) -> str:
    """G1-G5 from the authorized D1 contract. Fail-closed: every
    unverifiable condition is a failure. Returns the version on success.

    PERSEUS_GATE_HOST_ROOT (or host_root arg): strict test/diagnostic
    seam — when set it is the ONLY considered root; failure never falls
    back to the real host. No other knobs, no permissive warnings.
    """
    # The validation itself must be READ-ONLY with respect to the
    # host: importing host modules must not write .pyc files into
    # the host tree. Enforced internally, independent of any external
    # PYTHONDONTWRITEBYTECODE setting.
    sys.dont_write_bytecode = True

    override = (host_root or
                os.environ.get("PERSEUS_GATE_HOST_ROOT", "")).strip()
    if override:
        root = Path(override)
        if not (root / "agent" / "memory_provider.py").is_file():
            raise HostContractError(
                f"PERSEUS_GATE_HOST_ROOT={root} is not a host root")
    else:
        root = _discover_host_root()

    import ast as _ast
    import importlib
    import inspect as _inspect

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    # deterministic inspection: drop any previously-loaded host modules
    for _m in ("hermes_cli", "agent", "agent.memory_provider",
               "agent.memory_manager"):
        sys.modules.pop(_m, None)

    # G1: exact version from the installed host itself
    try:
        hc = importlib.import_module("hermes_cli")
        version = str(getattr(hc, "__version__", "") or "")
    except Exception as e:
        raise HostContractError(
            f"installed host version not readable: {e}")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise HostContractError(
            f"installed host version is not unambiguously readable: "
            f"{version!r}")
    if version != GATE_REQUIRED_VERSION:
        raise HostContractError(
            f"this line targets Hermes exactly {GATE_REQUIRED_VERSION}; "
            f"installed host is {version}")

    # G2: spawn_context_thread importable from the real module
    try:
        mp = importlib.import_module("agent.memory_provider")
        if not callable(getattr(mp, "spawn_context_thread", None)):
            raise AttributeError("missing or not callable")
    except HostContractError:
        raise
    except Exception as e:
        raise HostContractError(
            f"spawn_context_thread not importable from the installed "
            f"host: {e}")

    # G3: on_session_switch with explicit parameters (not **kwargs)
    try:
        sig = _inspect.signature(mp.MemoryProvider.on_session_switch)
    except Exception as e:
        raise HostContractError(
            f"on_session_switch not present/inspectable on the installed "
            f"host: {e}")
    for _name in ("new_session_id", "parent_session_id", "reset",
                  "rewound"):
        _p = sig.parameters.get(_name)
        if _p is None or _p.kind == _inspect.Parameter.VAR_KEYWORD:
            raise HostContractError(
                f"on_session_switch parameter {_name!r} is not explicit "
                f"in the installed host")

    # G4: the real initialization call verifiably PROPAGATES
    # hermes_home to the initialize() call itself. AST-scoped to
    # MemoryManager.initialize_all; structural similarity (unparse +
    # substring) is not evidence, and locals that never reach the call
    # are not evidence. Propagation forms accepted exactly as Hermes
    # expresses them: direct keyword or **dict carrying the key.
    try:
        tree = _ast.parse(
            (root / "agent" / "memory_manager.py").read_text(
                encoding="utf-8"))
        init_fn = next(
            fn for cls in tree.body
            if isinstance(cls, _ast.ClassDef) and cls.name == "MemoryManager"
            for fn in cls.body
            if isinstance(fn, _ast.FunctionDef)
            and fn.name == "initialize_all")
    except Exception as e:
        raise HostContractError(
            f"initialization contract not verifiable "
            f"(MemoryManager.initialize_all): {e}")
    if not _gate_proves_key_passed(init_fn, _ast, "hermes_home"):
        raise HostContractError(
            "MemoryManager.initialize_all does not verifiably propagate "
            "hermes_home to an initialize() call")

    # G5: declared version == embedded plugin.yaml (BEFORE any write)
    declared_yaml = plugin_yaml_version()
    if declared_yaml != PLUGIN_VERSION:
        raise HostContractError(
            f"internal inconsistency: installer declares {PLUGIN_VERSION} "
            f"but embedded plugin.yaml declares {declared_yaml!r}")
    return version


# ===========================================================================
# Plugin files content
# ===========================================================================

PLUGIN_YAML = r"""name: perseus-vault
version: "0.3.0"
description: "Perseus Vault memory provider via MCP HTTP — persistent, cross-session knowledge with semantic search and bi-temporal queries"
author: "Perseus Computing"
cli_commands:
  - perseus-vault
"""

INIT_PY = r'''"""Perseus Vault Memory Provider Plugin for Hermes Agent.

Registers the MemoryProvider implementation via the plugin system.
"""

from .provider import PerseusVaultProvider, create_provider


def register(ctx):
    """Plugin entry point - registers the MemoryProvider instance."""
    ctx.register_memory_provider(create_provider())


__all__ = ["PerseusVaultProvider", "create_provider", "register"]
'''

PROVIDER_PY = r'''"""Perseus Vault Memory Provider for Hermes Agent.

Native MemoryProvider implementation backed by the Perseus Vault MCP HTTP
endpoint. Key properties (see integrations/hermes/README.md):

- prefetch() returns a real recall block; Hermes injects that string before
  turns (memory_manager.prefetch_all). This is the automatic per-turn
  memory surface. on_turn_start() and queue_prefetch() warm it in the
  background; on a cold start prefetch() falls back to a bounded synchronous
  recall (timeouts sized to the host's prefetch budget, issue #753 pattern).
- Curated tool allowlist: only safe read + scoped write tools are exposed to
  the agent. No purge/consolidate/authority/state/admin tools.
- Pre-compress checkpoints implement the Hermes checkpoint contract
  (API v2): on_pre_compress() persists content-addressed evidence
  (category=checkpoint, status=draft) and returns 'checkpoint: <key>';
  any failure raises. on_session_end() is the ONLY memory-distilling
  path: it replays unconsumed checkpoints of its session as evidence,
  distills once (receipt-keyed for idempotent recovery after a crash),
  and marks checkpoints consumed only after the distillation persisted.
  The operator gate compression.checkpoint_required stays OFF here.
- Config resolution order: env vars (canonical PERSEUS_VAULT_*, legacy
  MCP_PERSEUS_VAULT_API_KEY / MCP_HOST_PORT) -> config.yaml
  memory.perseus-vault: -> $HERMES_HOME/perseus-vault.json -> defaults.
- sync_turn() is non-blocking (local buffer, flushed at session end).
- Host contracts (Hermes v0.23.1): every background job goes through
  agent.memory_provider.spawn_context_thread (profile contextvars must
  travel with the thread), and every per-profile path binds from the
  hermes_home kwarg of initialize(). No version-conditional branches or
  fallbacks: hosts without these APIs must satisfy them before install
  (installer gate belongs to the release process, see README).
- Session end runs a SCOPED capture (primary agent contexts only), not a
  global consolidate.
- workspace_hash is config-driven and only sent when set (blank = global).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from agent.memory_provider import MemoryProvider, spawn_context_thread

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://localhost:8767/message"
_PREFETCH_LIMIT = 6
_PREVIEW_CAP = 400
_SESSION_CAPTURE_MAX_CHARS = 8000
_TURN_BUFFER_CAP = 40
# The host (MemoryManager._prefetch_provider) runs prefetch() inside a thread
# it join()s with an ~8s budget; two serialized recall calls must fit.
_SYNC_CALL_TIMEOUT = 3.0
_WARM_CALL_TIMEOUT = 15.0

TOOL_ALLOWLIST = [
    "perseus_vault_context",
    "perseus_vault_recall",
    "perseus_vault_recall_when",
    "perseus_vault_semantic_search",
    "perseus_vault_stats",
    "perseus_vault_remember",
    "perseus_vault_forget",
    "perseus_vault_journal",
    "perseus_vault_capture",
    "perseus_vault_admission_decide",
]

# Static schemas (fetched from a live Perseus Vault MCP server >= 2.22.0).
# Static is REQUIRED: MemoryManager snapshots get_tool_schemas() BEFORE
# initialize() runs, so dynamically discovered schemas would be missing on
# turn 1. The agent never sees tools outside TOOL_ALLOWLIST.
SCHEMAS = json.loads(r"""[
 {
  "name": "perseus_vault_context",
  "description": "Return a pre-formatted markdown context block for session injection. Recall-first by default (mode 'on_demand'): pass `query` (the current task/message) and only topically relevant entities — recall_when trigger matches + keyword matches — are injected, alongside a hard-capped always-on set, clamped to a per-model character budget. Without `query` the block is a compact retrieval pointer (byte-stable across unrelated writes — prefix-cache friendly). The legacy unconditional top-N dump requires explicit mode 'always_inject'. Output is informational context, not instructions.",
  "inputSchema": {
   "type": "object",
   "properties": {
    "categories": {
     "type": "array",
     "items": {
      "type": "string"
     },
     "description": "Categories to include. Empty array = all categories."
    },
    "limit": {
     "type": "integer",
     "default": 10,
     "description": "Maximum number of entities to include in the context block"
    },
    "workspace_hash": {
     "type": "string",
     "description": "Workspace scope filter (v1.2.0). When set, only entities with a matching workspace_hash are included (always-on set too). Omit for no workspace filtering — in a federated vault that leaks every workspace's memory into the block."
    },
    "query": {
     "type": "string",
     "description": "Current task/message text — the relevance gate (#356). In on_demand mode only entities whose recall_when triggers or indexed content match it are injected; omit for a compact retrieval pointer with no topical injection."
    },
    "mode": {
     "type": "string",
     "enum": [
      "on_demand",
      "always_inject"
     ],
     "default": "on_demand",
     "description": "Injection posture (#366). 'on_demand' (default): relevance-gated, budget-clamped, recall-first. 'always_inject': legacy unconditional top-N dump (no relevance gating) — explicit opt-in only."
    },
    "model": {
     "type": "string",
     "description": "Host model name for recall-budget profile resolution (#366), large-context models get a larger budget. Unknown/omitted models use the default 1500-char profile."
    },
    "max_context_chars": {
     "type": "integer",
     "description": "Explicit character budget for the rendered block; overrides the model profile. In always_inject mode output is clamped only when this is set."
    },
    "session_id": {
     "type": "string",
     "description": "Session id for preload usage telemetry (#875): injected entities are attributed to this session for precision/recall resolution. Omit or leave empty when unknown."
    }
   },
   "required": []
  },
  "outputSchema": {
   "type": "object",
   "properties": {
    "markdown": {
     "type": "string",
     "description": "Markdown-formatted context block with entity details"
    },
    "total_chars": {
     "type": "integer",
     "description": "Character count of the markdown content"
    },
    "mode": {
     "type": "string",
     "description": "Resolved injection mode: on_demand or always_inject"
    },
    "budget_chars": {
     "type": "integer",
     "description": "Resolved character budget (0 = unclamped legacy output)"
    },
    "entities_injected": {
     "type": "integer",
     "description": "Number of entities actually injected (always-on + topical)"
    },
    "warnings": {
     "type": "array",
     "items": {
      "type": "string"
     },
     "description": "Soft warnings: always-on cap overflow, budget truncation"
    }
   }
  },
  "annotations": {
   "readOnlyHint": true
  },
  "title": "Get Context Block"
 },
 {
   "name": "perseus_vault_recall",
   "description": "Search entities with FTS5 keyword search. Words are OR'd together. Returns entities sorted by relevance with expanded content/summary fields at top level. Use this to find previously stored facts, decisions, or architecture notes. When encryption is enabled, body_json is decrypted transparently.",
   "inputSchema": {
     "type": "object",
     "properties": {
       "query": {
         "type": "string",
         "description": "Search query — words are OR'd together for broad recall. An EMPTY string (\"\") is the match-all / enumeration path: it drops the keyword predicate and returns every entity in scope (respecting category/type/limit/offset), so it is the way to 'list all' a category. Wildcards are NOT globs: \"*\" is a literal FTS5 term and matches nothing — pass \"\" to enumerate, not \"*\"."
       },
       "category": {
         "type": "string",
         "description": "Filter by category, e.g. 'decision' or 'architecture'"
       },
       "type": {
         "type": "string",
         "description": "Filter by entity type, e.g. 'insight' or 'reference'"
       },
       "limit": {
         "type": "integer",
         "default": 10,
         "description": "Maximum number of results to return (max 1000)"
       },
       "offset": {
         "type": "integer",
         "default": 0,
         "description": "Number of results to skip for pagination"
       },
       "min_decay": {
         "type": "number",
         "default": 0.0,
         "description": "Minimum decay score threshold 0.0–1.0 — higher values return fresher results"
       },
       "topic_path": {
         "type": "string",
         "description": "Filter by topic path prefix, e.g. 'architecture/'"
       },
       "mode": {
         "type": "string",
         "default": "fts5",
         "description": "Search mode: 'fts5' (keyword), 'dense' (vector), 'hybrid' (fused via RRF), or 'fused' (TEMPR-style multi-strategy: fts5 + dense + graph + temporal with weighted RRF, token-budget truncation, and a full fused_trace, #883)",
         "enum": [
           "fts5",
           "dense",
           "hybrid",
           "fused"
         ]
       },
       "strategies": {
         "type": "array",
         "items": {
           "type": "string",
           "enum": [
             "fts5",
             "dense",
             "graph",
             "temporal"
           ]
         },
         "description": "Fused mode only: strategies to engage (2-4). Omit = all four. Unknown names are rejected."
       },
       "max_tokens": {
         "type": "integer",
         "default": 0,
         "description": "Fused mode only: token-budget truncation (estimated tokens = chars/4 per body). 0 = derive from depth_budget (mid = 4096)."
       },
       "depth_budget": {
         "type": "string",
         "enum": [
           "low",
           "mid",
           "high"
         ],
         "description": "Fused mode only: depth budget -> default token caps 1024 / 4096 / 16384 when max_tokens is unset."
       },
       "strategy_weights": {
         "type": "object",
         "description": "Fused mode only: per-strategy RRF weight multipliers (default 1.0 each). Arms that find nothing contribute nothing."
       },
       "rerank": {
         "type": "boolean",
         "default": false,
         "description": "Fused mode only: optional rerank stage over the fused pool (rank-calibrated dense + BM25 agreement signals; default off, latency-preserving)."
       },
       "query_time_unix_ms": {
         "type": "integer",
         "description": "Fused mode only: anchor instant for the temporal strategy (unix ms; default now). Accepts a number or numeric string."
       },
       "graph_utility_threshold": {
         "type": "number",
         "description": "Fused mode only (#869): graph utility gate threshold in [0,1]. The graph strategy engages only when the query's classified graph utility is >= this value. Omit = 0.5 (documented default). 0.0 disables the gate; 1.0 effectively never engages. The routing decision is always observable in fused_trace.graph_route (reason, selected, skipped_reason, gate counts)."
       },
       "profile": {
         "type": "string",
         "enum": [
           "default",
           "validity"
         ],
         "description": "#860: validity-aware recall profile. 'validity' re-ranks fused results by a deterministic validity multiplier (freshness decay, scope match, provenance class, supersession, expiry proximity) and annotates every item with its validity info; 'default'/omitted keeps relevance-only ordering. On non-fused modes the profile only enables item annotation. The weights, grade distribution, and context-invalid count are observable in fused_trace.validity."
       },
       "validity_annotate": {
         "type": "boolean",
         "default": false,
         "description": "#860: annotate delivered items with their validity info (grade, freshness, scope match, provenance class, superseded, expiring/expired, multiplier, signals); context-invalid items are additionally flagged 'context_invalid': true. Implied by profile='validity'."
       },
       "include_archived": {
         "type": "boolean",
         "default": false,
         "description": "Include archived (soft-deleted) entities in results"
       },
       "include_confidence": {
         "type": "boolean",
         "default": false,
         "description": "Add a normalized confidence score (0.0-1.0) to each result, rolled up from rank, trust (verified/certainty), and decay. Presentation-only; does not change ranking."
       },
       "include_conflict_flags": {
         "type": "boolean",
         "default": false,
         "description": "#917: add deterministic contradiction/superseded/stale flags containing only entity IDs, validity ranges, and hash-linked claim-card evidence refs. Suppressed values disclose existence only; no body value is rendered."
       },
       "include_conflict_flags_markdown": {
         "type": "boolean",
         "default": false,
         "description": "#917: independently add an ID/hash/validity-only markdown conflict block. Does not implicitly enable structured conflict_flags."
       },
       "reinforce": {
         "type": "boolean",
         "default": false,
         "description": "Opt-in reinforcement for mode='dense'/'hybrid': bump retrieval_count/last_accessed/decay on the returned hits so semantically-used memories resist decay and promote through layers. Default false keeps semantic recall side-effect-free and byte-deterministic over a frozen DB. No effect on mode='fts5', which already reinforces."
       },
       "expansion": {
         "type": "object",
         "properties": {
           "enabled": {
             "type": "boolean",
             "default": false,
             "description": "Enable stemming-based query expansion"
           },
           "n_variants": {
             "type": "integer",
             "default": 1,
             "description": "Number of stemmed token variants to generate"
           }
         },
         "description": "Configuration for FTS5 query expansion using Porter stemming"
       },
       "preview_cap": {
         "type": "integer",
         "description": "If set, truncate body_json at N chars and append drill-down footer. Use perseus_vault_get_entity to read full body."
       },
       "content_weight": {
         "type": "number",
         "minimum": 0,
         "maximum": 1,
         "default": 0,
         "description": "Additive boost for content witness — rewards entities whose body text literally contains query terms. Damped by body length. Never penalizes."
       },
       "trust_weight": {
         "type": "number",
         "minimum": 0,
         "maximum": 1,
         "default": 0.15,
         "description": "Additive boost for provenance/trust (default 0.15, on by default) — verified sources rank above unverified AI drafts on the same topic. Verified entities get the full boost; unverified ones are scaled by certainty. Set 0 to disable. Never penalizes."
       },
       "diversity_halving": {
         "type": "number",
         "minimum": 0,
         "maximum": 1,
         "default": 1,
         "description": "Per-keyword diversity quota factor (1.0=disabled). Each distinct matched keyword gets ceil(N x halving^n) slots — first keyword N, second N/2, etc."
       },
       "recency_half_life_secs": {
         "type": "number",
         "minimum": 0,
         "description": "Time-aware ranking for mode='hybrid' (default off). When set, each fused result's score is multiplied by 0.5^(age / this), where age is seconds since the memory was created — so a memory this many seconds old keeps half its weight and recent context outranks older but similar hits. Omit for relevance-only ranking."
       },
       "workspace_hash": {
         "type": "string",
         "description": "Workspace scope filter (v1.2.0). When set, only entities with a matching workspace_hash are returned. Omit for no workspace filtering."
       },
       "requesting_agent_id": {
         "type": "string",
         "description": "Transport-stamped requester identity used for private/fleet visibility enforcement."
       },
       "scope_weight": {
         "type": "number",
         "minimum": 0,
         "maximum": 1,
         "description": "#485: scope as a ranking multiplier instead of a hard filter. Requires workspace_hash. Widens the workspace filter to also include GLOBAL (workspace_hash='') memories, weighted by this factor in the ranking (hybrid/dense scores multiplied; keyword mode returns current-scope hits first) — current-workspace memories outrank equally-relevant global ones, but a strong global memory still surfaces. Never exposes other workspaces' memories. Omit for the strict filter (unchanged default)."
       },
       "agent_id": {
         "type": "string",
         "description": "Agent identity filter (v1.2.0). When set, only entities with a matching agent_id are returned. Omit for no agent filtering."
       },
       "epistemic_state": {
         "type": "string",
         "enum": [
           "candidate",
           "verified",
           "corroborated",
           "rejected",
           "defensively_recalled"
         ],
         "description": "#880: epistemic trust-axis filter. When set, only entities in the requested trust state are returned — 'candidate' surfaces useful-but-unverified records, 'verified'/'corroborated' restrict to established fact, 'rejected' shows reviewed-and-refused records. Omit for no trust filtering (default)."
       },
       "retrieval_profile": {
         "type": "string",
         "enum": [
           "personal",
           "agent",
           "shared"
         ],
         "description": "#784 serving posture. personal returns preference/personal classes; agent returns convention/correction/keystone classes; shared (default) returns non-personal memory in the requested workspace. Applied after visibility filtering."
       },
       "layer": {
         "type": "string",
         "description": "Filter by memory layer (world, episodic, semantic)."
       },
       "ref_type": {
         "type": "string",
         "description": "#728: post-filter hits to entities whose body external_refs carry this ref_type (exact match, e.g. 'repo', 'pull_request', 'jira_key')."
       },
       "ref_value": {
         "type": "string",
         "description": "#728: post-filter hits to entities whose body external_refs carry this ref_value. Matches exactly or as a hierarchical '/' prefix ('github:Org' matches 'github:Org/repo')."
       },
       "deadline_ms": {
         "type": "integer",
         "description": "#864: bounded recall. When set, the recall is timed; if it exceeds this many ms the response outcome.status is 'timeout' so callers know the result set may be incomplete. Results are still returned in full."
       },
       "include_outcome": {
         "type": "boolean",
         "default": false,
         "description": "#864/#873/#887: always attach the explicit 'outcome' block (status, backend health, abstention, reason). By default it is attached only when recall was degraded/partial/timeout/empty/unavailable/stale, so nominal responses stay byte-identical."
       },
       "as_of_unix_ms": {
         "type": "integer",
         "description": "#472 Temporal RAG: transaction-time instant (unix ms). Reconstruct semantic recall AS BELIEVED at this past instant — each hit's body is the version that was live at as_of_unix_ms; corrections recorded later do not leak in. Combine with valid_at for the full bi-temporal cell. Hits are stamped with is_live_version / recorded_at_unix_ms / valid_from_unix_ms / valid_to_unix_ms. Omit for today's live view. (v1: candidate generation is over the live index, so a fact fully deleted since that instant will not surface.)"
       },
       "valid_at": {
         "type": "integer",
         "description": "Valid-time instant (#363/#472, unix ms): reconstruct recall to the world-version whose application-time period [valid_from, valid_to) contains this instant — 'what was true at time T', per current (or as_of) knowledge. Rebuilds the point-in-time body from history (not just a live-row narrow) and returns hits stamped with is_live_version / recorded_at_unix_ms / valid_from/to. Combine with as_of_unix_ms for the full bi-temporal cell."
       },
       "valid_from_unix_ms": {
         "type": "integer",
         "description": "Valid-time period filter start (#363, unix ms). Pair with valid_to_unix_ms and valid_op; ignored when valid_at is set. Omit for unbounded start."
       },
       "valid_to_unix_ms": {
         "type": "integer",
         "description": "Valid-time period filter end (#363, unix ms, exclusive). Omit for unbounded end."
       },
       "valid_op": {
         "type": "string",
         "default": "overlaps",
         "enum": [
           "overlaps",
           "contains"
         ],
         "description": "SQL:2011 period predicate for the valid-time period filter (#363): 'overlaps' (fact's valid period shares at least one instant with the queried period) or 'contains' (fact's valid period contains the whole queried period)."
       }
     },
     "required": [
       "query"
     ]
   },
   "outputSchema": {
     "type": "object",
     "properties": {
       "items": {
         "type": "array",
         "items": {
           "type": "object"
         },
         "description": "Matching entities with expanded body_json fields at top level"
       },
       "total": {
         "type": "integer",
         "description": "Number of results returned"
       },
       "variants": {
         "type": "integer",
         "description": "Number of query variants used when expansion is enabled"
       },
       "conflict_flags": {
         "type": "array",
         "items": {
           "type": "object"
         },
         "description": "#917: optional deterministic contradiction/supersession/staleness flags; IDs, validity ranges, and hash-linked evidence refs only"
       },
       "abstain_hint": {
         "type": "boolean",
         "description": "#917: true only when a high-confidence direct contradiction is present in the delivered set"
       },
       "conflict_flags_markdown": {
         "type": "string",
         "description": "#917: optional ID/hash/validity-only markdown rendering of conflict flags"
       }
     }
   },
   "annotations": {
     "readOnlyHint": true
   },
   "title": "Recall Entities"
 },
 {
  "name": "perseus_vault_recall_when",
  "description": "Search entities whose recall_when triggers match a given context. Use this for proactive just-in-time memory injection — before writing code, before plans, at session start. Pass the current task description as context and get back memories that declared they should be recalled in similar situations.",
  "inputSchema": {
   "type": "object",
   "properties": {
    "context": {
     "type": "string",
     "description": "The current task or context description to match against recall_when triggers"
    },
    "limit": {
     "type": "integer",
     "description": "Maximum entities to return (default 10, max 100)",
     "default": 10
    },
    "workspace_hash": {
     "type": "string",
     "description": "Workspace scope filter (v1.2.0). When set, only entities with a matching workspace_hash can fire. Omit for no workspace filtering — in a federated vault that lets one workspace's triggers inject into another's turns."
    },
    "session_id": {
     "type": "string",
     "description": "Session id for preload usage telemetry (#875): served entities are attributed to this session for precision/recall resolution. Omit or leave empty when unknown."
    }
   },
   "required": [
    "context"
   ]
  },
  "outputSchema": {
   "type": "object",
   "properties": {
    "items": {
     "type": "array",
     "items": {
      "type": "object"
     }
    },
    "total": {
     "type": "integer"
    },
    "context": {
     "type": "string"
    }
   }
  },
  "annotations": {
   "readOnlyHint": true
  },
  "title": "Proactive Recall by Context"
 },
 {
   "name": "perseus_vault_semantic_search",
   "description": "Dense-only semantic search: find entities by meaning, ranked purely by embedding similarity (no keyword fallback). On by default via the bundled in-process ONNX model — zero config, zero network. A one-tool shortcut for 'find things like this'. For fused keyword+vector results use perseus_vault_recall.",
   "inputSchema": {
     "type": "object",
     "properties": {
       "query": {
         "type": "string",
         "description": "Natural-language text to semantically match against stored memories"
       },
       "limit": {
         "type": "integer",
         "default": 10,
         "description": "Maximum number of results to return"
       },
       "category": {
         "type": "string",
         "description": "Filter by category, e.g. 'decision' or 'architecture'"
       },
       "workspace_hash": {
         "type": "string",
         "description": "Workspace scope filter. When set, only entities with a matching workspace_hash are returned."
       },
       "agent_id": {
         "type": "string",
         "description": "Agent identity filter. When set, only entities with a matching agent_id are returned."
       },
       "requesting_agent_id": {
         "type": "string",
         "description": "Transport-stamped requester identity used for private/fleet visibility enforcement."
       }
     },
     "required": [
       "query"
     ]
   },
   "outputSchema": {
     "type": "object",
     "properties": {
       "items": {
         "type": "array",
         "items": {
           "type": "object"
         },
         "description": "Matching entities ranked by dense embedding similarity, with expanded body_json fields at top level"
       },
       "total": {
         "type": "integer",
         "description": "Number of results returned"
       }
     }
   },
   "annotations": {
     "readOnlyHint": true
   },
   "title": "Semantic Search Entities"
 },
 {
  "name": "perseus_vault_stats",
  "description": "Return comprehensive database statistics: entity counts by category, type, and decay layer; journal event count; state entry count; database file size; date range of stored data; and history growth (stored version rows, bytes, and the top-10 keys by version count — #398).",
  "inputSchema": {
   "type": "object",
   "properties": {}
  },
  "outputSchema": {
   "type": "object",
   "properties": {
    "total_entities": {
     "type": "integer",
     "description": "Total entities in the database"
    },
    "by_category": {
     "type": "object",
     "description": "Entity counts grouped by category"
    },
    "by_type": {
     "type": "object",
     "description": "Entity counts grouped by type"
    },
    "by_layer": {
     "type": "object",
     "description": "Entity counts grouped by decay layer (buffer/working/core)"
    },
    "total_journal_events": {
     "type": "integer",
     "description": "Total journal events recorded"
    },
    "total_state_entries": {
     "type": "integer",
     "description": "Total state entries (including expired)"
    },
    "db_file_size_bytes": {
     "type": "integer",
     "description": "Database file size on disk in bytes"
    },
    "oldest_unix_ms": {
     "type": [
      "integer",
      "null"
     ],
     "description": "Oldest entity creation timestamp, or null when the database has no entities"
    },
    "newest_unix_ms": {
     "type": [
      "integer",
      "null"
     ],
     "description": "Newest entity creation timestamp, or null when the database has no entities"
    },
    "total_history_rows": {
     "type": "integer",
     "description": "Superseded versions stored in entity_history, incl. compaction tombstones (#398)"
    },
    "history_bytes": {
     "type": "integer",
     "description": "Stored history body bytes — SUM(LENGTH(body_json)); row/index overhead excluded (#398)"
    },
    "top_history_keys": {
     "type": "array",
     "description": "Top-10 (category, key) pairs by stored version count: [{category, key, versions, bytes}] (#398)"
    }
   }
  },
  "annotations": {
   "readOnlyHint": true
  },
  "title": "Get Database Statistics"
 },
 {
  "name": "perseus_vault_remember",
  "description": "Store or update an entity by (category, key). Idempotent — call as often as you want, same key returns an update. NEAR-DUPLICATE MERGING (#531): a NEW key whose body is >=70% trigram-similar to an existing entity in the same category+workspace does NOT create a new entity — the write is folded into the existing one (result: action='deduped', deduped=true, merged_into=<id>). Right for conversational memory; wrong for bulk ingest of templated records, which are similar by construction and will silently collapse to a handful of rows. For bulk ingest pass skip_dedup=true (or use perseus_vault_ingest_file), and check the returned action. Prefer recall_when triggers (retrieve when relevant) over always_on=true (inject unconditionally): the recall-first perseus_vault_context hard-caps the always-on set and warns when it overflows, so reserve always_on for genuinely identity-critical facts. Optional certainty (0.0-1.0) is used by perseus_vault_conflicts for typed-entity conflict detection. Pass derived_from (ids or {category,key} pairs of the memories you recalled) to auto-mark those sources useful — cited memories rank higher and decay slower. Use this for saving facts, decisions, architecture notes, and conventions. Optional hints (#919): 1-3 prospective query phrasings that should retrieve this entity (vocabulary-gap recall) — indexed into FTS5 alongside the body, default-off (PERSEUS_VAULT_HINTS_ENABLED=1 to enable), replaced wholesale on update. When encryption is enabled, body_json is encrypted at rest with AES-256-GCM.",
  "inputSchema": {
   "type": "object",
   "properties": {
    "category": {
     "type": "string",
     "description": "Entity category: 'decision', 'architecture', 'convention', 'insight', or custom"
    },
    "key": {
     "type": "string",
     "description": "Unique key within the category, e.g. 'use-postgres-16' or 'deployment-strategy'"
    },
    "body_json": {
     "type": "string",
     "description": "JSON object with the entity body — store content, summary, and any custom fields here"
    },
    "status": {
     "type": "string",
     "enum": [
      "active",
      "draft",
      "deprecated",
      "expired",
      "proposed",
      "quarantined",
      "redacted"
     ],
     "default": "active",
     "description": "Closed lifecycle status vocabulary; proposed/quarantined are never publicly serveable"
    },
    "type": {
     "type": "string",
     "default": "insight",
     "description": "Entity type: 'insight', 'architecture', 'decision', 'reference', 'convention'"
    },
    "tags": {
     "type": "array",
     "items": {
      "type": "string"
     },
     "description": "Tags for categorization and cross-referencing"
    },
    "importance": {
     "type": "number",
     "default": 0.5,
     "description": "Initial importance 0.0–1.0 — sets the starting decay score"
    },
    "topic_path": {
     "type": "string",
     "default": "",
     "description": "Hierarchical topic path, e.g. 'architecture/database/postgres'"
    },
    "workspace_hash": {
     "type": "string",
     "default": "",
     "description": "Workspace scope identifier (v1.2.0). Empty = global. Entities with a workspace_hash are invisible to recall queries scoped to a different workspace."
    },
    "agent_id": {
     "type": "string",
     "default": "",
     "description": "Agent identity (v1.2.0). Tracks which agent wrote this entity. Used for agent attribution and context filtering."
    },
    "actor_kind": {
     "type": "string",
     "default": "assistant",
     "description": "Actor basis for the write (for example assistant, user, connector, or system). Missing admission stays reviewable."
    },
    "admission": {
     "type": "object",
     "description": "Hash-only admission envelope. The server emits one stable outcome_class: save, drop, block, or pending_approval. Authoritative admission requires a validated source_event_id and matching workspace; missing or unverified evidence is retained as proposed/requires_review and is not serveable. DROP/BLOCK decisions return hash-only evidence without persisting the candidate.",
     "properties": {
      "record_digest": {
       "type": "string"
      },
      "source_identity": {
       "type": "string"
      },
      "authorization_scope": {
       "type": "string"
      },
      "ingestion_channel": {
       "type": "string"
      },
      "workspace_hash": {
       "type": "string"
      },
      "source_trust": {
       "type": "string",
       "enum": [
        "untrusted",
        "trusted",
        "authoritative"
       ]
      },
      "source_event_id": {
       "type": "string"
      },
      "actor_kind": {
       "type": "string"
      },
      "actor_identity": {
       "type": "string"
      },
      "validated": {
       "type": "boolean"
      },
      "valid_from_unix_ms": {
       "type": "integer"
      },
      "recorded_at_unix_ms": {
       "type": "integer"
      },
      "task_relevance_bps": {
       "type": "integer"
      },
      "instruction_bearing": {
       "type": "boolean"
      },
      "contradicts_authoritative": {
       "type": "boolean"
      }
     }
    },
    "valid_from_unix_ms": {
     "type": "integer",
     "description": "Application-time period start (#363): when the fact became TRUE IN THE WORLD, independent of when it was recorded. Set in the past for retroactive facts ('this was true last week, we just learned it') without rewriting transaction history. Default: transaction time (now). Query with perseus_vault_valid_at / perseus_vault_bitemporal / recall's valid_at filter."
    },
    "valid_to_unix_ms": {
     "type": "integer",
     "description": "Application-time period end (#363, exclusive): when the fact STOPPED being true in the world. Omit for 'still true' (unbounded). Must be greater than valid_from_unix_ms."
    },
    "skip_dedup": {
     "type": "boolean",
     "default": false,
     "description": "Opt out of near-duplicate merging for this write (#531). Set true for bulk/API ingest of templated records so every acknowledged write actually creates its key; leave false for conversational memory."
    },
    "allow_rejected": {
     "type": "boolean",
     "default": false,
     "description": "#849: deliberate trusted override of a rejected-value tombstone. Journaled as an audited override; never set automatically."
    },
    "derived_from": {
     "type": "array",
     "items": {
      "oneOf": [
       {
        "type": "string",
        "description": "Entity id of a cited source, e.g. 'mem-a1b2c3d4e5f6' (as returned by recall/remember)"
       },
       {
        "type": "object",
        "properties": {
         "category": {
          "type": "string"
         },
         "key": {
          "type": "string"
         }
        },
        "required": [
         "category",
         "key"
        ],
        "description": "A cited source addressed by (category, key)"
       }
      ]
     },
     "description": "#487: the memories this write was built on (max 64). Each cited source is automatically marked useful — usefulness_count bumped, last_useful/last_accessed refreshed — so memories that actually inform later writes rank higher in recall and decay slower. Cite the entities you recalled before composing this write. Unknown citations are reported in the result, not fatal; self-citations are ignored."
    },
    "origin": {
     "type": "object",
     "properties": {
      "memory_kind": {
       "type": "string",
       "enum": [
        "asserted",
        "extracted",
        "inferred",
        "imported",
        "observed"
       ]
      },
      "source_system": {
       "type": "string"
      },
      "capture_method": {
       "type": "string"
      },
      "observed_at_unix_ms": {
       "type": "integer"
      }
     },
     "description": "#729: optional memory-origin/provenance metadata (spec: docs/specs/memory-provenance-and-external-refs.md). Stored inside body_json under the reserved 'origin' key — surfaced by recall/get_entity via body expansion. All fields optional; unknown values are left absent, never guessed."
    },
    "external_refs": {
     "type": "array",
     "items": {
      "type": "object",
      "properties": {
       "ref_type": {
        "type": "string"
       },
       "ref_value": {
        "type": "string"
       },
       "source_system": {
        "type": "string"
       },
       "relationship": {
        "type": "string",
        "enum": [
         "about",
         "derived_from",
         "mentions",
         "applies_to",
         "supersedes"
        ]
       }
      },
      "required": [
       "ref_type",
       "ref_value"
      ]
     },
     "description": "#728: optional first-class pointers to external systems of record (max 32). Stored inside body_json under the reserved 'external_refs' key; filter recall with ref_type/ref_value."
    },
    "evidence": {
     "type": "object",
     "description": "Write-time audit envelope for captures and decisions. capture_mode distinguishes snapshot, hash_only, pointer_only, not_requested, capture_failed, and legacy_unknown; a missing value is never interpreted implicitly.",
     "properties": {
      "capture_mode": {
       "type": "string",
       "enum": [
        "snapshot",
        "hash_only",
        "pointer_only",
        "not_requested",
        "capture_failed",
        "legacy_unknown"
       ]
      },
      "resolved_value": {
       "description": "Resolved source value retained at write time when capture_mode=snapshot"
      },
      "content_sha256": {
       "type": "string",
       "description": "64-hex SHA-256 of the resolved value or source bytes"
      },
      "source_system": {
       "type": "string"
      },
      "source_ref": {
       "type": "string"
      },
      "captured_at_unix_ms": {
       "type": "integer"
      },
      "replayable": {
       "type": "boolean"
      }
     },
     "required": [
      "capture_mode",
      "captured_at_unix_ms",
      "replayable"
     ]
    },
    "interference_mode": {
     "type": "string",
     "enum": [
      "auto",
      "refuse",
      "quarantine"
     ],
     "default": "auto",
     "description": "#874: per-write interference-gate mode override. auto (default) uses the operator-configured mode (PERSEUS_VAULT_INTERFERENCE_MODE); refuse/quarantine tighten it per-write. Per-write 'off' is refused fail-closed — only the operator can disable the gate."
    },
    "interference_bound": {
     "type": "number",
     "minimum": 0,
     "maximum": 1,
     "description": "#874: per-write interference bound override — may only TIGHTEN the configured bound (PERSEUS_VAULT_INTERFERENCE_BOUND); a looser bound is refused fail-closed. Writes whose activation overlap with existing memory exceeds the bound are quarantined (default) or refused."
    },
    "sparse_update": {
     "type": "boolean",
     "default": false,
     "description": "#874: sparse update mode — touches only the activated subset of state (body slot, activated links), never disturbs neighbors: no salience inflation on re-assert, caller links admitted only when their target is activated by the new body, no near-duplicate absorption on insert."
    },
    "hints": {
     "type": "array",
     "items": {
      "type": "string"
     },
     "maxItems": 3,
     "description": "#919: optional 1-3 prospective query hints — natural-language phrasings that should retrieve this entity, indexed into FTS5 alongside the body (vocabulary-gap recall). Default-off: hints are rejected unless the server runs with PERSEUS_VAULT_HINTS_ENABLED=1. Hints replace any previously stored hints on update (omit to clear)."
    }
   },
   "required": [
    "category",
    "key",
    "body_json"
   ]
  },
  "outputSchema": {
   "type": "object",
   "properties": {
    "id": {
     "type": "string",
     "description": "Entity ID, e.g. 'mem-a1b2c3d4e5f6'"
    },
    "action": {
     "type": "string",
     "description": "'created' for new entities, 'updated' for existing ones"
    },
    "category": {
     "type": "string",
     "description": "Entity category"
    },
    "key": {
     "type": "string",
     "description": "Entity key"
    },
    "derived_from": {
     "type": "object",
     "description": "Present when derived_from citations were passed: {reinforced: n, not_found: [labels]}"
    },
    "proposed": {
     "type": "boolean",
     "description": "True when the write lacks authoritative admission and must remain reviewable."
    },
    "requires_review": {
     "type": "boolean",
     "description": "Whether the stored write must be reviewed before promotion or authoritative use."
    },
    "provenance": {
     "type": "object",
     "description": "Hash-only admission/provenance state; raw prompts, bodies, credentials, and tool arguments are excluded."
    },
    "outcome_class": {
     "type": "string",
     "enum": [
      "save",
      "drop",
      "block",
      "pending_approval"
     ],
     "description": "Stable four-way admission result. SAVE is durably active; DROP and BLOCK are non-persisting terminal decisions; PENDING_APPROVAL is retained but non-serveable until review."
    },
    "disposition": {
     "type": "string",
     "description": "Existing detailed disposition, such as quarantined; use outcome_class for stable aggregation."
    },
    "admission": {
     "type": "object",
     "description": "Hash-covered, content-minimized admission evidence."
    }
   }
  },
  "annotations": {
   "destructiveHint": true
  },
  "title": "Remember Entity"
 },
 {
  "name": "perseus_vault_forget",
  "description": "Soft-delete an entity by setting archived=1. The entity is hidden from queries but recoverable. Use this to clean up stale or incorrect facts without permanent data loss.",
  "inputSchema": {
   "type": "object",
   "properties": {
    "category": {
     "type": "string",
     "description": "Entity category to archive"
    },
    "key": {
     "type": "string",
     "description": "Entity key to archive"
    },
    "reason": {
     "type": "string",
     "default": "",
     "description": "Reason for archiving, logged for audit trail"
    }
   },
   "required": [
    "category",
    "key"
   ]
  },
  "outputSchema": {
   "type": "object",
   "properties": {
    "found": {
     "type": "boolean",
     "description": "Whether the entity was found and archived"
    },
    "category": {
     "type": "string",
     "description": "Entity category"
    },
    "key": {
     "type": "string",
     "description": "Entity key"
    }
   }
  },
  "annotations": {
   "destructiveHint": true
  },
  "title": "Forget Entity (Soft-Delete)"
 },
 {
  "name": "perseus_vault_journal",
  "description": "Append a structured decision/observation log entry. Uses evaluated/acted/forward pattern: what was considered, what was done, and what happens next. Essential for audit trails and timeline reconstruction. Public admission_source events additionally require an initialized clientInfo.name and an enforce-mode memory.admission.source authority for the exact workspace; caller-supplied identities are never authoritative.",
  "inputSchema": {
   "type": "object",
   "properties": {
    "event_type": {
     "type": "string",
     "default": "decision",
     "description": "Event type: 'decision', 'observation', 'action', 'error'"
    },
    "evaluated": {
     "type": "object",
     "description": "What was evaluated: options considered, context, constraints"
    },
    "acted": {
     "type": "object",
     "description": "What action was taken and why"
    },
    "forward": {
     "type": "object",
     "description": "What the plan is going forward"
    },
    "category": {
     "type": "string",
     "description": "Related entity category for linking"
    },
    "key": {
     "type": "string",
     "description": "Related entity key for linking"
    },
    "entity_id": {
     "type": "string",
     "description": "Related entity ID for linking"
    },
    "agent_id": {
     "type": "string",
     "default": "",
     "description": "Agent identity (v1.2.0). Records which agent created this journal event."
    },
    "workspace_hash": {
     "type": "string",
     "default": "",
     "description": "Explicit workspace attribution for the journal event; empty string denotes the global partition."
    },
    "requesting_agent_id": {
     "type": "string",
     "description": "Transport-stamped caller identity; required for admission_source events."
    },
    "source_attestation": {
     "type": "string",
     "minLength": 64,
     "maxLength": 64,
     "pattern": "^[0-9a-fA-F]{64}$",
     "description": "HMAC-SHA256 attestation over the canonical admission-source fields; required for public admission_source events and never stored."
    }
   },
   "required": [],
   "allOf": [
    {
     "if": {
      "properties": {
       "event_type": {
        "const": "admission_source"
       }
      },
      "required": [
       "event_type"
      ]
     },
     "then": {
      "required": [
       "evaluated",
       "workspace_hash",
       "requesting_agent_id",
       "source_attestation"
      ],
      "properties": {
       "evaluated": {
        "type": "object",
        "required": [
         "record_digest",
         "source_identity",
         "workspace_hash",
         "actor_kind",
         "actor_identity"
        ],
        "properties": {
         "record_digest": {
          "type": "string",
          "pattern": "^[0-9a-fA-F]{64}$"
         },
         "source_identity": {
          "type": "string",
          "minLength": 1
         },
         "workspace_hash": {
          "type": "string",
          "minLength": 1
         },
         "actor_kind": {
          "type": "string",
          "minLength": 1
         },
         "actor_identity": {
          "type": "string",
          "minLength": 1
         }
        }
       },
       "workspace_hash": {
        "minLength": 1
       },
       "requesting_agent_id": {
        "minLength": 1
       },
       "source_attestation": {
        "type": "string",
        "minLength": 64,
        "maxLength": 64,
        "pattern": "^[0-9a-fA-F]{64}$"
       }
      }
     }
    }
   ]
  },
  "outputSchema": {
   "type": "object",
   "properties": {
    "id": {
     "type": "string",
     "description": "Journal event ID"
    },
    "event_type": {
     "type": "string",
     "description": "Event type recorded"
    },
    "created_at_unix_ms": {
     "type": "integer",
     "description": "Creation timestamp in unix milliseconds"
    }
   }
  },
  "annotations": {
   "destructiveHint": true
  },
  "title": "Append Journal Entry"
 },
 {
  "name": "perseus_vault_capture",
  "description": "Opt-in in-session memory capture (#520): distill a session transcript or insight payload into durable memory entities the moment a problem is solved, instead of waiting for a scheduled harvest. Splits the payload into candidate notes (headed sections, paragraphs, or JSONL records — auto-detected), classifies each by cheap local signals into root-cause / pitfall / decision / pattern / takeaway, and writes each through the normal remember path with source='capture' (layer buffer, moderate importance). Fully local and deterministic by default — no LLM, no network; pass llm=true to distill via the configured --llm-endpoint instead (falls back to the rule-based path on any LLM failure or timeout). Anti-flood by design: near-duplicate merging stays ON (a re-captured solved problem merges into the existing memory), same-headline notes update in place, and writes are capped per invocation with dropped notes reported. Nothing runs automatically — capture happens only when this tool (or the `perseus-vault capture` CLI verb) is explicitly invoked, e.g. from an on_insight or SessionEnd lifecycle hook (run `maintain` after end-of-session capture).",
  "inputSchema": {
   "type": "object",
   "properties": {
    "text": {
     "type": "string",
     "description": "The transcript / insight payload to distill. Plain text, markdown (headed sections become separate notes), or JSONL (one note per record, using its content/text/insight/lesson/summary/message field)."
    },
    "workspace_hash": {
     "type": "string",
     "description": "Workspace hash to scope the captured entities to. Omit for unscoped (global) capture."
    },
    "agent_id": {
     "type": "string",
     "description": "Agent ID recorded on the captured entities."
    },
    "max_entities": {
     "type": "integer",
     "default": 20,
     "description": "Anti-flood cap: max entities written by this invocation (1-20; callers can lower the cap, not raise it). Notes beyond the cap are dropped and counted in the result."
    },
    "dry_run": {
     "type": "boolean",
     "default": false,
     "description": "Distill and return the would-be notes without writing anything."
    },
    "llm": {
     "type": "boolean",
     "default": false,
     "description": "Distill via the configured LLM endpoint instead of the local rule-based distiller. Requires --llm-endpoint; falls back to the rule-based path on any LLM failure (the result's llm_fallback field says why)."
    },
    "consume": {
     "type": "boolean",
     "default": false,
     "description": "#563: after a SUCCESSFUL non-dry-run capture, atomically remove exactly the captured regions from source_file (temp file + rename, leaving a <source_file>.bak). Scoped to captured records only — surrounding headers/rules/pointers are left untouched. No-op under dry_run, when nothing was captured, or when source_file is unset, so it can never delete content that was not durably stored. Use it to keep a host-inlined write-buffer (e.g. an AGENTS.local.md the agent loads every turn) from accumulating already-stored blocks forever. The result reports 'consumed' (regions removed) and 'source_backup'."
    },
    "source_file": {
     "type": "string",
     "description": "#563: path to the file the payload came from. Required for consume to have anything to prune; ignored when consume is false."
    },
    "evidence": {
     "type": "object",
     "description": "Write-time evidence envelope for captured notes. Omit only for legacy_unknown compatibility.",
     "properties": {
      "capture_mode": {
       "type": "string",
       "enum": [
        "snapshot",
        "hash_only",
        "pointer_only",
        "not_requested",
        "capture_failed",
        "legacy_unknown"
       ]
      },
      "resolved_value": {
       "description": "Resolved source value retained at capture time"
      },
      "content_sha256": {
       "type": "string"
      },
      "source_system": {
       "type": "string"
      },
      "source_ref": {
       "type": "string"
      },
      "captured_at_unix_ms": {
       "type": "integer"
      },
      "replayable": {
       "type": "boolean"
      }
     },
     "required": [
      "capture_mode",
      "captured_at_unix_ms",
      "replayable"
     ]
    }
   },
   "required": [
    "text"
   ]
  },
  "outputSchema": {
   "type": "object",
   "properties": {
    "captured": {
     "type": "integer",
     "description": "Number of notes distilled (and written, unless dry_run)"
    },
    "created": {
     "type": "integer",
     "description": "Notes that created a new entity"
    },
    "updated": {
     "type": "integer",
     "description": "Notes that updated an existing entity in place (same category+key)"
    },
    "merged": {
     "type": "integer",
     "description": "Notes merged into an existing near-duplicate entity by the trigram dedup (the capture flood control)"
    },
    "candidates": {
     "type": "integer",
     "description": "Candidate notes found in the payload before capping"
    },
    "dropped": {
     "type": "integer",
     "description": "Candidate notes dropped by the per-invocation cap"
    },
    "dry_run": {
     "type": "boolean",
     "description": "True when nothing was written"
    },
    "distiller": {
     "type": "string",
     "description": "'rule_based' or 'llm' — which distiller produced the notes"
    },
    "llm_fallback": {
     "type": "string",
     "description": "Present when llm=true was requested but the rule-based path was used; says why"
    },
    "notes": {
     "type": "array",
     "items": {
      "type": "object"
     },
     "description": "Per-note report: {id, key, type, summary, action}"
    },
    "message": {
     "type": "string",
     "description": "Unambiguous empty state when the payload contained nothing durable"
    },
    "consumed": {
     "type": "integer",
     "description": "#563: number of captured regions removed from source_file (0 unless consume=true and the prune ran). See source_backup / consume_skipped / consume_error."
    },
    "source_backup": {
     "type": "string",
     "description": "#563: path to the pre-prune backup (<source_file>.bak) written when consumed > 0"
    }
   }
  },
  "title": "Capture Session Insights"
 },
 {
  "name": "perseus_vault_admission_decide",
  "description": "#1107: resolve a proposed trust-admission candidate through an explicit operator decision. approve re-signs the pending evidence as SAVE and activates the existing row through the verified writer; reject requires rejection_class=drop or block, re-signs the evidence as that terminal class, archives the row, and never serves it. Both transitions are hash-only in the response, record an admission_review_started intent before mutation, and append a completed admission_approved/admission_rejected receipt only after durable transition. Public calls require an initialized clientInfo.name and an enforce-mode memory.admission.review authority for the exact workspace.",
  "inputSchema": {
   "type": "object",
   "properties": {
    "category": {
     "type": "string",
     "minLength": 1,
     "description": "Candidate entity category"
    },
    "key": {
     "type": "string",
     "minLength": 1,
     "description": "Candidate entity key"
    },
    "workspace_hash": {
     "type": "string",
     "minLength": 1,
     "description": "Exact non-empty workspace scope of the candidate"
    },
    "requesting_agent_id": {
     "type": "string",
     "minLength": 1,
     "description": "Operator/reviewer identity stamped into the audit event"
    },
    "decision": {
     "type": "string",
     "enum": [
      "approve",
      "reject"
     ]
    },
    "rejection_class": {
     "type": "string",
     "enum": [
      "drop",
      "block"
     ],
     "description": "Required when decision=reject"
    },
    "reason": {
     "type": "string",
     "minLength": 1,
     "description": "Bounded non-empty review reason; the response stores only its SHA-256"
    }
   },
   "required": [
    "category",
    "key",
    "workspace_hash",
    "requesting_agent_id",
    "decision",
    "reason"
   ],
   "allOf": [
    {
     "if": {
      "properties": {
       "decision": {
        "const": "reject"
       }
      }
     },
     "then": {
      "required": [
       "rejection_class"
      ]
     }
    }
   ]
  },
  "outputSchema": {
   "type": "object",
   "properties": {
    "ok": {
     "type": "boolean"
    },
    "id": {
     "type": "string"
    },
    "category": {
     "type": "string"
    },
    "key": {
     "type": "string"
    },
    "decision": {
     "type": "string",
     "enum": [
      "approve",
      "reject"
     ]
    },
    "outcome_class": {
     "type": "string",
     "enum": [
      "save",
      "drop",
      "block"
     ]
    },
    "status": {
     "type": "string"
    },
    "serveable": {
     "type": "boolean"
    },
    "audit_event_id": {
     "type": "string"
    },
    "reason_sha256": {
     "type": "string"
    }
   }
  }
 }
]""")


class MCPClient:
    """Minimal MCP HTTP client for Perseus Vault; returns {ok, text, data}."""

    PROTOCOL = "2025-06-18"
    CLIENT_INFO = {"name": "hermes-perseus-vault", "version": "0.2.0"}

    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self.next_id = 1

    def _request(self, method: str, params: dict | None = None,
                 timeout: float = 30.0) -> dict:
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        self.next_id += 1
        if params is not None:
            message["params"] = params

        data = json.dumps(message).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body[:400]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Connection error: {e.reason}")
        if not body.strip():
            return {}
        # Defensive: streamable HTTP may frame with SSE even when JSON was
        # requested. Prefer JSON; fall back to the last data: payload.
        if body.lstrip().startswith(("event:", "data:")):
            lines = [l[5:].strip() for l in body.splitlines()
                     if l.startswith("data:")]
            body = lines[-1] if lines else body
        return json.loads(body)

    def initialize(self) -> dict:
        return self._request(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL,
                "capabilities": {},
                "clientInfo": self.CLIENT_INFO,
            },
        )

    def list_tools(self) -> list:
        result = self._request("tools/list")
        return result.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None,
                  timeout: float = 30.0) -> dict:
        """Call a Vault MCP tool; returns {"ok": bool, "text": str, "data": Any}."""
        raw = self._request("tools/call",
                            {"name": name, "arguments": arguments or {}},
                            timeout=timeout)
        if "error" in raw:
            return {"ok": False, "text": json.dumps(raw["error"]), "data": None}
        result = raw.get("result", {}) or {}
        is_error = bool(result.get("isError", False))
        parts = [c.get("text", "") for c in result.get("content", [])
                 if c.get("type") == "text"]
        text = "\n".join(p for p in parts if p)
        data = result.get("structuredContent")
        if not text and data is not None:
            text = json.dumps(data)
        return {"ok": not is_error, "text": text, "data": data}


class PerseusVaultProvider(MemoryProvider):
    """Perseus Vault memory provider via MCP HTTP with lifecycle hooks."""

    name = "perseus-vault"
    # Hermes checkpoint contract (API v2, docs: developer-guide/
    # memory-provider-plugin#pre-compress-checkpoints-fail-closed):
    # a successful on_pre_compress() return == durable checkpoint committed;
    # any failure must raise. Enforcement stays operator-gated
    # (compression.checkpoint_required, default off in this deployment).
    pre_compress_checkpoint_api_version = 2

    # Redaction applied to every capture payload BEFORE it reaches the
    # shared vault: session transcripts may contain credentials and LAN
    # addresses, and vault memory is readable by other clients.
    _REDACT_RES = (
        re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+=*"),
        re.compile(r"(?i)\b(?:api[_-]?key|token|password|passwd|secret)\b\s*[:=]\s*\S+"),
        re.compile(r"\b(?:sk-|ghp_|gho_|xoxb_|xoxp_|npm_)[A-Za-z0-9_\-]{10,}"),
        re.compile(r"\b(?:192\.168|10\.\d+|172\.(?:1[6-9]|2[0-9]|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    )

    @classmethod
    def _redact(cls, text: str) -> str:
        """Strip credentials and private-network addresses from capture text."""
        for rx in cls._REDACT_RES[:-1]:
            text = rx.sub("[REDACTED]", text)
        text = cls._REDACT_RES[-1].sub("[redacted-ip]", text)
        return text

    @staticmethod
    def _deaccent(text: str) -> str:
        """Fold Latin accents to ASCII (diseñar -> disenar).

        Mitigates the server preview_cap panic: byte-indexed truncation
        inside a multi-byte UTF-8 char kills the request (HTTP 500) and
        Spanish text hits it ~1/3 of the time. Only combining marks are
        stripped, so emoji/CJK pass through untouched; ASCII fast-path
        keeps the call free for English content."""
        if text.isascii():
            return text
        return "".join(c for c in unicodedata.normalize("NFKD", text)
                       if not unicodedata.combining(c))

    def __init__(self):
        self._client: Optional[MCPClient] = None
        self._session_id: str = ""
        self._agent_context: str = "primary"
        self._agent_label: str = "default"
        self._workspace_hash: str = ""
        self._enabled: bool = False
        self._prefetched: str = ""
        self._prefetch_lock = threading.Lock()
        self._turn_buffer: List[Dict[str, str]] = []
        self._captured_hashes: set = set()
        self._session_label: str = ""
        self._resumed: bool = False
        self._checkpoint_keys: set = set()
        self._config: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Config resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _config_section() -> Dict[str, Any]:
        try:
            from hermes_cli.config import cfg_get, load_config
            config = load_config()
            section = cfg_get(config, "memory", "perseus-vault")
            return section if isinstance(section, dict) else {}
        except Exception:
            return {}

    def _json_config(self) -> Dict[str, str]:
        # Bound at initialize() when present. The ambient resolution below is
        # reachable ONLY pre-initialize (is_available); never from worker
        # threads or after activation.
        home = getattr(self, "_hermes_home", None)
        if home is None:
            try:
                from hermes_constants import get_hermes_home
                home = get_hermes_home()
            except Exception:
                home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        path = Path(home) / "perseus-vault.json"
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _load_config(self) -> None:
        section = self._config_section()
        jcfg = self._json_config()
        url = (os.getenv("PERSEUS_VAULT_URL", "").strip()
               or os.getenv("PERSEUS_MCP_URL", "").strip()
               or str(section.get("url", "") or "").strip()
               or str(jcfg.get("url", "") or "").strip()
               or _DEFAULT_URL)
        token = (os.getenv("PERSEUS_VAULT_MCP_TOKEN", "").strip()
                 or os.getenv("MCP_PERSEUS_VAULT_API_KEY", "").strip()
                 or str(section.get("token", "") or "").strip()
                 or str(jcfg.get("api_key", "") or "").strip())
        ws = (os.getenv("PERSEUS_VAULT_WORKSPACE", "").strip()
              or str(section.get("workspace_hash", "") or "").strip()
              or str(jcfg.get("workspace_hash", "") or "").strip())
        self._config = {"url": url, "api_key": token, "workspace_hash": ws}
        self._workspace_hash = ws

    def _ws_args(self) -> Dict[str, str]:
        return ({"workspace_hash": self._workspace_hash}
                if self._workspace_hash else {})

    # ------------------------------------------------------------------
    # Core lifecycle
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Quick health check — no network calls per contract."""
        self._load_config()
        return bool(self._config.get("url") and self._config.get("api_key"))

    def initialize(self, session_id: str, **kwargs) -> None:
        # Profile binding (Hermes storage-path contract): every per-profile
        # path used after this point derives from the hermes_home kwarg.
        # A missing kwarg is a host-contract violation: fail closed.
        self._hermes_home = Path(kwargs["hermes_home"])
        self._session_id = session_id
        self._agent_context = kwargs.get("agent_context", "primary")
        self._agent_label = (kwargs.get("agent_id")
                             or os.environ.get("HERMES_PROFILE")
                             or "default")
        # Stable identity for checkpoint scoping: gateway_session_key
        # survives process restarts for the same chat (session_id does
        # not), which is what makes post-restart replay possible.
        self._session_label = str(kwargs.get("gateway_session_key")
                                  or session_id or "")
        self._resumed = bool(kwargs.get("resumed", False))
        if self._resumed:
            logger.info("perseus-vault: resumed session; draft checkpoints "
                        "left unconsumed by a previous process will be "
                        "replayed into this session's final distillation")
        self._load_config()
        if not self._config.get("api_key"):
            logger.warning(
                "perseus-vault: no token configured (env PERSEUS_VAULT_MCP_TOKEN, "
                "config memory.perseus-vault.token, or perseus-vault.json); "
                "provider disabled")
            self._enabled = False
            return
        try:
            self._client = MCPClient(self._config["url"], self._config["api_key"])
            init_result = self._client.initialize()
            if "error" in init_result:
                raise RuntimeError(f"MCP initialize failed: {init_result['error']}")
            tools = self._client.list_tools()
            logger.info(
                "Perseus Vault connected: %s MCP tools available "
                "(%d exposed to agent)",
                len(tools), len(TOOL_ALLOWLIST))
            self._enabled = True
        except Exception as e:
            logger.warning("perseus-vault: connect failed, provider disabled: %s", e)
            self._client = None
            self._enabled = False

    def shutdown(self) -> None:
        self._client = None
        self._enabled = False

    # ------------------------------------------------------------------
    # Prompt + prefetch (the actual injection surface)
    # ------------------------------------------------------------------

    def system_prompt_block(self) -> str:
        if not self._enabled:
            return ""
        return (
            "## Long-term memory: Perseus Vault\n"
            "A shared Perseus Vault is connected as the external memory provider. "
            "Relevant Vault memories are prefetched and injected before turns when "
            "available. Use `perseus_vault_recall` to search it before asking the "
            "user to repeat context, and `perseus_vault_remember` to persist "
            "durable facts, decisions, and corrections. Never store secret values."
        )

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not self._enabled or not self._client or not query.strip():
            return

        def _work() -> None:
            block = self._build_recall_block(query, call_timeout=_WARM_CALL_TIMEOUT)
            with self._prefetch_lock:
                self._prefetched = block

        # Host threading contract (v0.23.1): every memory-provider background
        # job goes through spawn_context_thread so the owning profile's
        # contextvars (HERMES_HOME scope, secrets) travel with the thread.
        # No bare-thread fallback exists by design; incompatible hosts are
        # stopped by the installer gate, not by degrading here.
        spawn_context_thread(_work, name="perseus-vault-prefetch").start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return recalled context; Hermes injects non-empty results."""
        with self._prefetch_lock:
            block = self._prefetched
            self._prefetched = ""
        if block:
            return block
        if not self._enabled or not self._client or not query.strip():
            return ""
        try:
            # Cold-start path (issue #753): bounded synchronous recall inside
            # the host's prefetch thread budget.
            return self._build_recall_block(query, call_timeout=_SYNC_CALL_TIMEOUT)
        except Exception as e:
            logger.debug("perseus-vault: synchronous prefetch failed: %s", e)
            return ""

    def _build_recall_block(self, query: str,
                            call_timeout: float = _WARM_CALL_TIMEOUT) -> str:
        items: List[Dict[str, Any]] = []
        seen: set = set()

        res = self._client.call_tool(
            "perseus_vault_recall_when",
            {"context": query, "limit": 4, **self._ws_args()},
            timeout=call_timeout)
        for it in self._extract_items(res):
            if it.get("id") not in seen:
                seen.add(it.get("id"))
                items.append(it)

        res = self._client.call_tool(
            "perseus_vault_recall",
            {"query": query, "limit": _PREFETCH_LIMIT,
             "preview_cap": _PREVIEW_CAP, **self._ws_args()},
            timeout=call_timeout)
        for it in self._extract_items(res):
            if it.get("id") not in seen:
                seen.add(it.get("id"))
                items.append(it)

        if not items:
            return ""
        lines = ["## Recalled from Perseus Vault (shared memory)"]
        for it in items[:_PREFETCH_LIMIT + 2]:
            text = self._item_text(it)
            if text:
                lines.append(f"- [{it.get('category', '')}/{it.get('key', '')}] {text}")
        return "\n".join(lines) if len(lines) > 1 else ""

    @staticmethod
    def _extract_items(res: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not res.get("ok"):
            return []
        payload = res.get("data")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(res.get("text", "") or "{}")
            except Exception:
                return []
        inner = payload.get("result")
        if isinstance(inner, str):
            try:
                payload = json.loads(inner)
            except Exception:
                return []
        items = payload.get("entities") or payload.get("items") or []
        return items if isinstance(items, list) else []

    @staticmethod
    def _item_text(it: Dict[str, Any]) -> str:
        text = it.get("summary") or it.get("content") or it.get("body") or ""
        if isinstance(text, (dict, list)):
            text = json.dumps(text)
        text = str(text).strip().replace("\n", " ")
        return text[:_PREVIEW_CAP]

    # ------------------------------------------------------------------
    # Turn hooks
    # ------------------------------------------------------------------

    def on_turn_start(self, turn: int, message: str, **kwargs) -> None:
        """Warm the prefetch cache for the next turn (results are NOT
        injected by Hermes; only prefetch()'s return value is)."""
        if turn == 1 and message:
            self.queue_prefetch(message)

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages=None) -> None:
        """Non-blocking: buffer locally, flush at session end."""
        if not self._enabled:
            return
        self._turn_buffer.append({
            "user": (user_content or "")[:2000],
            "assistant": (assistant_content or "")[:2000],
        })
        if len(self._turn_buffer) > _TURN_BUFFER_CAP:
            self._turn_buffer = self._turn_buffer[-_TURN_BUFFER_CAP:]

    # ------------------------------------------------------------------
    # Mid-process session rebinding (Hermes on_session_switch contract)
    # ------------------------------------------------------------------

    def on_session_switch(self, new_session_id: str, *,
                         parent_session_id: str = "", reset: bool = False,
                         rewound: bool = False, **kwargs) -> None:
        """Rebind per-session state when session_id rotates without
        teardown (/resume, /branch, /reset, /new, /undo, compression).

        Contract (design rev.4, review conditions 2026-11-10):
        - rewound (=/undo): same id, transcript truncated -> the pending
          turn buffer is dropped (it may hold undone content).
        - reset: genuinely new conversation -> buffers cleared, id
          rebound.
        - plain switch (/branch, /resume mid-process): id rebound;
          buffered turns are PRESERVED with their original association.
          The host never re-passes gateway_session_key: chat identity
          (_session_label) is deliberately untouched, and entries are
          never inherited by, nor reassigned to, another record.
        - ownership validation: a parent_session_id that does not match
          this instance warns explicitly; the rebinding still applies
          (the host is authoritative) but entries are never re-tagged.
        - This method performs NO Vault calls: it never captures,
          distills, marks or emits receipts, and never touches
          checkpoints or receipts.
        """
        if not self._enabled:
            return
        if parent_session_id and parent_session_id != self._session_id:
            logger.warning(
                "perseus-vault: on_session_switch parent_session_id (%s) "
                "does not match this instance's session (%s); rebinding "
                "without touching buffered turns",
                parent_session_id[:16], (self._session_id or "")[:16])
        if rewound:
            # /undo: pending buffer content was just undone — invalid.
            self._turn_buffer.clear()
            return
        if reset:
            self._turn_buffer.clear()
        self._session_id = new_session_id

    # ------------------------------------------------------------------
    # Pre-compress checkpoints (Hermes checkpoint API v2)
    # ------------------------------------------------------------------

    _CHECKPOINT_CATEGORY = "checkpoint"
    _RECEIPT_CATEGORY = "hermes-capture-receipt"

    def _scope_key(self) -> str:
        """Capture-scope fingerprint: session identity + profile + context.

        Two different sessions holding identical text never collide, and
        per-profile / subagent scopes stay separated in the Vault."""
        raw = "%s|%s|%s" % (self._session_label, self._agent_label,
                            self._agent_context)
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    @staticmethod
    def _evidence_rows(messages: List[Dict[str, Any]]) -> List[Any]:
        """user/assistant prose rows only.

        Mirrors the host-side API v2 normalization as defense-in-depth:
        tool result rows, system rows, empty or tool_calls-only assistant
        rows and prior _compressed_summary rows are dropped.
        KNOWN LIMITATION (documented deliberately): facts that appear
        ONLY inside tool output are not preserved by the checkpoint path;
        the host normalizes evidence this way by contract, and re-adding
        tool output would re-introduce the noise the last quarantine
        cleanup removed. The final distillation stays the semantic layer.
        """
        rows: List[Any] = []
        for m in messages or []:
            role = str(m.get("role", "")).lower()
            if role not in ("user", "assistant"):
                continue
            content = m.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if m.get("_compressed_summary") or \
                    content.lstrip().startswith("[compressed summary"):
                continue
            rows.append((role.upper(), content.strip()))
        return rows

    def on_pre_compress(self, messages: List[Dict[str, Any]], *,
                       require_checkpoint: bool = False) -> str:
        """Durable pre-compress checkpoint (contract API v2).

        Success == the checkpoint is durably committed. ANY failure
        raises — never partial success — so the operator can arm
        compression.checkpoint_required without touching this code.
        Idempotent by contract: the entity key is a content+scope
        digest and remember upserts by (category, key), so retries and
        overlapping transcripts deduplicate instead of accumulating."""
        if not self._enabled or not self._client:
            raise RuntimeError(
                "perseus-vault: client offline, cannot checkpoint")
        rows = self._evidence_rows(messages)
        if not rows:
            raise RuntimeError(
                "perseus-vault: no user/assistant evidence to checkpoint")
        text = self._deaccent("\n".join(
            f"{role}: {self._redact(content[:1000])}"
            for role, content in rows))
        text = text[:_SESSION_CAPTURE_MAX_CHARS]
        digest = hashlib.sha1(text.encode()).hexdigest()[:12]
        key = f"ckpt-{self._scope_key()}-{digest}"
        body = json.dumps({
            "kind": "hermes-pre-compress-checkpoint",
            "session": self._session_label,
            "agent_context": self._agent_context,
            "evidence": text,
        }, ensure_ascii=False)
        res = self._client.call_tool(
            "perseus_vault_remember",
            {"category": self._CHECKPOINT_CATEGORY, "key": key,
             "body_json": body, "type": "reference", "status": "draft",
             "importance": 0.3,
             "tags": ["checkpoint", "hermes"],
             "external_refs": [{"ref_type": "hermes_session",
                                "ref_value": self._session_label,
                                "relationship": "mentions"}],
             "agent_id": self._agent_label,
             **self._ws_args()},   # F-B: writes scope-symmetric with reads
            timeout=15)
        if not self._write_confirmed(res):
            raise RuntimeError(
                "perseus-vault: checkpoint write NOT durable (rejected, "
                "transport error, or write-quarantined qrn id): %s"
                % (res.get("text", "") or "")[:200])
        self._checkpoint_keys.add(key)
        return f"checkpoint: {key}"

    _RECALL_PAGE = 50
    _RECALL_MAX_ITEMS = 1000
    # Transient 'no_match' hardening (real-server E2E follow-up of
    # 8bcb008): the server occasionally answers a ref-scoped listing
    # with an empty 'no_match' diagnostic moments after a write while
    # the identical query succeeds seconds later. Bounded, scoped
    # re-reads only — never a global retry inside _recall_pages.
    _NO_MATCH_RETRIES = 2          # at most two additional reads
    _NO_MATCH_RETRY_DELAY = 1.0    # seconds between reads

    def _recall_pages(self, args_base: dict) -> Tuple[List[Any], str]:
        """Paginate a recall until a SHORT page. Returns
        (hits, state) with state EXPLICITLY one of:
          "complete"   — every page was retrieved (short page reached)
          "failed"     — a page raised or was rejected (ok=False)
          "truncated"  — the defensive _RECALL_MAX_ITEMS cap was hit
        The cap is a PROTECTION, never a license to continue:
        TRUNCATED is treated like a failure by callers. An incomplete
        read is UNKNOWN state, never a valid partial answer (R3)."""
        hits: List[Any] = []
        offset = 0
        state = "complete"
        diag_reason: Optional[str] = None   # first diagnostic seen, if any
        while offset < self._RECALL_MAX_ITEMS:
            a = dict(args_base)
            a["limit"] = self._RECALL_PAGE
            a["offset"] = offset
            try:
                res = self._client.call_tool("perseus_vault_recall", a,
                                             timeout=10)
            except Exception:
                state = "failed"
                break
            if not res.get("ok"):
                state = "failed"
                break
            d = res.get("data")
            if not isinstance(d, dict):
                try:
                    d = json.loads(res.get("text") or "{}")
                except Exception:
                    d = {}
            if isinstance(d, dict):
                diag = d.get("diagnostic")
                if (isinstance(diag, dict)
                        and isinstance(diag.get("reason"), str)
                        and diag_reason is None):
                    diag_reason = diag["reason"]
            batch = self._recall_hits(res)
            hits.extend(batch)
            if len(batch) < self._RECALL_PAGE:
                break
            offset += self._RECALL_PAGE
        else:
            state = "truncated"
            logger.warning(
                "perseus-vault: recall pagination hit safety cap %d (%s)",
                self._RECALL_MAX_ITEMS, args_base.get("category"))
        # Passive only: the diagnostic the server attached to the
        # pages it served (never used for retries inside this
        # helper). Callers that need it read it right after their
        # own listing (see _draft_checkpoints).
        self._last_recall_diagnostic = diag_reason
        return hits, state

    def _draft_checkpoints(self) -> Tuple[List[Any], str]:
        """Unconsumed (status=draft) checkpoints for THIS session scope
        (fully paginated). Returns (items, state) on EVERY path."""
        if not self._session_label:
            # initialize() sets _session_label from gateway_session_key
            # or session_id; the host always supplies one at a session
            # boundary. A missing label therefore means INVALID context,
            # not a legitimate empty scope: recovery coverage cannot be
            # proven, so it is non-processable (fail-closed).
            return [], "failed"
        out: List[Any] = []
        self._draft_initial_diagnostic = None
        raw, state = self._recall_pages(self._draft_listing_scope())
        if not raw:
            # exact diagnostic reason reported by the server for the
            # initial listing ('no_match', another reason, or None if
            # absent) — real data from the response, not inferred
            # from emptiness.
            self._draft_initial_diagnostic = getattr(
                self, "_last_recall_diagnostic", None)
        if state != "complete":
            # R3 rule: an incomplete recovery (FAILED or TRUNCATED) is
            # never processed as if it were complete. on_session_end
            # aborts the cycle; nothing is lost — drafts are re-read
            # fully by the next cycle.
            logger.warning(
                "perseus-vault: draft-checkpoint listing %s; "
                "consumption cycle will be aborted", state.upper())
        out.extend(self._parse_draft_items(raw))
        return out, state

    @staticmethod
    def _parse_draft_items(raw: List[Any]) -> List[Any]:
        """Shared parser for draft-listing pages (used by
        _draft_checkpoints and the bounded no_match re-read)."""
        out: List[Any] = []
        for h in raw:
            # F-A (live-server evidence 2026-11-07): agent-attributed
            # writes land in the server's 'proposed' lifecycle tier even
            # when 'draft' was requested, and recall DOES serve them
            # (ref-scoped, stable: no auto-expiry TTL, no auto-promote,
            # no self-archive). Unconsumed checkpoints are therefore
            # exactly {draft, proposed}; 'expired'/'deprecated'/active
            # rows are not. Correctness never depends on the tier:
            # consume-receipts gate re-distillation regardless.
            if not isinstance(h, dict) or h.get("status") not in ("draft", "proposed"):
                continue
            body = h.get("body_json")
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except Exception:
                    continue
            if isinstance(body, dict) and body.get("evidence"):
                out.append((h.get("key") or h.get("id") or "", body))
        return out

    def _draft_listing_scope(self) -> Dict[str, Any]:
        return {"query": "", "category": self._CHECKPOINT_CATEGORY,
                "ref_type": "hermes_session",
                "ref_value": self._session_label,
                **self._ws_args()}   # F-B: read scope == write scope

    def _probe_draft_listing(self) -> Tuple[List[Any], str, bool]:
        """ONE bounded page of the draft-checkpoint listing; used as a
        retry attempt only, never inside _recall_pages. Returns
        (items, state, saw_no_match). saw_no_match is True ONLY when
        the server itself served an empty page carrying
        diagnostic.reason == 'no_match' — its explicit 'queried and
        found nothing' signal. Transport/parse errors and any other
        diagnostic return saw_no_match=False so the retry band never
        covers them. This read never mutates vault state."""
        if not self._session_label:
            return [], "failed", False
        a = self._draft_listing_scope()
        a["limit"] = self._RECALL_PAGE
        a["offset"] = 0
        try:
            res = self._client.call_tool("perseus_vault_recall", a,
                                         timeout=10)
        except Exception:
            return [], "failed", False
        if not isinstance(res, dict) or not res.get("ok"):
            return [], "failed", False
        d = res.get("data")
        if not isinstance(d, dict):
            try:
                d = json.loads(res.get("text") or "{}")
            except Exception:
                return [], "failed", False
        if not isinstance(d, dict):
            return [], "failed", False
        batch = self._recall_hits(res)
        if batch:
            state = "complete" if len(batch) < self._RECALL_PAGE else "truncated"
            return self._parse_draft_items(batch), state, False
        diag = d.get("diagnostic")
        saw = bool(isinstance(diag, dict)
                   and diag.get("reason") == "no_match")
        return [], "complete", saw

    @staticmethod
    def _write_confirmed(res: dict) -> bool:
        """ok:true is NOT synonymous with durable persistence (F-C).
        Durable only when: response is a dict, ok:true, no JSON-RPC /
        isError transport failure already folded into ok, and the
        resulting id is a materialized entity (mem-*). A 'qrn-*' id
        means the write went to the server's write-quarantine and is
        NEVER served (verified live: ok:true + qrn id + 'quarantined
        writes are never served'). Missing/unparseable ids are treated
        as unconfirmed."""
        if not isinstance(res, dict) or not res.get("ok"):
            return False
        rid = None
        data = res.get("data")
        if isinstance(data, dict):
            rid = data.get("id")
        if rid is None:
            rid = res.get("id")
        if rid is None:
            try:
                parsed = json.loads(res.get("text") or "")
                if isinstance(parsed, dict):
                    rid = parsed.get("id")
            except Exception:
                pass
        if not isinstance(rid, str) or not rid.startswith("mem-"):
            return False
        return True

    @staticmethod
    def _recall_hits(res: dict) -> List[Any]:
        data = res.get("data") or {}
        if isinstance(data, list):
            return data
        # 'items' is the real server's key (verified live 2026-11-08:
        # structured content is {"items": [...], "total": N,
        # "retrieval_profile": ...}); 'results'/'entities' kept for
        # compatibility. Blindness to 'items' was the real cause of
        # empty ref-scoped listings against the live server.
        hits = (data.get("items") or data.get("results")
                or data.get("entities") or [])
        return hits if isinstance(hits, list) else []

    def _consumed_checkpoint_keys(self) -> Tuple[Set[str], str]:
        """Checkpoint keys whose distillation ALREADY persisted.

        Consume-receipts are written only after a verified capture, so
        a receipt means the content is durable even if the draft was
        never marked (crash between capture and marking). Keyed per
        checkpoint, so replay is item-level idempotent — a post-restart
        transcript can never reproduce the pre-crash transcript digest,
        which is why a transcript-keyed receipt was the wrong identity.

        Returns (keys, state) with state "complete"/"failed"/
        "truncated" (see _recall_pages). An incomplete receipt lookup
        must never decide what is safe to distill — items whose
        receipts sit on the unread pages would be re-distilled.
        Callers must fail the cycle closed instead of proceeding."""
        if not self._session_label:
            # same contract as _draft_checkpoints: no session identity
            # = unprovable scope = non-processable (see above)
            return set(), "failed"
        out = set()
        try:
            raw, state = self._recall_pages(
                {"query": "", "category": self._RECEIPT_CATEGORY,
                 "ref_type": "hermes_session",
                 "ref_value": self._session_label,
                 **self._ws_args()})   # F-B: read scope == write scope
            for h in raw:
                if not isinstance(h, dict):
                    continue
                body = h.get("body_json")
                if isinstance(body, str):
                    try:
                        body = json.loads(body)
                    except Exception:
                        continue
                if isinstance(body, dict) and body.get("consumed_checkpoint"):
                    out.add(str(body["consumed_checkpoint"]))
        except Exception:
            return out, "failed"
        return out, state

    def _write_consume_receipts(self, processed: List[Any]) -> List[Any]:
        """Called ONLY after the capture call persisted successfully.

        Returns ONLY the items whose receipt write was CONFIRMED by the
        server (res.ok) — a call that raised no exception is NOT
        confirmed persistence. A rejected/failed receipt is recoverable:
        that checkpoint stays draft and a later replay re-distills it
        (bounded duplicate) instead of risking silent loss."""
        confirmed: List[Any] = []
        for key, body in processed:
            if not key:
                continue
            try:
                res = self._client.call_tool(
                    "perseus_vault_remember",
                    {"category": self._RECEIPT_CATEGORY,
                     "key": "consume-%s-%s" % (
                         self._scope_key(),
                         hashlib.sha1(key.encode()).hexdigest()[:16]),
                     "body_json": json.dumps(
                         {"kind": "consume-receipt",
                          "consumed_checkpoint": key,
                          "session": self._session_label}),
                     "type": "reference", "importance": 0.2,
                     "external_refs": [{"ref_type": "hermes_session",
                                        "ref_value": self._session_label,
                                        "relationship": "mentions"}],
                     **self._ws_args()},   # F-B: writes scope-symmetric
                    timeout=10)
            except Exception as e:
                logger.warning(
                    "perseus-vault: consume-receipt write failed (%s): %s",
                    key, e)
                continue
            if not self._write_confirmed(res):
                # F-C: ok:true with a qrn-* id is NOT durable. Leave the
                # checkpoint unconsumed (recoverable): a later replay
                # re-distills it as a bounded duplicate.
                logger.warning(
                    "perseus-vault: consume-receipt NOT confirmed "
                    "(rejected/transport/quarantined) (%s): %s",
                    key, (res.get("text", "") or "")[:200])
                continue
            confirmed.append((key, body))
        return confirmed

    def _mark_checkpoints_consumed(self, checkpoints: List[Any]) -> None:
        """Called ONLY after the final distillation persisted (safe
        consumption). Failure here is non-fatal: the receipt makes a
        later replay skip re-distillation, so idempotency holds."""
        for key, body in checkpoints:
            if not key:
                continue
            try:
                res = self._client.call_tool(
                    "perseus_vault_remember",
                    {"category": self._CHECKPOINT_CATEGORY, "key": key,
                     "body_json": json.dumps(body, ensure_ascii=False),
                     "type": "reference", "status": "deprecated",
                     "importance": 0.3,
                     "tags": ["checkpoint", "hermes", "consumed"],
                     # remember() REPLACES the entity: the session ref
                     # must be re-stated or the marked checkpoint becomes
                     # invisible to session-scoped recovery paths (R4).
                     "external_refs": [{"ref_type": "hermes_session",
                                        "ref_value": self._session_label,
                                        "relationship": "mentions"}],
                     "agent_id": self._agent_label,
                     **self._ws_args()},   # F-B: writes scope-symmetric
                    timeout=10)
                if not self._write_confirmed(res):
                    # F-C marking rule: an unconfirmed marking (rejected,
                    # transport error, or qrn quarantine) must NOT be
                    # treated as consumed. The receipt already protects
                    # against re-distillation; the marking is retried by
                    # the next cycle's confirmed-items path.
                    logger.warning(
                        "perseus-vault: marking NOT confirmed for %s "
                        "(rejected/transport/quarantined): %s",
                        key, (res.get("text", "") or "")[:200])
            except Exception as e:
                logger.warning(
                    "perseus-vault: could not consume checkpoint %s: %s",
                    key, e)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Final distillation for the session (primary contexts only).

        Safe-consumption order (review condition 2): read draft
        checkpoints -> use them as evidence -> distill -> VERIFY
        persistence -> only then mark checkpoints consumed. A crash
        between distillation and marking is recovered idempotently
        through the content-keyed distillation receipt. No global
        consolidate — that is the server cron's nightly job."""
        if not self._enabled or not self._client:
            return
        if self._agent_context != "primary":
            return  # cron/subagent sessions would pollute shared memory
        checkpoints, ck_state = self._draft_checkpoints()
        already, rc_state = self._consumed_checkpoint_keys()
        if ck_state != "complete" or rc_state != "complete":
            # R3 fail-closed, explicit tri-state: a recovery is
            # processed ONLY when provably COMPLETE. FAILED or
            # TRUNCATED on either lookup aborts the cycle: no
            # capture, no receipts, no marking, buffer untouched.
            # Nothing is lost — the next session-end re-reads fully.
            logger.warning(
                "perseus-vault: recovery incomplete (checkpoints=%s, "
                "receipts=%s) — aborting consumption cycle; state "
                "preserved for a later retry", ck_state, rc_state)
            return
        # Transient 'no_match' band (authorized 2026-11-08): fires ONLY
        # when ALL hold — the listing is empty, the server itself
        # reported diagnostic 'no_match', ZERO receipts exist, and BOTH
        # listings completed (FAILED/TRUNCATED aborted above; transport
        # errors yield saw_no_match=False and never re-enter). At most
        # two additional scoped reads, one second apart. Nothing is
        # marked consumed by retries alone: the decision below is the
        # same normal path; if every read stays empty the behavior is
        # unchanged fail-closed (no capture, no receipts, no marking).
        if (not checkpoints and not already and self._session_label
                and ck_state == "complete" and rc_state == "complete"
                and getattr(self, "_draft_initial_diagnostic", None)
                == "no_match"):
            for attempt in range(self._NO_MATCH_RETRIES):
                # one second between reads, including before the first
                time.sleep(self._NO_MATCH_RETRY_DELAY)
                found, probe_state, saw_no_match = self._probe_draft_listing()
                logger.warning(
                    "perseus-vault: empty+no_match checkpoint listing "
                    "with zero receipts — scoped re-read %d/%d: %d found "
                    "(state=%s)", attempt + 1, self._NO_MATCH_RETRIES,
                    len(found), probe_state)
                if found:
                    checkpoints, ck_state = found, probe_state
                    break
                if probe_state != "complete" or not saw_no_match:
                    break
        fresh = [(k, b) for (k, b) in checkpoints if k and k not in already]
        entries = []
        for t in self._turn_buffer:
            if t["user"]:
                entries.append(("USER", t["user"]))
            if t["assistant"]:
                entries.append(("ASSISTANT", t["assistant"]))
        text_parts: List[str] = []
        processed: List[Any] = []   # EXACTLY what enters this capture (R1)
        total = 0
        for ck_key, ck_body in fresh:
            chunk = f"[checkpoint {ck_key[:24]}] {ck_body['evidence']}"
            if total + len(chunk) > _SESSION_CAPTURE_MAX_CHARS:
                if total:
                    # Stop here: the REMAINDER stays draft and is
                    # recovered by a later session-end (partial capture
                    # resumes, nothing is silently consumed).
                    break
                # Oversized single item: include it truncated (its full
                # body remains durable in the Vault entity itself).
                chunk = chunk[:_SESSION_CAPTURE_MAX_CHARS]
            text_parts.append(chunk)
            total += len(chunk)
            processed.append((ck_key, ck_body))
        for role, content in entries:
            if not isinstance(content, str) or not content.strip():
                continue
            chunk = f"{role}: {self._redact(content.strip())}"
            if total + len(chunk) > _SESSION_CAPTURE_MAX_CHARS:
                break
            text_parts.append(chunk)
            total += len(chunk)
        if not text_parts:
            # Nothing new to distill. Only items whose consume-receipt
            # was CONFIRMED earlier are safe to mark consumed; any
            # unprocessed draft stays available for a later session-end.
            self._mark_checkpoints_consumed(
                [(k, b) for (k, b) in checkpoints if k in already])
            return
        # Session-keyed preamble: the capture distiller derives keys from
        # headlines, so a constant preamble collided across every session
        # (interference >=0.9 -> write-quarantine flood).
        transcript = self._deaccent(
            f"[session {self._session_id[:12]}] Session transcript to distill "
            "into durable memories "
            "(facts, decisions, corrections, lessons only; skip chit-chat; "
            "never include secret values):\n\n" + "\n\n".join(text_parts))
        digest = hashlib.sha1(transcript.encode()).hexdigest()[:12]
        if digest not in self._captured_hashes:
            try:
                res = self._client.call_tool(
                    "perseus_vault_capture",
                    {"text": transcript, "max_entities": 5,
                     "agent_id": self._agent_label},
                    timeout=15)
            except Exception as e:
                logger.warning("perseus-vault session capture failed: %s", e)
                return
            if not res.get("ok"):
                logger.warning("perseus-vault session capture rejected: %s",
                               (res.get("text", "") or "")[:200])
                return
            # Persistence VERIFIED -> receipts for PROCESSED items only,
            # then consumption marking for receipt-CONFIRMED items only
            # (R1: unprocessed fresh checkpoints are never consumed;
            # R2: unconfirmed receipts never count as persisted).
            self._captured_hashes.add(digest)
            confirmed = self._write_consume_receipts(processed)
        else:
            confirmed = []
        retryable = [(k, b) for (k, b) in checkpoints if k in already]
        if confirmed or retryable:
            self._mark_checkpoints_consumed(confirmed + retryable)
        self._turn_buffer.clear()

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict] = None) -> None:
        """Mirror built-in memory writes to the Vault."""
        if not self._enabled or not self._client or not content:
            return
        digest = hashlib.sha1(content.encode()).hexdigest()[:8]
        category = "hermes-memory"
        key = f"builtin-{target}-{digest}"
        try:
            if action in ("add", "replace"):
                self._client.call_tool("perseus_vault_remember", {
                    "category": category,
                    "key": key,
                    "body_json": json.dumps({
                        "summary": self._deaccent(content[:600]),
                        "source": "hermes-builtin-memory",
                        "target": target,
                    }),
                    "type": "insight",
                    "tags": ["hermes", "builtin-memory", target],
                    **self._ws_args(),
                }, timeout=15)
            elif action == "remove":
                self._client.call_tool("perseus_vault_forget", {
                    "category": category, "key": key,
                    "reason": "removed from built-in memory",
                    **self._ws_args(),
                }, timeout=15)
        except Exception:
            pass  # Non-blocking

    # ------------------------------------------------------------------
    # Tools (curated allowlist)
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> list:
        # Static: MemoryManager snapshots schemas BEFORE initialize() runs.
        return SCHEMAS

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any],
                         **kwargs) -> str:
        if not self._enabled or not self._client:
            return json.dumps({"success": False,
                               "error": "perseus-vault not connected"})
        if tool_name not in TOOL_ALLOWLIST:
            return json.dumps({"success": False,
                               "error": f"tool {tool_name} is not exposed by "
                                        f"the perseus-vault provider"})
        try:
            call_args = dict(args or {})
            # Accent-fold on the write path: entities this plugin stores then
            # never carry a multi-byte char at the preview-cap byte boundary.
            # body_json arrives as a JSON *string* (often with \uXXXX escapes),
            # so fold inside the decoded values, not over the serialized text.
            if tool_name == "perseus_vault_remember" and isinstance(
                    call_args.get("body_json"), str):
                try:
                    def _fold(o):
                        if isinstance(o, str):
                            return self._deaccent(o)
                        if isinstance(o, dict):
                            return {k: _fold(v) for k, v in o.items()}
                        if isinstance(o, list):
                            return [_fold(v) for v in o]
                        return o
                    call_args["body_json"] = json.dumps(
                        _fold(json.loads(call_args["body_json"])),
                        ensure_ascii=False)
                except ValueError:
                    call_args["body_json"] = self._deaccent(
                        call_args["body_json"])
            elif tool_name == "perseus_vault_capture" and isinstance(
                    call_args.get("text"), str):
                call_args["text"] = self._deaccent(call_args["text"])
            if tool_name in ("perseus_vault_recall", "perseus_vault_recall_when",
                             "perseus_vault_semantic_search", "perseus_vault_context",
                             "perseus_vault_remember", "perseus_vault_forget",
                             "perseus_vault_capture"):
                call_args.update(self._ws_args())
            if tool_name == "perseus_vault_recall" and "preview_cap" not in call_args:
                call_args["preview_cap"] = _PREVIEW_CAP
            res = self._client.call_tool(tool_name, call_args, timeout=20)
            if tool_name in ("perseus_vault_recall", "perseus_vault_recall_when",
                             "perseus_vault_semantic_search", "perseus_vault_context"):
                items = self._extract_items(res)
                return json.dumps({
                    "success": res.get("ok", False),
                    "count": len(items),
                    "items": [{
                        "category": it.get("category", ""),
                        "key": it.get("key", ""),
                        "text": self._item_text(it),
                    } for it in items],
                })
            return json.dumps({
                "success": res.get("ok", False),
                "result": res.get("text", "")[:800],
            })
        except Exception as e:
            logger.error("Tool call %s failed: %s", tool_name, e)
            return json.dumps({"success": False, "error": str(e)})

    # ------------------------------------------------------------------
    # Setup wizard integration
    # ------------------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "token",
                "description": "Perseus Vault MCP bearer token",
                "secret": True,
                "required": True,
                "env_var": "PERSEUS_VAULT_MCP_TOKEN",
                "url": "https://github.com/sowerkoku/Perseus-Continuum",
            },
            {
                "key": "url",
                "description": "Vault MCP endpoint URL",
                "required": False,
                "default": _DEFAULT_URL,
                "env_var": "PERSEUS_VAULT_URL",
            },
            {
                "key": "workspace_hash",
                "description": "Workspace scope hash (blank = global/unscoped)",
                "required": False,
                "default": "",
                "env_var": "PERSEUS_VAULT_WORKSPACE",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Persist non-secret config to $HERMES_HOME/perseus-vault.json.

        I2 contract (D3, guarantees D9-D11):
        - api_key is read-only legacy: never written here, never removed;
          an existing value survives any update byte-for-value.
        - Secrets (schema key 'token') are NEVER persisted by this
          method; the core mechanism alone writes them to .env.
        - A missing or empty value NEVER overwrites or deletes an
          existing entry. url: only an explicit non-empty value updates
          it. workspace_hash: updated only when the key is explicitly
          present in values ("" intentionally means global).
        - A failed write RAISES: the host (memory_setup / dashboard)
          reports the explicit error. No silent success, no alternative
          write path, and no secret in the message.
        """
        path = Path(hermes_home) / "perseus-vault.json"
        data: Dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text())
            except Exception as e:
                # corrupt existing file: refuse to rewrite over it
                raise RuntimeError(
                    "perseus-vault: existing perseus-vault.json is "
                    f"unreadable ({e.__class__.__name__}); refusing to "
                    "rewrite it") from e
            if not isinstance(loaded, dict):
                # valid JSON but wrong root type (list/null/scalar): same
                # contract as corrupt content - preserve it, never rewrite
                raise RuntimeError(
                    "perseus-vault: existing perseus-vault.json root is "
                    "not a JSON object; refusing to rewrite it")
            data = loaded
        v = values if isinstance(values, dict) else {}
        url = str(v.get("url") or "").strip()
        if url:
            data["url"] = url
        elif "url" not in data:
            data["url"] = _DEFAULT_URL
        if "workspace_hash" in v:
            data["workspace_hash"] = str(v.get("workspace_hash") or "")
        # 'token' and any secret keys from the panel are deliberately
        # ignored: never added, never replace api_key (single write
        # source for tokens is core -> .env).
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2))
            tmp.chmod(0o600)
            os.replace(tmp, path)
        except OSError as e:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(
                f"perseus-vault: config write failed ({e.__class__.__name__})"
                ) from e

    def backup_paths(self) -> List[str]:
        return [
            "~/.hermes/perseus-vault.json",
            "~/.hermes/plugins/perseus-vault/",
        ]

    def get_status_config(self, provider_config: Dict[str, Any]) -> Dict[str, Any]:
        """Redacted view for `hermes memory status`."""
        self._load_config()
        return {
            "url": self._config.get("url", _DEFAULT_URL),
            "workspace_hash": self._workspace_hash or "(global)",
            "token": "set" if self._config.get("api_key") else "MISSING",
        }


def create_provider() -> PerseusVaultProvider:
    """Factory function for plugin registration."""
    return PerseusVaultProvider()
'''

CLI_PY = r'''"""CLI commands for Perseus Vault memory provider.

Registers subcommands under `hermes perseus-vault <cmd>`.
Only available when perseus-vault is the active memory provider.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def _get_config() -> dict:
    """Load provider config: env (canonical + legacy) -> config.yaml
    memory.perseus-vault: -> perseus-vault.json."""
    config: dict = {}
    try:
        from hermes_cli.config import cfg_get, load_config
        section = cfg_get(load_config(), "memory", "perseus-vault")
        if isinstance(section, dict):
            config.update({k: v for k, v in section.items() if k != "token"})
    except Exception:
        pass
    try:
        jpath = get_hermes_home() / "perseus-vault.json"
        if jpath.exists():
            config.update(json.loads(jpath.read_text()))
    except Exception:
        pass
    if url := os.getenv("PERSEUS_VAULT_URL") or os.getenv("PERSEUS_MCP_URL"):
        config["url"] = url
    if token := (os.getenv("PERSEUS_VAULT_MCP_TOKEN")
                 or os.getenv("MCP_PERSEUS_VAULT_API_KEY")):
        config["api_key"] = token
    if ws := os.getenv("PERSEUS_VAULT_WORKSPACE"):
        config["workspace_hash"] = ws
    return config


class MCPClient:
    """Minimal MCP HTTP client (mirrors provider.py)."""

    PROTOCOL = "2025-06-18"
    CLIENT_INFO = {"name": "hermes-perseus-vault-cli", "version": "0.2.0"}

    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self.next_id = 1

    def _request(self, method: str, params: dict | None = None) -> dict:
        import urllib.error
        import urllib.request

        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        self.next_id += 1
        if params is not None:
            message["params"] = params
        data = json.dumps(message).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body[:400]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Connection error: {e.reason}")
        if not body.strip():
            return {}
        if body.lstrip().startswith(("event:", "data:")):
            lines = [l[5:].strip() for l in body.splitlines()
                     if l.startswith("data:")]
            body = lines[-1] if lines else body
        return json.loads(body)

    def initialize(self) -> dict:
        return self._request(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL,
                "capabilities": {},
                "clientInfo": self.CLIENT_INFO,
            },
        )

    def list_tools(self) -> list:
        result = self._request("tools/list")
        return result.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        raw = self._request("tools/call",
                            {"name": name, "arguments": arguments or {}})
        if "error" in raw:
            return {"ok": False, "text": json.dumps(raw["error"]), "data": None}
        result = raw.get("result", {}) or {}
        parts = [c.get("text", "") for c in result.get("content", [])
                 if c.get("type") == "text"]
        return {"ok": not result.get("isError", False),
                "text": "\n".join(p for p in parts if p),
                "data": result.get("structuredContent")}


def _get_client() -> MCPClient:
    config = _get_config()
    url = config.get("url", "http://localhost:8767/message")
    token = config.get("api_key")
    if not token:
        raise RuntimeError(
            "No token configured. Run `hermes memory setup` or set "
            "PERSEUS_VAULT_MCP_TOKEN.")
    client = MCPClient(url, token)
    init_result = client.initialize()
    if "error" in init_result:
        raise RuntimeError(f"MCP initialize failed: {init_result['error']}")
    return client


def cmd_status(args) -> int:
    try:
        client = _get_client()
        print("Provider: perseus-vault")
        print("Connected: True")
        print(f"  URL: {client.url}")
        tools = client.list_tools()
        print(f"  Tools available: {len(tools)}")
    except Exception as e:
        print("Provider: perseus-vault")
        print("Connected: False")
        print(f"  Error: {e}")
        return 1
    return 0


def cmd_tools(args) -> int:
    try:
        client = _get_client()
        tools = client.list_tools()
        if not tools:
            print("No tools available")
            return 1
        print(f"Available tools ({len(tools)}):")
        for t in tools:
            print(f"  - {t['name']}: {t.get('description', '')[:80]}")
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_call(args) -> int:
    if not args.tool:
        print("Usage: hermes perseus-vault call <tool_name> [json_args]")
        return 1
    try:
        client = _get_client()
        arguments = {}
        if args.json_args:
            try:
                arguments = json.loads(args.json_args)
            except json.JSONDecodeError as e:
                print(f"Invalid JSON: {e}")
                return 1
        result = client.call_tool(args.tool, arguments)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_recall(args) -> int:
    if not args.query:
        print("Usage: hermes perseus-vault recall <query> [--limit N]")
        return 1
    try:
        client = _get_client()
        result = client.call_tool(
            "perseus_vault_recall",
            {"query": args.query, "limit": args.limit or 10, "preview_cap": 400})
        payload = result.get("data")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(result.get("text", "") or "{}")
            except Exception:
                payload = {}
        inner = payload.get("result")
        if isinstance(inner, str):
            try:
                payload = json.loads(inner)
            except Exception:
                pass
        entities = payload.get("entities") or []
        print(f"Found {len(entities)} entities:")
        for e in entities:
            print(f"  - [{e.get('category')}/{e.get('key')}] "
                  f"{str(e.get('summary', e.get('content', '')))[:80]}")
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_semantic(args) -> int:
    if not args.query:
        print("Usage: hermes perseus-vault semantic <query> [--top_k N]")
        return 1
    try:
        client = _get_client()
        result = client.call_tool("perseus_vault_semantic_search",
                                  {"query": args.query, "limit": args.top_k or 10})
        payload = result.get("data")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(result.get("text", "") or "{}")
            except Exception:
                payload = {}
        inner = payload.get("result")
        if isinstance(inner, str):
            try:
                payload = json.loads(inner)
            except Exception:
                pass
        entities = payload.get("entities") or []
        print(f"Found {len(entities)} entities:")
        for e in entities:
            print(f"  - [{e.get('category')}/{e.get('key')}] "
                  f"{str(e.get('summary', e.get('content', '')))[:80]}")
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_remember(args) -> int:
    if not args.category or not args.content:
        print("Usage: hermes perseus-vault remember <category> <content> "
              "[--key KEY] [--type TYPE] [--tags TAGS]")
        return 1
    try:
        client = _get_client()
        config = _get_config()
        call_args = {
            "category": args.category,
            "key": args.key or f"cli-{abs(hash(args.content)) % 100000}",
            "body_json": json.dumps({"summary": args.content}),
            "type": args.type or "insight",
            "tags": args.tags.split(",") if args.tags else [],
        }
        if config.get("workspace_hash"):
            call_args["workspace_hash"] = config["workspace_hash"]
        result = client.call_tool("perseus_vault_remember", call_args)
        print(f"Remembered: {result.get('ok')}")
        print(result.get("text", "")[:400])
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_stats(args) -> int:
    try:
        client = _get_client()
        result = client.call_tool("perseus_vault_stats", {})
        payload = result.get("data")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(result.get("text", "") or "{}")
            except Exception:
                payload = {}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_config_show(args) -> int:
    config = _get_config()
    safe = {k: v for k, v in config.items() if k != "api_key"}
    print(json.dumps(safe, indent=2))
    return 0


def register_cli(parser) -> None:
    sub = parser.add_subparsers(dest="pv_cmd", required=True)

    sub.add_parser("status", help="Show provider status").set_defaults(func=cmd_status)
    sub.add_parser("tools", help="List available tools").set_defaults(func=cmd_tools)

    p_call = sub.add_parser("call", help="Call a tool directly")
    p_call.add_argument("tool", help="Tool name")
    p_call.add_argument("json_args", nargs="?", help="JSON arguments")
    p_call.set_defaults(func=cmd_call)

    p_recall = sub.add_parser("recall", help="FTS5 keyword recall")
    p_recall.add_argument("query", help="Search query")
    p_recall.add_argument("--limit", type=int, default=10)
    p_recall.set_defaults(func=cmd_recall)

    p_sem = sub.add_parser("semantic", help="Semantic vector search")
    p_sem.add_argument("query", help="Search query")
    p_sem.add_argument("--top_k", type=int, default=10)
    p_sem.set_defaults(func=cmd_semantic)

    p_rem = sub.add_parser("remember", help="Store an entity")
    p_rem.add_argument("category", help="Category")
    p_rem.add_argument("content", help="Content")
    p_rem.add_argument("--key", help="Stable key")
    p_rem.add_argument("--type", default="insight")
    p_rem.add_argument("--tags", help="Comma-separated tags")
    p_rem.set_defaults(func=cmd_remember)

    sub.add_parser("stats", help="Show Vault statistics").set_defaults(func=cmd_stats)
    sub.add_parser("config", help="Show config").set_defaults(func=cmd_config_show)

    def _dispatch(args):
        if hasattr(args, "func"):
            return args.func(args)
        parser.print_help()
        return 1

    parser.set_defaults(func=_dispatch)
'''


def write_plugin_files(plugin_dir: Path) -> None:
    """Write all plugin files."""
    files = {
        "plugin.yaml": PLUGIN_YAML,
        "__init__.py": INIT_PY,
        "provider.py": PROVIDER_PY,
        "cli.py": CLI_PY,
    }
    for name, content in files.items():
        (plugin_dir / name).write_text(content)
    log_ok("Plugin files created")


def merge_json_config(config_file: Path, url: str, workspace: str) -> dict:
    """Update perseus-vault.json WITHOUT discarding keys this installer
    does not own (I2/D3 contract):

    - api_key (legacy) is never written and never removed: an existing
      value is preserved byte-for-byte as a read-only legacy fallback.
    - unknown future keys are preserved for the same reason.
    - EXCEPTION to the preservation rule (policy A, review 2026-11-10):
      if the existing file is unparsable or its root is not a JSON
      object, the installer warns, keeps the original bytes in a
      0600 backup for manual recovery, and writes a fresh config.
      "unknown keys are never removed" does not hold for an invalid
      file; it is replaced deliberately, never silently.
    - url is always set (the installer owns it); workspace_hash is set
      only when a value was provided.
    - an existing file is backed up before being rewritten.

    Returns the merged dict (for logging/tests).
    """
    merged = {}
    if config_file.exists():
        raw = config_file.read_text()
        # the backup may hold legacy api_key: it must NEVER be less
        # protected than the original (review 2026-11-10). O_EXCL keeps
        # creation atomic at mode 600; a numeric suffix resolves same-
        # second collisions deterministically.
        base = "%s.bak-%s" % (config_file.name, _now_stamp())
        backup = config_file.with_name(base)
        for attempt in range(1, 100):
            try:
                fd = os.open(str(backup),
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                backup = config_file.with_name("%s-%d" % (base, attempt))
        else:
            raise RuntimeError("could not create a unique config backup")
        with os.fdopen(fd, "w") as bf:
            bf.write(raw)
        os.chmod(backup, 0o600)
        if oct(os.stat(backup).st_mode)[-3:] != "600":
            raise RuntimeError(
                "backup %s did not end at mode 600; refusing to rewrite "
                "config" % backup.name)
        log_info(f"Backed up existing config to {backup.name}")
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                merged = loaded
            else:
                log_warn("existing perseus-vault.json is not an object; "
                         "keeping backup, writing fresh config")
        except Exception as e:
            log_warn(f"existing perseus-vault.json unparsable ({e}); "
                     "keeping backup, writing fresh config")
    merged["url"] = url
    if workspace:
        merged["workspace_hash"] = workspace
    config_file.write_text(json.dumps(merged, indent=2))
    config_file.chmod(0o600)
    return merged


def write_env_token(hermes_home: Path, token: str) -> None:
    """Persist the token to $HERMES_HOME/.env (backup first, mode 600)."""
    env_file = hermes_home / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("PERSEUS_VAULT_MCP_TOKEN="):
                log_ok("PERSEUS_VAULT_MCP_TOKEN already set in .env (left as-is)")
                return
        backup = env_file.with_name(".env.bak-%s" % _now_stamp())
        shutil.copy2(env_file, backup)
        log_info(f"Backed up existing .env to {backup.name}")
        with open(env_file, "a") as f:
            f.write(f"\nPERSEUS_VAULT_MCP_TOKEN={token}\n")
    else:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(f"PERSEUS_VAULT_MCP_TOKEN={token}\n")
    try:
        os.chmod(env_file, 0o600)
    except OSError:
        pass
    log_ok("Token written to .env as PERSEUS_VAULT_MCP_TOKEN")


def _now_stamp() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def main() -> int:
    print("=" * 60)
    print("  Perseus Vault Memory Provider Installer for Hermes")
    print("=" * 60)
    print()

    # D1 contract gate (fail-closed): runs BEFORE anything touches the
    # host - no prompts, no host commands, no writes.
    try:
        _cv = check_host_contract()
        log_ok(f"Host contract gate OK (G1-G5): Hermes {_cv}")
    except HostContractError as e:
        log_error(f"Host contract gate: {e}")
        log_error("Aborting with NO changes made (fail closed).")
        return 1

    # Check Hermes
    log_info("Checking Hermes installation...")
    hermes_version = check_hermes()
    if not hermes_version:
        log_error("Hermes not found in PATH. Install Hermes first: "
                  "https://hermes-agent.nousresearch.com")
        return 1
    log_ok(f"Hermes found: {hermes_version}")

    # Line declaration (contract with users; see integrations/hermes/README.md)
    log_info(f"Plugin line: perseus-vault {PLUGIN_VERSION} — supported "
             f"Hermes >= {SUPPORTED_HERMES_MIN} (documented floor), "
             f"verified through {VERIFIED_HERMES_CEILING}. The 0.4.x line "
             f"is independent and NOT retro-compatible (v0.23.1 only).")
    declared = plugin_yaml_version()
    if declared != PLUGIN_VERSION:
        log_error(f"Internal inconsistency: embedded plugin.yaml declares "
                  f"{declared!r}, installer declares {PLUGIN_VERSION!r}. "
                  "Aborting without changes.")
        return 1

    hermes_home = get_hermes_home()
    log_info(f"Hermes home: {hermes_home}")

    # Environment (canonical names first, legacy aliases accepted)
    env_token = (os.environ.get("PERSEUS_VAULT_MCP_TOKEN", "").strip()
                 or os.environ.get("MCP_PERSEUS_VAULT_API_KEY", "").strip())
    env_url = os.environ.get("PERSEUS_VAULT_URL", "").strip()
    env_host_port = os.environ.get("MCP_HOST_PORT", "").strip()
    if not env_url and env_host_port:
        env_url = f"http://{env_host_port}/message"
    workspace = os.environ.get("PERSEUS_VAULT_WORKSPACE", "").strip()

    print()
    print("--- Perseus Vault MCP Server Configuration ---")

    if not sys.stdin.isatty():
        # Non-interactive: token REQUIRED. Never install with a placeholder.
        if not env_token:
            log_error(
                "Non-interactive mode requires PERSEUS_VAULT_MCP_TOKEN "
                "(legacy alias MCP_PERSEUS_VAULT_API_KEY accepted). "
                "Refusing to install with a placeholder token.")
            return 1
        mcp_url = env_url or f"http://localhost:8767/message"
        connection_type = "local" if mcp_url.startswith(
            ("http://localhost", "http://127.0.0.1")) else "remote"
        log_info(f"Non-interactive mode: url={mcp_url}")
        log_info(f"Workspace hash: {workspace or '(global)'}")
    else:
        print("Enter the IP:port of the machine running the Perseus Vault "
              "MCP server")
        print("Example: localhost:8767 or 192.168.1.54:8767")
        print()
        location = prompt_with_default(
            "Is the server local or remote? (local/remote)", "local").lower()
        if location in ("local", "l", "localhost"):
            default_host_port = env_host_port or "localhost:8767"
            mcp_host_port = prompt_with_default("MCP server IP:port",
                                                default_host_port)
            connection_type = "local"
        else:
            default_host_port = env_host_port or "192.168.1.54:8767"
            mcp_host_port = prompt_with_default("MCP server IP:port",
                                                default_host_port)
            connection_type = "remote"
        mcp_url = f"http://{mcp_host_port}/message"
        log_info(f"MCP URL: {mcp_url}")
        if env_token:
            log_info("Token taken from environment (PERSEUS_VAULT_MCP_TOKEN "
                     "or MCP_PERSEUS_VAULT_API_KEY)")
        else:
            mcp_token = prompt_required("MCP bearer token")
            env_token = mcp_token

    # Plugin directory
    plugin_dir = hermes_home / "plugins" / "perseus-vault"
    if plugin_dir.exists() and any(plugin_dir.iterdir()):
        log_warn(f"Existing plugin found at {plugin_dir}; overwriting files")
    plugin_dir.mkdir(parents=True, exist_ok=True)

    write_plugin_files(plugin_dir)

    log_info("Enabling plugin...")
    result = run_cmd(["hermes", "plugins", "enable", "perseus-vault"],
                     capture=False)
    if result.returncode == 0:
        log_ok("Plugin enabled")
    else:
        log_warn("`hermes plugins enable` returned non-zero "
                 "(may already be enabled)")

    write_env_token(hermes_home, env_token)

    config_file = hermes_home / "perseus-vault.json"
    merged = merge_json_config(config_file, mcp_url, workspace)
    if "api_key" in merged:
        log_info("legacy api_key in perseus-vault.json left untouched "
                 "(read-only; effective token comes from .env when set)")
    log_ok(f"Config saved to {config_file} (mode 600)")

    log_info("Setting memory.provider...")
    result = run_cmd(["hermes", "config", "set", "memory.provider",
                      "perseus-vault"], capture=False)
    if result.returncode == 0:
        log_ok("memory.provider = perseus-vault")
    else:
        log_warn("`hermes config set memory.provider` returned non-zero; "
                 "set it manually: hermes config set memory.provider "
                 "perseus-vault")

    log_info("Verifying installation...")
    result = run_cmd(["hermes", "memory", "status"])
    stdout = result.stdout or ""
    if "perseus-vault" in stdout and "available" in stdout.lower():
        log_ok("Installation verified (provider available)")
    else:
        log_warn("`hermes memory status` did not confirm availability; "
                 "the provider may need a gateway/session restart to pick "
                 "up the new .env token")

    print()
    print("=" * 60)
    print("  Perseus Vault Memory Provider Ready")
    print("=" * 60)
    print()
    print("Commands available:")
    print("  hermes perseus-vault status       # Check connection")
    print("  hermes perseus-vault tools        # List MCP tools")
    print("  hermes perseus-vault recall <q>   # Keyword search")
    print("  hermes perseus-vault semantic <q> # Vector search")
    print("  hermes perseus-vault remember     # Store memory")
    print("  hermes perseus-vault stats        # Vault stats")
    print("  hermes perseus-vault config       # Show config")
    print()
    print("Memory is injected automatically each turn via prefetch().")
    print(f"Connection: {connection_type}")
    print()
    print("Next steps:")
    print("  1. Restart Hermes (or the gateway) so the new .env token is "
          "picked up.")
    print("  2. Optional: `hermes memory setup` to reconfigure interactively.")
    print("  3. Optional: disable built-in memory for a vault-only setup "
          "(memory.memory_enabled / user_profile_enabled in config.yaml); "
          "hybrid mode is supported — built-in writes mirror to the Vault.")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
