#!/usr/bin/env python3
"""Suite D — I3 session-switch contract (design rev.4, authorized 2026-11-10).

Contract under test (host-verified against installed v0.23.1:
agent/memory_provider.py:200, agent/memory_manager.py:663-703):
  on_session_switch(new_session_id, *, parent_session_id="", reset=False,
                    rewound=False, **kwargs)
  - /undo  (same id, rewound=True): clear _turn_buffer only.
  - reset=True: clear _turn_buffer, rebind session id.
  - plain switch (/branch, /resume): rebind session id; buffered turns
    are PRESERVED with their original association (the host never
    re-passes gateway_session_key: chat identity is unchanged); entries
    are never inherited by, or reassigned to, another record.
  - ownership validation: mismatched parent_session_id -> explicit
    warning, rebind still applied (host is authoritative), zero
    re-association of entries.
  - NO Vault effect in any variant: zero calls, zero writes;
    checkpoints/receipts/entities untouched; never capture, distill,
    mark, or emit receipts.

Fail-first: D0-D3 are RED against the current provider (it inherits the
base no-op: no clear on undo/reset, no rebind). D4-D6 are invariant
guards that pass now and MUST keep passing.

Standalone: python3 integrations/hermes/tests/test_contract_i3.py
"""
import inspect
import json
import logging
import sys

import test_contract_i1 as c  # harness: loaders, StubMCP, make_ready, base

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}   {name}")
    if detail and not ok:
        print(f"        -> {detail}")


def store_bytes(vault):
    return json.dumps(sorted(((k, json.dumps(v, sort_keys=True))
                              for (k, v) in vault.store.items())))


def seeded_vault():
    v = c.base.FakeVault()
    v.call_tool("perseus_vault_remember", {
        "category": "checkpoint", "key": "ck-pre",
        "body_json": json.dumps({"kind": "hermes-pre-compress-checkpoint"}),
        "status": "draft", "type": "reference"})
    v.call_tool("perseus_vault_remember", {
        "category": "hermes-capture-receipt", "key": "rc-pre",
        "body_json": json.dumps({"content_sha256": "deadbeef"})})
    v.calls.clear()
    return v


def buffer_turns(p, n=3):
    for i in range(n):
        p.sync_turn(f"u{i}: content", f"a{i}: reply", session_id="s")


# ----------------------------------------------------------------------
# D0 — method is defined by the provider, not inherited from the base
# ----------------------------------------------------------------------

def d0(mod):
    cls = mod.PerseusVaultProvider
    if "on_session_switch" not in vars(cls):
        return False, ("provider inherits the base no-op: no switch "
                       "handling exists (the defect under repair)")
    sig = inspect.signature(cls.on_session_switch)
    need = {"new_session_id", "parent_session_id", "reset", "rewound"}
    missing = need - set(sig.parameters)
    if missing:
        return False, f"signature missing {missing}"
    return True, ""


# ----------------------------------------------------------------------
# D1 — /undo (rewound=True): buffer cleared, everything else intact
# ----------------------------------------------------------------------

def d1(mod):
    p = c.make_ready(mod, session_id="sess-undo", hermes_home="/tmp/d1",
                     gsk="gsk-undo", client=c.base.FakeVault())
    buffer_turns(p)
    vault = p._client
    if len(p._turn_buffer) == 0:
        return False, "precondition: buffer empty, scenario vacuous"
    pre = store_bytes(vault)
    p.on_session_switch("sess-undo", parent_session_id="", reset=False,
                        rewound=True)
    problems = []
    if p._turn_buffer:
        problems.append("buffer NOT cleared on rewound=True (stale undone "
                        "content would contaminate the next capture)")
    if p._session_id != "sess-undo":
        problems.append("session id changed on /undo (same id per host)")
    if vault.calls:
        problems.append(f"vault calls during switch: {vault.calls}")
    if store_bytes(vault) != pre:
        problems.append("vault store mutated")
    return (not problems), "; ".join(problems)


# ----------------------------------------------------------------------
# D2 — reset=True: buffer cleared, session id rebound, zero vault effect
# ----------------------------------------------------------------------

def d2(mod):
    p = c.make_ready(mod, session_id="sess-old", hermes_home="/tmp/d2",
                     gsk="gsk-old", client=seeded_vault())
    buffer_turns(p)
    if not p._turn_buffer:
        return False, "precondition: buffer empty"
    vault = p._client
    pre = store_bytes(vault)
    p.on_session_switch("sess-new", parent_session_id="sess-old",
                        reset=True, reason="new_session")
    problems = []
    if p._turn_buffer:
        problems.append("buffer NOT cleared on reset=True")
    if p._session_id != "sess-new":
        problems.append(f"session id not rebound (still {p._session_id!r})")
    if p._session_label != "gsk-old":
        problems.append("chat identity (gsk) mutated on switch — host does "
                        "not re-pass it")
    if vault.calls or store_bytes(vault) != pre:
        problems.append("vault mutated on reset")
    return (not problems), "; ".join(problems)


# ----------------------------------------------------------------------
# D3 — plain switch (/branch, /resume): rebind, preserve, never re-tag
# ----------------------------------------------------------------------

def d3(mod):
    p = c.make_ready(mod, session_id="sess-parent", hermes_home="/tmp/d3",
                     gsk="gsk-chat", client=seeded_vault())
    buffer_turns(p)
    original = list(p._turn_buffer)
    vault = p._client
    pre = store_bytes(vault)
    # owner-match path
    p.on_session_switch("sess-branch", parent_session_id="sess-parent")
    problems = []
    if p._session_id != "sess-branch":
        problems.append(f"session id not rebound on plain switch "
                        f"(still {p._session_id!r}): later writes would "
                        "land in the wrong record")
    if p._turn_buffer != original or len(p._turn_buffer) == 0:
        problems.append("buffered turns damaged on plain switch (must "
                        "preserve per /resume rule)")
    if any(t is not o for t, o in zip(p._turn_buffer, original)):
        problems.append("entries re-created/re-tagged (must not reassign)")
    if p._session_label != "gsk-chat":
        problems.append("gsk changed (host never re-passes it)")
    if vault.calls or store_bytes(vault) != pre:
        problems.append("vault mutated on switch")
    # mismatched-owner path: warning + zero re-association
    buf_before = list(p._turn_buffer)
    with _captured_logs(mod) as cap:
        p.on_session_switch("sess-x", parent_session_id="not-mine")
    if p._session_id != "sess-x":
        problems.append("rebind skipped on parent mismatch (host is "
                        "authoritative)")
    if p._turn_buffer != buf_before:
        problems.append("entries touched on parent mismatch")
    if not any("parent" in m.lower() for m in cap):
        problems.append("no explicit warning on parent mismatch")
    return (not problems), "; ".join(problems)


class _captured_logs:
    def __init__(self, mod):
        self.mod = mod
        self.msgs = []

    def __enter__(self):
        rec = self
        class H(logging.Handler):
            def emit(self, r):
                rec.msgs.append(r.getMessage())
        self.h = H()
        self.mod.logger.addHandler(self.h)
        return self.msgs

    def __exit__(self, *a):
        self.mod.logger.removeHandler(self.h)


# ----------------------------------------------------------------------
# D4 — zero-effect guard across every switch variant
# ----------------------------------------------------------------------

def d4(mod):
    if "on_session_switch" not in vars(mod.PerseusVaultProvider):
        return True, ("trivial guard (no method = no effects); must stay "
                      "green after implementing")
    variants = [
        dict(parent_session_id="s1", rewound=True),
        dict(parent_session_id="s1", reset=True),
        dict(parent_session_id="s1"),
        dict(),
    ]
    p = c.make_ready(mod, session_id="s1", hermes_home="/tmp/d4",
                     gsk="gsk-4", client=seeded_vault())
    buffer_turns(p)
    vault = p._client
    pre = store_bytes(vault)
    for kw in variants:
        p.on_session_switch("s2" if not kw.get("rewound") else "s1", **kw)
    problems = []
    if vault.calls:
        problems.append(f"calls: {vault.calls}")
    if store_bytes(vault) != pre:
        problems.append("store mutated across switch variants")
    return (not problems), "; ".join(problems)


# ----------------------------------------------------------------------
# D5 — two profiles switching concurrently: no cross-state
# ----------------------------------------------------------------------

def d5(mod):
    p1 = c.make_ready(mod, session_id="p1-old", hermes_home="/tmp/p1",
                      gsk="gsk-p1", client=c.base.FakeVault())
    p2 = c.make_ready(mod, session_id="p2-old", hermes_home="/tmp/p2",
                      gsk="gsk-p2", client=c.base.FakeVault())
    buffer_turns(p1)
    buffer_turns(p2)
    # interleaved switches: p1 undo, p2 branch, p1 new-session reset
    p1.on_session_switch("p1-old", parent_session_id="p1-old", rewound=True)
    p2.on_session_switch("p2-branch", parent_session_id="p2-old")
    p1.on_session_switch("p1-new", parent_session_id="p1-old", reset=True)
    problems = []
    if p1._turn_buffer:
        problems.append("p1 buffers not cleared by its own undo+reset")
    if p2._session_id != "p2-branch":
        problems.append("p2 not rebound (cross-state or no handling)")
    if not p2._turn_buffer:
        problems.append("p2 buffer damaged by p1's switches (cross-state)")
    if p1._client.calls or p2._client.calls:
        problems.append("a profile made vault calls during switches")
    return (not problems), "; ".join(problems)


# ----------------------------------------------------------------------
# D6 — resumed session preserves buffers (invariant guard)
# ----------------------------------------------------------------------

def d6(mod):
    orig = mod.MCPClient
    mod.MCPClient = c.StubMCP
    try:
        p = mod.PerseusVaultProvider()
        p._load_config = lambda: p._config.update(
            {"api_key": "tok", "url": "http://fake", "workspace_hash": ""})
        p.initialize("sess-resumed", hermes_home="/tmp/d6",
                     gateway_session_key="gsk-r", resumed=True)
        assert p._enabled
    finally:
        mod.MCPClient = orig
    buffer_turns(p, 2)
    if len(p._turn_buffer) != 2:
        return False, "buffer not retained after resumed initialize"
    return True, ""


def main():
    src = c.INSTALLER.read_text()
    mod = c.load_provider(src, "i3")
    with _captured_logs_quiet():
        for name, fn in (("D0 method defined by provider", d0),
                         ("D1 /undo clears buffer only", d1),
                         ("D2 reset clears buffer + rebind", d2),
                         ("D3 plain switch rebind+preserve", d3),
                         ("D4 zero vault effect (all variants)", d4),
                         ("D5 two profiles concurrent switches", d5),
                         ("D6 resumed preserves buffers", d6)):
            try:
                ok, det = fn(mod)
            except Exception as e:
                ok, det = False, f"unexpected: {type(e).__name__}: {e}"
            check(name, ok, det)
    n = sum(1 for _, ok in results if ok)
    print(f"\n{n}/{len(results)} PASS")
    return 0 if n == len(results) else 1


class _captured_logs_quiet:
    """Silence provider logging during the suite (not an assertion)."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


if __name__ == "__main__":
    sys.exit(main())
