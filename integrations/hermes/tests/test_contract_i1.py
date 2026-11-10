"""Suite C — I1 contract tests (Hermes v0.23.1 threading + hermes_home binding).

Standalone runner (no pytest needed):  python3 test_contract_i1.py

Contracts under test (design-I1-I2-I3.md, phase I1, authorized 2026-11-10):
  C1  queue_prefetch goes through agent.memory_provider.spawn_context_thread
      (never a bare thread; no fallback branch exists).
  C3  contextvars of the spawning profile reach the worker (host ctx-bound
      helper semantics) — a bare thread does not carry them.
  C4  two providers bound to different hermes_home NEVER cross-read each
      other's perseus-vault.json, even with an ambiguous ambient HERMES_HOME.
  C5  config resolution (env precedence / json / defaults / empty workspace)
      is IDENTICAL between the a9a2c2b baseline and the corrected provider
      for every not-modified path (golden parity).
  C6  static: no bare `threading.Thread(` use remains; the host helper is
      used and started.
  C7  semantic equivalence of non-modified routes vs the a9a2c2b baseline:
      identical call traces (presence AND absence of every write), identical
      arguments, order and session association, identical receipts/checkpoints/
      marking behavior, identical error behavior. Counting writes is NOT
      sufficient.
  C0  meta (fail-first): C1/C3/C4 MUST detect the gap against the OLD
      (a9a2c2b) provider — a test that passes against uncorrected code
      proves nothing.

Reuses the stateful fakes from test_checkpoint_api_v2.py (FakeVault /
FakeClient) to avoid behavioral drift between suites.
"""
import ast
import contextvars
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent  # repo root
INSTALLER = HERE.parent / "install-perseus-vault.py"
OLD_REF = "a9a2c2b"  # immutable approved baseline (provider 9be2dd2a)

sys.path.insert(0, str(HERE))
import test_checkpoint_api_v2 as base  # noqa: E402  (guarded by __main__)


# --------------------------------------------------------------------------
# module loaders
# --------------------------------------------------------------------------

def extract_provider_src(installer_src: str) -> str:
    code = None
    for node in ast.walk(ast.parse(installer_src)):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "PROVIDER_PY":
                    code = ast.literal_eval(node.value)
    assert code, "PROVIDER_PY not found"
    return code


def load_provider(installer_src: str, tag: str):
    """Load the embedded provider with a fresh, host-faithful stub.

    The stub's spawn_context_thread reproduces the REAL host semantics
    (agent/memory_provider.py:29): an UNSTARTED thread running the target
    under a copy of the caller's contextvars. The provider is expected to
    call .start() itself.
    """
    stub = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    handle = {"spawn_calls": [], "threads": []}

    def spawn_context_thread(target, *, name, daemon=True, args=(), kwargs=None):
        handle["spawn_calls"].append(name)
        t = threading.Thread(
            target=contextvars.copy_context().run,
            args=(target,) + tuple(args), kwargs=kwargs or {},
            name=name, daemon=daemon)
        handle["threads"].append(t)
        return t

    stub.MemoryProvider = MemoryProvider
    stub.spawn_context_thread = spawn_context_thread
    agent_pkg = types.ModuleType("agent")
    agent_pkg.memory_provider = stub
    sys.modules["agent"] = agent_pkg  # NOT setdefault: fresh stub per load
    sys.modules["agent.memory_provider"] = stub

    mod = types.ModuleType(f"pv_under_test_{tag}")
    code = extract_provider_src(installer_src)
    exec(compile(code, f"{tag}:PROVIDER_PY", "exec"), mod.__dict__)
    mod._pv_handle = handle
    return mod


def new_module():
    return load_provider(INSTALLER.read_text(), "new")


def old_module():
    out = subprocess.run(
        ["git", "show", f"{OLD_REF}:integrations/hermes/install-perseus-vault.py"],
        cwd=REPO, capture_output=True, text=True)
    assert out.returncode == 0, f"git show {OLD_REF} failed: {out.stderr[:200]}"
    return load_provider(out.stdout, "old")


class StubMCP:
    def __init__(self, *a, **k):
        pass

    def initialize(self):
        return {}

    def list_tools(self):
        return []


def make_ready(mod, *, session_id, hermes_home, gsk, client=None):
    """Initialize through the REAL initialize() path with a contract-compliant
    kwarg set (hermes_home included), then swap in a fake client."""
    orig_mcp = mod.MCPClient
    mod.MCPClient = StubMCP
    try:
        p = mod.PerseusVaultProvider()
        p._load_config = lambda: p._config.update(
            {"api_key": "tok", "url": "http://fake", "workspace_hash": ""})
        p.initialize(session_id, hermes_home=str(hermes_home),
                     gateway_session_key=gsk)
        assert p._enabled, "provider must activate with a token configured"
        if client is not None:
            p._client = client
        return p
    finally:
        mod.MCPClient = orig_mcp


def join_workers(mod, timeout=5.0):
    for t in mod._pv_handle["threads"]:
        t.join(timeout)
    mod._pv_handle["threads"].clear()


# --------------------------------------------------------------------------
# C1 — background job must go through the host helper (C0: old fails)
# --------------------------------------------------------------------------

def c1(mod):
    mod._pv_handle["spawn_calls"].clear()
    client = base.FakeClient(responses={
        "perseus_vault_recall_when": [{"ok": True, "text": '{"items": []}',
                                       "data": {"items": []}}],
        "perseus_vault_recall": [{"ok": True, "text": '{"items": []}',
                                  "data": {"items": []}}]})
    p = make_ready(mod, session_id="s-c1", hermes_home="/tmp/c1",
                   gsk="gw-c1", client=client)
    p.queue_prefetch("calentamiento")
    join_workers(mod)
    if mod._pv_handle["spawn_calls"] != ["perseus-vault-prefetch"]:
        return False, (f"spawn_context_thread calls = "
                      f"{mod._pv_handle['spawn_calls']} (expected exactly "
                      f"['perseus-vault-prefetch'])")
    if not client.calls:
        return False, "worker did not perform the recall (helper thread dead?)"
    return True, ""


# --------------------------------------------------------------------------
# C3 — profile contextvars must reach the worker (C0: old loses them)
# --------------------------------------------------------------------------
_SCOPE = contextvars.ContextVar("pv_contract_scope", default=None)


def c3(mod):
    seen = []

    class ScopeRecorder:
        def call_tool(self, name, arguments=None, timeout=30.0):
            seen.append(_SCOPE.get())
            return {"ok": True, "text": '{"items": []}', "data": {"items": []}}

    p = make_ready(mod, session_id="s-c3", hermes_home="/tmp/c3",
                   gsk="gw-c3", client=ScopeRecorder())
    tok = _SCOPE.set("profile-scope-42")  # emulate the owning profile scope
    try:
        p.queue_prefetch("contexto")
        join_workers(mod)
    finally:
        _SCOPE.reset(tok)
    if not seen:
        return False, "worker never ran (no calls recorded)"
    if any(v != "profile-scope-42" for v in seen):
        return False, f"worker saw scope={seen!r} (context did not travel)"
    return True, ""


# --------------------------------------------------------------------------
# C4 — two profiles, two homes: never cross-read (C0: old cross-reads)
# --------------------------------------------------------------------------

def c4(mod):
    saved = {k: os.environ.get(k) for k in
             ("PERSEUS_VAULT_URL", "PERSEUS_VAULT_MCP_TOKEN",
              "PERSEUS_MCP_URL", "MCP_PERSEUS_VAULT_API_KEY",
              "PERSEUS_VAULT_WORKSPACE", "MCP_HOST_PORT", "HERMES_HOME")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        homeA = Path(tempfile.mkdtemp(prefix="pv-C4-A"))
        homeB = Path(tempfile.mkdtemp(prefix="pv-C4-B"))
        (homeA / "perseus-vault.json").write_text(json.dumps(
            {"url": "http://vault-A/message", "api_key": "tok-A"}))
        (homeB / "perseus-vault.json").write_text(json.dumps(
            {"url": "http://vault-B/message", "api_key": "tok-B"}))
        # ambiguous ambient home: the WRONG one for instance B
        os.environ["HERMES_HOME"] = str(homeA)

        orig_mcp = mod.MCPClient
        mod.MCPClient = StubMCP
        try:
            pa = mod.PerseusVaultProvider()
            pa.initialize("s-A", hermes_home=str(homeA), gateway_session_key="gw-A")
            pb = mod.PerseusVaultProvider()
            pb.initialize("s-B", hermes_home=str(homeB), gateway_session_key="gw-B")
        finally:
            mod.MCPClient = orig_mcp

        problems = []
        if pa._config.get("url") != "http://vault-A/message":
            problems.append(f"profile A read {pa._config.get('url')!r}")
        if pb._config.get("url") != "http://vault-B/message":
            problems.append(f"profile B read {pb._config.get('url')!r}")
        if problems:
            return False, "; ".join(problems)
        return True, ""
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --------------------------------------------------------------------------
# C5 — golden parity of config resolution on non-modified paths
# --------------------------------------------------------------------------

def _resolve(mod, env, json_home):
    saved = {k: os.environ.get(k) for k in
             ("PERSEUS_VAULT_URL", "PERSEUS_VAULT_MCP_TOKEN",
              "PERSEUS_MCP_URL", "MCP_PERSEUS_VAULT_API_KEY",
              "PERSEUS_VAULT_WORKSPACE", "MCP_HOST_PORT", "HERMES_HOME")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        for k, v in env.items():
            os.environ[k] = v
        if json_home is not None:
            os.environ["HERMES_HOME"] = str(json_home)
        p = mod.PerseusVaultProvider()
        p._load_config()
        c = p._config
        return (c.get("url"), c.get("api_key"), c.get("workspace_hash"))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def c5(new_mod, old_mod):
    jh = Path(tempfile.mkdtemp(prefix="pv-C5"))
    (jh / "perseus-vault.json").write_text(json.dumps(
        {"url": "http://vault-json/message", "api_key": "tok-json"}))
    cases = [
        ("env precedence beats json", {
            "PERSEUS_VAULT_URL": "http://vault-env/message",
            "PERSEUS_VAULT_MCP_TOKEN": "tok-env"}, jh),
        ("json only", {}, jh),
        ("legacy token alias", {
            "MCP_PERSEUS_VAULT_API_KEY": "tok-legacy"}, None),
        ("defaults (no env, no json)", {}, None),
        ("workspace passthrough", {
            "PERSEUS_VAULT_WORKSPACE": "ws-hash-1"}, jh),
        ("empty workspace stays global", {
            "PERSEUS_VAULT_WORKSPACE": ""}, jh),
    ]
    problems = []
    for name, env, home in cases:
        r_new = _resolve(new_mod, env, home)
        r_old = _resolve(old_mod, env, home)
        if r_new != r_old:
            problems.append(f"{name}: new={r_new} old={r_old}")
    # explicit sanity of the golden expectations themselves
    g = _resolve(new_mod, {}, None)
    if g[0] != "http://localhost:8767/message" or g[1] != "" or g[2] != "":
        problems.append(f"defaults case wrong: {g}")
    g2 = _resolve(new_mod, {"PERSEUS_VAULT_URL": "http://vault-env/message",
                            "PERSEUS_VAULT_MCP_TOKEN": "tok-env"}, jh)
    if g2 != ("http://vault-env/message", "tok-env", ""):
        problems.append(f"env-precedence case wrong: {g2}")
    if problems:
        return False, " | ".join(problems)
    return True, ""


# --------------------------------------------------------------------------
# C6 — static contract of the source itself
# --------------------------------------------------------------------------

def c6():
    new_src = extract_provider_src(INSTALLER.read_text())
    problems = []
    if "threading.Thread(" in new_src:
        problems.append("bare threading.Thread( remains in provider source")
    if "spawn_context_thread(" not in new_src:
        problems.append("host helper spawn_context_thread not used")
    if 'spawn_context_thread(_work, name="perseus-vault-prefetch").start()' \
            not in new_src:
        problems.append("helper thread is not started (host helper returns "
                        "an UNSTARTED thread)")
    if 'kwargs["hermes_home"]' not in new_src:
        problems.append("hermes_home kwarg not bound in initialize()")
    if problems:
        return False, "; ".join(problems)
    return True, ""


# --------------------------------------------------------------------------
# C7 — semantic equivalence vs the a9a2c2b baseline (non-modified routes)
# --------------------------------------------------------------------------

class StageFail:
    """Stage-aware failure injector over FakeVault.

    Intercepts exactly one kind of call and returns a failure or
    quarantine-shaped response WITHOUT mutating vault state (real server:
    a rejected write leaves no entity; a qrn write is never served).
    Intercepted calls are recorded in the delegate's trace because the
    provider did make them.
    Stages: ckpt-write | recall-ckpt | recall-receipts | capture |
            receipt-write | marking.   Shapes: error | qrn.
    """

    def __init__(self, delegate, stage, shape):
        self.d = delegate
        self.stage = stage
        self.shape = shape
        self._fired = False

    def _resp(self):
        if self.shape == "qrn":
            payload = {"ok": True, "action": "quarantined",
                       "id": "qrn-fixed0000000"}
            return {"ok": True, "text": json.dumps(payload), "data": payload}
        return {"ok": False, "text": "stage-rejected", "data": None}

    def _matches(self, name, a):
        if self._fired:
            return False
        if self.stage == "ckpt-write":
            return (name == "perseus_vault_remember"
                    and a.get("category") == "checkpoint"
                    and a.get("status") == "draft")
        if self.stage == "marking":
            return (name == "perseus_vault_remember"
                    and a.get("category") == "checkpoint"
                    and a.get("status") == "deprecated")
        if self.stage == "receipt-write":
            return (name == "perseus_vault_remember"
                    and a.get("category") == "hermes-capture-receipt")
        if self.stage == "capture":
            return name == "perseus_vault_capture"
        if self.stage == "recall-ckpt":
            return (name == "perseus_vault_recall"
                    and a.get("category") == "checkpoint")
        if self.stage == "recall-receipts":
            return (name == "perseus_vault_recall"
                    and a.get("category") == "hermes-capture-receipt")
        return False

    @property
    def calls(self):
        return self.d.calls

    def call_tool(self, name, arguments=None, timeout=30.0):
        a = dict(arguments or {})
        if self._matches(name, a):
            self._fired = True
            self.d.calls.append((name, a))
            return self._resp()
        return self.d.call_tool(name, a, timeout)


def _final_state(vault):
    ck = {}
    for (cat, key), v in vault.store.items():
        if cat == "checkpoint":
            ck[key] = v["status"]
    receipts = sorted(k for (cat, k) in vault.store
                      if cat == "hermes-capture-receipt")
    return {"checkpoints": ck, "receipts": receipts,
            "captures": len(vault.captured)}


def _scenario(mod, case):
    """case: dict(stage=None|str, shape=None). Returns trace, results and
    final observable state (checkpoints consumed/draft, receipts present)."""
    if case["stage"] is None:
        client = base.FakeClient(default_ok=False)  # generic-failure extra
        vault = None
    else:
        vault = base.FakeVault()
        client = StageFail(vault, case["stage"], case["shape"])
    p = make_ready(mod, session_id="sess-parity", hermes_home="/tmp/parity",
                   gsk="gw-parity", client=client)
    p.sync_turn("u1: confirmar respaldo nightly", "a1: respaldo ok",
                session_id="sess-parity")
    p.sync_turn("u2: horario", "a2: 02:00", session_id="sess-parity")
    raised = None
    try:
        res = p.on_pre_compress([
            {"role": "user", "content": "confirmar respaldo incremental nightly"},
            {"role": "assistant", "content": "RESPALDO CONFIRMADO: nightly 02:00"}])
    except Exception as e:
        res, raised = None, type(e).__name__
    try:
        p.on_session_end([])
    except Exception as e:
        raised = raised or type(e).__name__
    trace = [(n, json.dumps(a, sort_keys=True, ensure_ascii=False))
             for (n, a) in client.calls]
    state = _final_state(vault) if vault is not None else None
    return trace, (res, raised), state


# Failure matrix required by review 2026-11-10: independent failure at each
# stage of the persistence cycle, plus observable final state. The generic
# all-fail client is kept as an additional case.
C7_MATRIX = [
    {"stage": "ckpt-write", "shape": "error"},
    {"stage": "recall-ckpt", "shape": "error"},
    {"stage": "capture", "shape": "error"},
    {"stage": "receipt-write", "shape": "error"},
    {"stage": "receipt-write", "shape": "qrn"},
    {"stage": "marking", "shape": "error"},
    {"stage": "marking", "shape": "qrn"},
    {"stage": "recall-receipts", "shape": "error"},
    {"stage": None, "shape": None},  # generic-failure (additional)
]


def c7(new_mod, old_mod):
    problems = []
    detected_something = False  # anti-false-positive sentinel
    for case in C7_MATRIX:
        t_new, r_new, s_new = _scenario(new_mod, case)
        t_old, r_old, s_old = _scenario(old_mod, case)
        label = f"{case['stage']}/{case['shape']}"
        if t_new != t_old:
            detail = []
            for i in range(max(len(t_new), len(t_old))):
                a = t_new[i] if i < len(t_new) else None
                b = t_old[i] if i < len(t_old) else None
                if a != b:
                    detail.append(f"pos {i}: new={a} old={b}")
                    if len(detail) >= 3:
                        break
            problems.append(f"trace mismatch [{label}]: " + "; ".join(detail))
        if r_new != r_old:
            problems.append(f"result/behavior mismatch [{label}]: "
                            f"new={r_new} old={r_old}")
        if s_new is not None and s_new != s_old:
            problems.append(f"final-state mismatch [{label}]: "
                            f"new={s_new} old={s_old}")
        # the scenario must have actually exercised something (no empty
        # vacuous comparisons): at least a checkpoint write call happened
        if not t_new:
            problems.append(f"[{label}] empty trace (scenario did not run)")
        # non-vacuity: the comparison must have actually exercised real
        # behavior (a real store populated, or a raise somewhere)
        if r_new[1]:
            detected_something = True
        if s_new and (s_new["checkpoints"] or s_new["receipts"]
                      or s_new["captures"]):
            detected_something = True
    if problems:
        return False, " | ".join(problems)[:900]
    if not detected_something:
        return False, "matrix never observed a failing/partial outcome " \
                      "(sentinel: comparisons may be vacuous)"
    return True, ""


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------

def check(name, fn, *a):
    try:
        ok, detail = fn(*a)
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"unexpected exception: {type(e).__name__}: {e}"
    return (name, "PASS" if ok else "FAIL", detail)


def main() -> int:
    results = []
    new_mod = new_module()
    old_mod = old_module()

    # C0: the gap-detection meta-test — C1/C3/C4 MUST fail on the old module
    for name, fn, args in (("C1", c1, (old_module(),)),
                          ("C3", c3, (old_module(),)),
                          ("C4", c4, (old_module(),))):
        r = check(f"{name}-on-old baseline", fn, *args)
        gap_detected = r[1] == "FAIL"
        results.append((
            f"C0 {name} detects the gap on {OLD_REF}",
            "PASS" if gap_detected else "FAIL",
            ("gap correctly detected: " + (r[2] or ""))[:300]
            if gap_detected else "expected FAIL on old code, got PASS"))

    results.append(check("C1 host helper is the only thread path", c1, new_mod))
    results.append(check("C3 profile context reaches the worker", c3, new_mod))
    results.append(check("C4 two profiles no cross-read", c4, new_mod))
    results.append(check("C5 config-resolution golden parity", c5, new_mod, old_mod))
    results.append(check("C6 static source contract", c6))
    results.append(check("C7 semantic equivalence vs baseline", c7, new_mod, old_mod))

    failed = 0
    for name, status, detail in results:
        print(f"{status:5s}  {name}" + (f"\n        -> {detail}" if detail else ""))
        failed += status != "PASS"
    print(f"\n{len(results) - failed}/{len(results)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
