"""Suite G - D-1 recall-hardening tests (query normalization, preview fallback, CLI items).

Standalone runner (no pytest):  python3 test_recall_hardening_d1.py

Defects under test (operational cycle 2026-11-15, server issues #10/#11):
  R1 _fts_query: strip accents/punctuation/function words; keep tokens
     containing digits; deduplicate preserving order; greetings are not
     semantic signal.
  R2 Empty normalization => ZERO vault calls and empty block (by design:
     absence of signal is not an empty match expression).
  R3 One bounded retry WITHOUT preview_cap when the preview-capped call
     fails (server char-boundary panic -> HTTP 500). Exactly one retry.
  R4 Both attempts failing => empty block, still exactly one retry
     (no retry loops).
  R5 recall_when receives the NORMALIZED context, not the raw message.
  R6 CLI asset parses server items/results (was: 'entities' only, so the
     CLI always printed "Found 0").
  M0 meta (fail-first): R2/R3/R6 MUST fail against the released 0.4.0
     installer (e6a38bbd) - a test that passes against uncorrected code
     proves nothing.
  H  version traceability (0.4.2): PLUGIN_VERSION == embedded plugin.yaml;
     the banner prints the integration version and per-asset SHA-256s
     (verified equal to the module's assets), before the fail-closed gate;
     the gate still writes nothing; the installed CLI status prints the
     version declared by the installed plugin.yaml.
  I  remember body_json normalization (0.4.2): a dict/list body_json is
     coerced to a JSON string BEFORE accent-folding (server schema is
     strict: "invalid type: map, expected a string"); string input keeps
     working unchanged; non-remember tools are untouched. Unit scope:
     normalization logic only (base class stubbed here); MemoryProvider
     contract acceptance stays with the real-Hermes tests.
"""
import ast
import subprocess
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALLER = HERE.parent / "install-perseus-vault.py"

sys.path.insert(0, str(HERE))
from test_contract_i1 import load_provider  # stub loader for agent.memory_provider

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def get_asset(src, tag):
    t = ast.parse(src)
    for s in t.body:
        if isinstance(s, ast.Assign):
            for tgt in s.targets:
                if getattr(tgt, "id", "") == tag:
                    return ast.literal_eval(s.value)
    raise KeyError(tag)


class RecorderClient:
    def __init__(self, fail_preview=False, items=None):
        self.calls = []
        self.fail_preview = fail_preview
        self.items = items if items is not None else []

    def call_tool(self, name, args, timeout=None):
        self.calls.append((name, dict(args)))
        if name == "perseus_vault_recall" and "preview_cap" in args and self.fail_preview:
            return {"ok": False, "text": ""}
        if name == "perseus_vault_recall" and self.items == "ALWAYS_EMPTY":
            return {"ok": True, "data": {"items": []}}
        return {"ok": True, "data": {"items": self.items}}


def make_provider(installer_src, client):
    mod = load_provider(installer_src, "PROVIDER_PY")
    p = mod.PerseusVaultProvider.__new__(mod.PerseusVaultProvider)
    p._client = client
    p._enabled = True
    p._ws_args = lambda: {}
    return p, mod


def main():
    src = INSTALLER.read_text()
    fts = get_asset(src, "PROVIDER_PY")
    cli = get_asset(src, "CLI_PY")

    # M0: released 0.4.0 asset for fail-first meta
    old_src = subprocess.run(
        ["git", "-C", str(HERE.parent.parent), "show",
         "e6a38bbd:integrations/hermes/install-perseus-vault.py"],
        capture_output=True, text=True).stdout
    assert "PROVIDER_PY" in old_src, "meta: cannot load released installer"

    print("== R1 _fts_query ==")
    _, mod = make_provider(src, RecorderClient(items="ALWAYS_EMPTY"))
    P = mod.PerseusVaultProvider
    check("R1a natural question normalizes, keeps digit tokens",
          P._fts_query("¿Qué sabes del despliegue 0.4.0 en .53?")
          == "sabes despliegue 0.4.0 53")
    for trivial in ("", "hola", "Listo!", "dale gracias", "ok"):
        check(f"R1b trivial has no signal: {trivial!r}", P._fts_query(trivial) == "")
    check("R1c accents normalize",
          P._fts_query("operación matemática") == "operacion matematica")
    check("R1d dedup preserving order",
          P._fts_query("vault vault perseus") == "vault perseus")

    print("== R2 empty signal => zero calls ==")
    c = RecorderClient()
    p, _ = make_provider(src, c)
    blk = p._build_recall_block("hola")
    check("R2 zero calls and empty block", c.calls == [] and blk == "")

    print("== R3 bounded preview fallback ==")
    item = {"id": "x1", "category": "decision", "key": "k",
            "summary": "despliegue 0.4.0 instalado"}
    c = RecorderClient(fail_preview=True, items=[item])
    p, _ = make_provider(src, c)
    blk = p._build_recall_block("despliegue 0.4.0 instalado")
    recalls = [ca for ca in c.calls if ca[0] == "perseus_vault_recall"]
    check("R3 exactly one preview + one non-preview attempt",
          len(recalls) == 2 and "preview_cap" in recalls[0][1]
          and "preview_cap" not in recalls[1][1])
    check("R3 block recovered from retry", "despliegue 0.4.0 instalado" in blk)

    print("== R4 both fail: no retry loop ==")
    class AlwaysFail:
        def __init__(self): self.calls = []
        def call_tool(self, name, args, timeout=None):
            self.calls.append((name, dict(args)))
            return {"ok": False, "text": ""}
    c = AlwaysFail()
    p, _ = make_provider(src, c)
    blk = p._build_recall_block("despliegue 0.4.0")
    recalls = [ca for ca in c.calls if ca[0] == "perseus_vault_recall"]
    check("R4 exactly two recall attempts total", len(recalls) == 2)
    check("R4 empty block", blk == "")

    print("== R5 recall_when receives normalized context ==")
    c = RecorderClient(items="ALWAYS_EMPTY")
    p, _ = make_provider(src, c)
    p._build_recall_block("¿Qué sabes del despliegue 0.4.0 en .53?")
    rw = [a for n, a in c.calls if n == "perseus_vault_recall_when"]
    check("R5 context normalized",
          len(rw) == 1 and rw[0]["context"] == "sabes despliegue 0.4.0 53")

    print("== R6 CLI parses items ==")
    check("R6 CLI reads items/results", 'payload.get("items")' in cli
          and 'payload.get("results")' in cli)

    print("== M0 meta: tests must fail against released 0.4.0 ==")
    old_cli = get_asset(old_src, "CLI_PY")
    check("M0 released CLI did NOT read items", 'payload.get("items")' not in old_cli)
    oldp, oldmod = make_provider(old_src, RecorderClient())
    has_fts = hasattr(oldmod.PerseusVaultProvider, "_fts_query")
    check("M0 released provider had no normalization", not has_fts)
    c = RecorderClient()
    oldp, _ = make_provider(old_src, c)
    oldp._build_recall_block("hola")
    check("M0 released provider DID call vault for 'hola'", len(c.calls) > 0)
    c = RecorderClient(fail_preview=True, items=[item])
    oldp, _ = make_provider(old_src, c)
    blk = oldp._build_recall_block("despliegue 0.4.0")
    recalls = [ca for ca in c.calls if ca[0] == "perseus_vault_recall"]
    check("M0 released provider had no preview fallback",
          len(recalls) == 1)

    print("== H version traceability (0.4.2) ==")
    cur = INSTALLER.read_text(encoding="utf-8")
    ns = {"__name__": "inst_under_test", "__file__": str(INSTALLER)}
    exec(compile(cur, str(INSTALLER), "exec"), ns)
    v = ns["PLUGIN_VERSION"]
    check("H1 declared version == embedded plugin.yaml",
          v == ns["plugin_yaml_version"]() and f'version: "{v}"' in ns["PLUGIN_YAML"])
    import hashlib as _hl
    import os as _os
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        gate = Path(td) / "empty-gate"
        gate.mkdir()
        home = Path(td) / "empty-home"
        home.mkdir()
        env = dict(__import__("os").environ)
        env.update({"PERSEUS_GATE_HOST_ROOT": str(gate), "HERMES_HOME": str(home)})
        r = subprocess.run([sys.executable, str(INSTALLER)], capture_output=True,
                           text=True, timeout=30, env=env, input="")
        out = r.stdout
        want = _hl.sha256(ns["PROVIDER_PY"].encode()).hexdigest()[:12]
        check("H2 banner prints integration version",
              f"Integration version: {v}" in out)
        check("H2 banner asset digest == module asset digest",
              f"provider.py: sha256 {want}" in out)
        check("H2 gate still fail-closed: no writes, rc=1",
              r.returncode == 1 and not any(home.rglob("*")))
    cli = get_asset(cur, "CLI_PY")
    check("H3 CLI status reads installed plugin.yaml and prints version",
          cli.count("_installed_manifest_version()") >= 2
          and 'print(f"Version:' in cli)

    print("== I remember body_json normalization (0.4.2) ==")
    import json as _json
    import types as _types
    _ag = _types.ModuleType("agent")
    _mp = _types.ModuleType("agent.memory_provider")
    class _Base:  # unit scope: the base class plays no role in the
        pass      # normalization logic under test
    _mp.MemoryProvider = _Base
    _mp.spawn_context_thread = lambda *a, **k: None  # unit scope: host
    _ag.memory_provider = _mp                        # contract stays elsewhere
    _saved = {k: sys.modules.get(k) for k in
              ("agent", "agent.memory_provider")}
    sys.modules["agent"] = _ag
    sys.modules["agent.memory_provider"] = _mp
    try:
        pm = _types.ModuleType("pv_provider_unit")
        pm.__file__ = "provider_unit"
        exec(compile(ns["PROVIDER_PY"], "provider_unit", "exec"),
             pm.__dict__)
        cap = {}
        class _FC:
            def call_tool(self, name, args, timeout=None):
                cap["args"] = args
                return {"ok": True}
        prov = pm.PerseusVaultProvider.__new__(pm.PerseusVaultProvider)
        prov._enabled = True
        prov._client = _FC()
        prov._ws_args = lambda: {}
        prov.handle_tool_call("perseus_vault_remember",
            {"category": "x", "key": "k",
             "body_json": {"nota": "operaci\u00f3n real"}})
        bj = cap["args"]["body_json"]
        check("I1 object body_json reaches the server as a JSON string",
              isinstance(bj, str)
              and _json.loads(bj) == {"nota": "operacion real"})
        prov.handle_tool_call("perseus_vault_remember",
            {"category": "x", "key": "k",
             "body_json": '{"nota": "operaci\u00f3n"}'})
        bj2 = cap["args"]["body_json"]
        check("I2 string body_json unchanged in kind, still folded",
              isinstance(bj2, str) and "operacion" in bj2
              and _json.loads(bj2) == {"nota": "operacion"})
        prov.handle_tool_call("perseus_vault_remember",
            {"body_json": ["\u00c1a"]})
        bj3 = cap["args"]["body_json"]
        check("I3 list body_json normalized to JSON string",
            isinstance(bj3, str) and _json.loads(bj3) == ["Aa"])
        prov.handle_tool_call("perseus_vault_recall", {"query": "x"})
        check("I4 non-remember tools untouched by the coercion",
              "body_json" not in cap["args"]
              and cap["args"].get("query") == "x"
              and cap["args"].get("preview_cap") == pm._PREVIEW_CAP)
    finally:
        for k, v in _saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    print(f"\nG: {PASS}/{PASS + FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
