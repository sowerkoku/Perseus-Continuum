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

    print(f"\nG: {PASS}/{PASS + FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
