"""Contract tests for the Hermes checkpoint API v2 implementation (patch 0003).

Standalone runner (no pytest needed):  python3 test_checkpoint_api_v2.py

Loads the embedded PROVIDER_PY from ../install-perseus-vault.py, injects a
fake MCP client, and asserts the contract conditions from the architecture
review:

  C1  checkpoint identity (session + profile + context scoping)
  C2  persistence failure semantics (raise, never partial success)
  C3  normalized evidence (API v2 rows only; documented tool-output gap)
  C4  safe consumption order (mark consumed ONLY after distill persisted)
  C5  idempotent recovery via distillation receipt
  C6  regression: redaction, accent-fold, session-key preamble coexist
"""
import ast
import json
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALLER = HERE.parent / "install-perseus-vault.py"


def load_provider_module():
    src = INSTALLER.read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "PROVIDER_PY":
                    code = ast.literal_eval(node.value)
    mod = types.ModuleType("perseus_provider_under_test")
    stub = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # minimal host stub
        pass

    stub.MemoryProvider = MemoryProvider
    stub.spawn_context_thread = lambda fn, name=None: None
    agent_pkg = types.ModuleType("agent")
    agent_pkg.memory_provider = stub
    sys.modules.setdefault("agent", agent_pkg)
    sys.modules["agent.memory_provider"] = stub
    exec(compile(code, str(INSTALLER) + ":PROVIDER_PY", "exec"), mod.__dict__)
    return mod


class FakeClient:
    """Scriptable call_tool recorder. responses: {tool: [resp, ...] or callable}"""

    def __init__(self, responses=None, default_ok=True):
        self.calls = []
        self.responses = dict(responses or {})
        self.default_ok = default_ok

    def call_tool(self, name, arguments=None, timeout=30.0):
        self.calls.append((name, dict(arguments or {})))
        spec = self.responses.get(name)
        if callable(spec):
            return spec(name, arguments or {})
        if isinstance(spec, list):
            return spec.pop(0) if spec else {"ok": self.default_ok, "text": "", "data": None}
        if spec is not None:
            return spec
        return {"ok": self.default_ok, "text": "", "data": None}

    def names(self):
        return [c[0] for c in self.calls]

    def args_for(self, tool, nth=0):
        hits = [a for (n, a) in self.calls if n == tool]
        return hits[nth] if len(hits) > nth else None


def make_provider(mod, session_label="chat-77", agent_label="default",
                 agent_context="primary", session_id="20261107_sess"):
    p = mod.PerseusVaultProvider()
    p._session_id = session_id
    p._session_label = session_label
    p._agent_label = agent_label
    p._agent_context = agent_context
    p._enabled = True
    return p


OK = {"ok": True, "text": "", "data": None}
MSGS = [
    {"role": "user", "content": "Decision: el backup diario usa rsync a NAS, diseño verificado"},
    {"role": "assistant", "content": "Registrado con token sk-abcdefghijklmnop"},
]

results = []


def check(name, fn):
    try:
        fn()
        results.append((name, "PASS", ""))
    except AssertionError as e:
        results.append((name, "FAIL", str(e)))
    except Exception as e:
        results.append((name, "ERROR", f"{type(e).__name__}: {e}"))


def main():
    mod = load_provider_module()

    # ---- contract advertisement ----
    def c0():
        assert mod.PerseusVaultProvider.pre_compress_checkpoint_api_version == 2
    check("C0 class advertises checkpoint API v2", c0)

    # ---- C1 identity: session/profile/context scoping, content-addressed ----
    def c1():
        p = make_provider(mod, session_label="chat-A")
        p._client = FakeClient()
        k1 = p.on_pre_compress(MSGS)
        p2 = make_provider(mod, session_label="chat-B")
        p2._client = FakeClient()
        k2 = p2.on_pre_compress(MSGS)
        assert k1 != k2, "same text in different sessions must not collide"
        assert p._client.args_for("perseus_vault_remember")["external_refs"][0]["ref_value"] == "chat-A"
        # same session + same content -> same key (upsert), no error
        p._client = FakeClient()
        k1b = p.on_pre_compress(MSGS)
        assert k1b == k1, f"replay must reuse key: {k1} vs {k1b}"
        # different profile -> different key even for same session label
        p3 = make_provider(mod, session_label="chat-A", agent_label="webon")
        p3._client = FakeClient()
        k3 = p3.on_pre_compress(MSGS)
        assert k3 != k1, "profile must be part of the identity"
        # subagent context isolated from primary
        p4 = make_provider(mod, session_label="chat-A", agent_context="subagent")
        p4._client = FakeClient()
        k4 = p4.on_pre_compress(MSGS)
        assert k4 != k1, "subagent scope must not collide with primary"
    check("C1 checkpoint identity (session+profile+context+content)", c1)

    # ---- C2 persistence failure: raise, never partial success ----
    def c2():
        p = make_provider(mod)
        p._client = FakeClient({"perseus_vault_remember": {"ok": False, "text": "server error", "data": None}})
        try:
            p.on_pre_compress(MSGS)
            raise AssertionError("must raise when write rejected")
        except RuntimeError:
            pass
        p._client = FakeClient({"perseus_vault_remember": RuntimeError("conn refused")})
        try:
            p._client = FakeClient()
            def boom(name, args):
                raise RuntimeError("conn refused")
            p._client.responses["perseus_vault_remember"] = boom
            p.on_pre_compress(MSGS)
            raise AssertionError("must raise on transport failure")
        except RuntimeError:
            pass
        # success path returns the checkpoint token format
        p._client = FakeClient()
        out = p.on_pre_compress(MSGS)
        assert out.startswith("checkpoint: ckpt-"), out
        body = json.loads(p._client.args_for("perseus_vault_remember")["body_json"])
        assert body["kind"] == "hermes-pre-compress-checkpoint"
        assert p._client.args_for("perseus_vault_remember")["status"] == "draft"
    check("C2 raise on failed write; checkpoint: token on durable success", c2)

    # ---- C3 normalized evidence (v2 rows only) ----
    def c3():
        p = make_provider(mod)
        p._client = FakeClient()
        msgs = [
            {"role": "system", "content": "sys prompt"},
            {"role": "tool", "content": "tool output with SECRET-ONLY-FACT"},
            {"role": "user", "content": "hecho veridico uno"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": "[compressed summary] resumen viejo"},
            {"role": "user", "content": {"_compressed_summary": True, "text": "x"}},
            {"role": "assistant", "content": "prosa del asistente con diseño acentuado"},
        ]
        p.on_pre_compress(msgs)
        body = json.loads(p._client.args_for("perseus_vault_remember")["body_json"])
        ev = body["evidence"]
        assert "hecho veridico uno" in ev and "prosa del asistente" in ev
        assert "SECRET-ONLY-FACT" not in ev, "tool rows must be excluded (documented gap)"
        assert "sys prompt" not in ev
        assert "resumen viejo" not in ev, "prior summaries must not be re-archived"
        assert "diseño" not in ev and "diseno" in ev, "accent-fold applies to evidence"
        # no checkpointable rows -> raise (never claim a checkpoint without a write)
        try:
            p.on_pre_compress([{"role": "tool", "content": "only tools"}])
            raise AssertionError("must raise when no evidence rows")
        except RuntimeError:
            pass
    check("C3 normalized evidence rows only + documented gap", c3)

    # ---- C4 safe consumption order ----
    def c4():
        draft = {"results": [{"key": "ckpt-aaa-111", "status": "draft",
                              "body_json": json.dumps({"evidence": "USER: evidencia previa"})}]}
        p = make_provider(mod)
        p._client = FakeClient({"perseus_vault_recall": [{"ok": True, "text": "", "data": draft},
                                                         {"ok": True, "text": "", "data": {"results": []}}],
                                "perseus_vault_capture": [OK]})
        p._turn_buffer.append({"user": "turno nuevo", "assistant": "respuesta nueva"})
        p.on_session_end([])
        names = p._client.names()
        assert names[0] == "perseus_vault_recall", names  # read checkpoints first
        assert "perseus_vault_capture" in names
        cap_args = p._client.args_for("perseus_vault_capture")
        assert "evidencia previa" in cap_args["text"], "checkpoint must feed distillation"
        assert "turno nuevo" in cap_args["text"]
        deps = [a for (n, a) in p._client.calls
                if n == "perseus_vault_remember" and a.get("category") == "checkpoint"]
        assert deps and deps[0]["status"] == "deprecated", "must mark consumed after verified persist"
        # order: capture before deprecated-marking
        assert names.index("perseus_vault_capture") < names.index("perseus_vault_remember")
        # capture REJECTED -> checkpoints stay draft, no receipt
        p2 = make_provider(mod)
        p2._client = FakeClient({"perseus_vault_recall": [{"ok": True, "text": "", "data": draft},
                                                          {"ok": True, "text": "", "data": {"results": []}}],
                                 "perseus_vault_capture": {"ok": False, "text": "rejected", "data": None}})
        p2._turn_buffer.append({"user": "hola", "assistant": "adios"})
        p2.on_session_end([])
        assert not any(a.get("status") == "deprecated"
                       for (n, a) in p2._client.calls if n == "perseus_vault_remember"), \
            "failed distillation must NOT consume checkpoints"
        assert all(a.get("category") != "hermes-capture-receipt"
                   for (n, a) in p2._client.calls if n == "perseus_vault_remember"), \
            "no receipt on failed distillation"
    check("C4 safe consumption order + draft retained on failure", c4)

    # ---- C5 idempotent recovery (receipt short-circuits re-distill) ----
    def c5():
        draft = {"results": [{"key": "ckpt-bbb-222", "status": "draft",
                              "body_json": {"evidence": "evidencia"}}]}
        p = make_provider(mod)
        # build the transcript once to learn the receipt key: emulate by
        # letting first run create receipt, second run (fresh provider) skip.
        p._client = FakeClient({"perseus_vault_recall": [{"ok": True, "text": "", "data": draft},
                                                         {"ok": True, "text": "", "data": {"results": []}}],
                                "perseus_vault_capture": [OK]})
        p._turn_buffer.append({"user": "u1", "assistant": "a1"})
        p.on_session_end([])
        receipts = [a for (n, a) in p._client.calls
                    if n == "perseus_vault_remember" and a.get("category") == "hermes-capture-receipt"]
        assert receipts, "first run must write a distillation receipt"
        receipt_key = receipts[0]["key"]
        # replay after crash (drafts still there, receipt present)
        draft2 = {"results": [{"key": "ckpt-bbb-222", "status": "draft",
                               "body_json": json.dumps({"evidence": "evidencia"})}]}
        p2 = make_provider(mod)
        p2._client = FakeClient({"perseus_vault_recall": [
            {"ok": True, "text": "", "data": draft2},
            {"ok": True, "text": "", "data": {"results": [{"key": receipt_key,
                                                           "status": "active", "body_json": "{}"}]}}]})
        p2._turn_buffer.append({"user": "u1", "assistant": "a1"})
        p2.on_session_end([])
        assert "perseus_vault_capture" not in p2._client.names(), \
            "receipt must short-circuit re-distillation (idempotent recovery)"
        dep = p2._client.args_for("perseus_vault_remember")
        assert dep and dep["status"] == "deprecated", "still (idempotently) consumes checkpoints"
    check("C5 idempotent recovery via content-keyed receipt", c5)

    # ---- C6 regression: fixes 0001/0002 coexist with 0003 ----
    def c6():
        p = make_provider(mod, session_id="20261107abcdef1234")
        p._client = FakeClient()
        p.on_pre_compress([{"role": "user",
                           "content": "configurar token =hunter2 y clave sk-abcdefghijklm en 192.168.1.54, diseño"}])
        ev = json.loads(p._client.args_for("perseus_vault_remember")["body_json"])["evidence"]
        assert "hunter2" not in ev and "[REDACTED]" in ev, "redaction must still apply"
        assert "[redacted-ip]" in ev, "LAN redaction must still apply"
        assert "diseno" in ev, "accent-fold must still apply"
        # session-keyed preamble preserved on distillation
        p2 = make_provider(mod, session_id="20261107abcdef1234")
        p2._client = FakeClient({"perseus_vault_recall": {"ok": True, "text": "", "data": {"results": []}},
                                "perseus_vault_capture": OK})
        p2._turn_buffer.append({"user": "tarea: diseñar el plan", "assistant": "ok token sk-abcdefghij1234"})
        p2.on_session_end([])
        tx = p2._client.args_for("perseus_vault_capture")["text"]
        assert tx.startswith("[session 20261107abcd]"), "session-key preamble preserved"
        assert "sk-abcdefghij1234" not in tx and "[REDACTED]" in tx
        assert "disenar" in tx and "diseñar" not in tx
    check("C6 redaction + accent-fold + session-keys coexist (0001/0002)", c6)

    width = max(len(n) for n, _, _ in results)
    failed = 0
    for name, status, detail in results:
        print(f"{status:5s}  {name}" + (f"\n        -> {detail}" if detail else ""))
        failed += status != "PASS"
    print(f"\n{len(results) - failed}/{len(results)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
