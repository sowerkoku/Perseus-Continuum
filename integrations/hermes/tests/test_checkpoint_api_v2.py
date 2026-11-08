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


class FakeVault:
    """Stateful in-memory vault: remembers by (category,key), recalls with
    category + ref filters. Exercises the provider against real state."""

    def __init__(self):
        self.store = {}          # (category,key) -> {status, body(dict), args}
        self.calls = []
        self.captured = []       # texts passed to capture
        self.fail_marks = False  # reject checkpoint-consumption writes
        self.reject_receipts = 0  # reject N next receipt writes
        self.reject_capture = False
        # fail the Nth (0-based) recall call per category
        self.fail_recall_call_index = {}
        self.raise_recall_call_index = {}
        self._recall_counts = {}

    def call_tool(self, name, arguments=None, timeout=30.0):
        a = dict(arguments or {})
        self.calls.append((name, a))
        if name == "perseus_vault_remember":
            ck = (a["category"], a["key"])
            if self.fail_marks and a["category"] == "checkpoint" \
                    and a.get("status") == "deprecated":
                return {"ok": False, "text": "mark rejected", "data": None}
            if self.reject_receipts and \
                    a["category"] == "hermes-capture-receipt":
                self.reject_receipts -= 1
                return {"ok": False, "text": "receipt rejected", "data": None}
            try:
                body = json.loads(a.get("body_json") or "{}")
            except Exception:
                body = {}
            self.store[ck] = {"status": a.get("status", "active"),
                              "body": body, "args": a}
            return {"ok": True, "text": "", "data": None}
        if name == "perseus_vault_recall":
            cat = a.get("category", "")
            idx = self._recall_counts.get(cat, 0)
            self._recall_counts[cat] = idx + 1
            if self.fail_recall_call_index.get(cat) == idx:
                return {"ok": False, "text": "read error", "data": None}
            if self.raise_recall_call_index.get(cat) == idx:
                raise ConnectionError("connection reset")
            hits = []
            for (cat, key), ent in self.store.items():
                if cat != a.get("category"):
                    continue
                rt, rv = a.get("ref_type"), a.get("ref_value")
                if rt or rv:
                    refs = ent["args"].get("external_refs") or \
                        ent["body"].get("external_refs") or []
                    if not any(r.get("ref_type") == rt and
                               str(r.get("ref_value", "")).startswith(str(rv or ""))
                               for r in refs):
                        continue
                hits.append({"key": key, "id": key, "status": ent["status"],
                             "body_json": json.dumps(ent["body"])})
            off = int(a.get("offset") or 0)
            lim = a.get("limit")
            page = hits[off:off + int(lim)] if lim is not None else hits[off:]
            return {"ok": True, "text": "", "data": {"results": page}}
        if name == "perseus_vault_capture":
            if self.reject_capture:
                return {"ok": False, "text": "capture rejected", "data": None}
            self.captured.append(a["text"])
            return {"ok": True, "text": "", "data": {"captured": 1}}
        return {"ok": True, "text": "", "data": None}

    def force_status(self, category, key, status):
        ent = self.store.get((category, key))
        if ent:
            ent["status"] = status

    def status_of(self, category, key):
        ent = self.store.get((category, key))
        return ent["status"] if ent else None

    def count(self, tool):
        return sum(1 for (n, _) in self.calls if n == tool)


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
                    if n == "perseus_vault_remember"
                    and a.get("category") == "hermes-capture-receipt"]
        assert receipts, "first run must persist a consume-receipt"
        rbody = json.loads(receipts[0]["body_json"])
        assert rbody.get("consumed_checkpoint") == "ckpt-bbb-222", rbody
        # replay after restart: the draft is STILL draft (marking never
        # ran), but its consume-receipt proves the content is durable:
        # the replay must NOT re-distill, only retry the marking.
        p2 = make_provider(mod)
        p2._client = FakeClient({"perseus_vault_recall": [
            {"ok": True, "text": "", "data": draft},
            {"ok": True, "text": "", "data": {"results": [
                {"key": "consume-x", "status": "active",
                 "body_json": json.dumps(
                     {"consumed_checkpoint": "ckpt-bbb-222"})}]}}]})
        p2.on_session_end([])  # empty buffer = post-restart replay
        assert "perseus_vault_capture" not in p2._client.names(), \
            "consume-receipt must short-circuit re-distillation"
        deps = [a for (n, a) in p2._client.calls
                if n == "perseus_vault_remember" and a.get("category") == "checkpoint"]
        assert deps and deps[0]["status"] == "deprecated", \
            "marking must still be completed (idempotently)"
    check("C5 item-level idempotent recovery via consume-receipt", c5)

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

    # ---- D (audit A): subagent checkpoints are recovered by the parent ----
    def d():
        vault = FakeVault()
        pf = make_provider(mod, session_label="gw-A", agent_context="subagent")
        pf._client = vault
        out = pf.on_pre_compress([
            {"role": "user", "content": "investiga precios del vendor"},
            {"role": "assistant",
             "content": "HALLAZGO FORK: el vendor cambia precios los martes"}])
        ck = out.split(": ", 1)[1]
        # a subagent never distills
        assert vault.count("perseus_vault_capture") == 0
        # the parent session-end recovers the fork evidence via session ref
        pp = make_provider(mod, session_label="gw-A")
        pp._client = vault
        pp._turn_buffer.append({"user": "cierre de semana", "assistant": "ok"})
        pp.on_session_end([])
        assert vault.captured, "parent must distill"
        assert "HALLAZGO FORK" in vault.captured[-1], \
            "fork evidence must flow into the parent distillation"
        assert vault.status_of("checkpoint", ck) == "deprecated", \
            "fork checkpoint must be consumed by the parent"
        # a DIFFERENT chat must not see it (isolation across sessions)
        vault2 = FakeVault()
        for k, ent in vault.store.items():
            vault2.store[k] = ent
        po = make_provider(mod, session_label="gw-OTHER")
        po._client = vault2
        po._turn_buffer.append({"user": "otra sesion", "assistant": "fin"})
        po.on_session_end([])
        assert "HALLAZGO FORK" not in vault2.captured[-1], "session isolation"
    check("D subagent checkpoint recovered by parent, not self-distilled", d)

    # ---- E (audit B): receipt ordering, invalid/incomplete receipt,
    #      marking failure recovery, duplicate-distillation guard ----
    def e():
        # E1: receipt written ONLY after capture ok (rejected -> no receipt)
        vault = FakeVault()
        p = make_provider(mod, session_label="gw-B")
        p._client = vault
        p.on_pre_compress(MSGS)
        names0 = [n for (n, _) in vault.calls]
        p._turn_buffer.append({"user": "u", "assistant": "a"})
        orig = vault.call_tool
        def reject_capture(name, arguments=None, timeout=30.0):
            if name == "perseus_vault_capture":
                vault.calls.append((name, dict(arguments or {})))
                return {"ok": False, "text": "rejected", "data": None}
            return orig(name, arguments, timeout)
        vault.call_tool = reject_capture
        p.on_session_end([])
        vault.call_tool = orig
        assert not any(n == "perseus_vault_remember"
                       and a.get("category") == "hermes-capture-receipt"
                       for (n, a) in vault.calls), "no receipt on failed distill"
        ck_keys = [k for (c, k) in vault.store if c == "checkpoint"]
        assert all(vault.status_of("checkpoint", k) == "draft" for k in ck_keys), \
            "checkpoints stay draft when distillation failed"
        # E2: successful run -> receipt exists with session ref; mark fails ->
        # checkpoints stay draft WITHOUT crashing
        vault2 = FakeVault()
        p2 = make_provider(mod, session_label="gw-C")
        p2._client = vault2
        p2.on_pre_compress(MSGS)
        vault2.fail_marks = True
        p2._turn_buffer.append({"user": "u2", "assistant": "a2"})
        p2.on_session_end([])  # must not raise
        assert any(n == "perseus_vault_remember"
                   and a.get("category") == "hermes-capture-receipt"
                   for (n, a) in vault2.calls), "receipt written after persist"
        ck2 = [k for (c, k) in vault2.store if c == "checkpoint"]
        assert all(vault2.status_of("checkpoint", k) == "draft" for k in ck2)
        # E3: replay (fresh instance) -> receipt found via session ref -> NO
        # second distillation, but the marking is RETRIED and completes
        p3 = make_provider(mod, session_label="gw-C")
        p3._client = vault2
        vault2.fail_marks = False
        p3.on_session_end([])
        assert vault2.count("perseus_vault_capture") == 1, \
            "receipt must prevent duplicate distillation on replay"
        assert all(vault2.status_of("checkpoint", k) == "deprecated" for k in ck2), \
            "marking retried and completed on replay"
    check("E receipt ordering + failed-mark recovery + no duplicate distill", e)

    # ---- F (audit C): full post-restart recovery, 8 steps, real initialize() ----
    def f():
        class StubMCP:
            def __init__(self, *a, **k): pass
            def initialize(self): return {}
            def list_tools(self): return []
        orig_mcp = mod.MCPClient
        mod.MCPClient = StubMCP
        try:
            vault = FakeVault()
            # 1) session A (process 1) creates checkpoints
            p1 = make_provider(mod, session_label="gw-R", session_id="20261107proc1")
            p1._client = vault
            p1.on_pre_compress([
                {"role": "user", "content": "confirmar respaldo incremental nightly"},
                {"role": "assistant", "content": "RESPALDO CONFIRMADO: incremental nightly 02:00"}])
            cks = [k for (c, k) in vault.store if c == "checkpoint"]
            assert cks and all(vault.status_of("checkpoint", k) == "draft" for k in cks)
            # 2) Hermes restarts: process 1 gone
            del p1
            # 3+4) session resumes: NEW session_id, SAME gateway key,
            #      resumed=True — through the REAL initialize() code path
            p2 = mod.PerseusVaultProvider()
            p2._load_config = lambda: p2._config.update(
                {"api_key": "tok", "url": "http://fake"})
            p2.initialize("20261107proc2",
                          gateway_session_key="gw-R", resumed=True)
            assert p2._enabled and p2._resumed
            assert p2._session_label == "gw-R", \
                "identity must come from gateway_session_key, not session_id"
            p2._client = vault  # keep the stateful vault
            # 5) previous checkpoints still associated with this session
            drafts, dstate = p2._draft_checkpoints()
            assert dstate == "complete"
            assert len(drafts) == len(cks), "all drafts must be found post-restart"
            # 6) on_session_end uses them as evidence
            p2.on_session_end([])
            assert vault.captured and "RESPALDO CONFIRMADO" in vault.captured[-1]
            # 7) receipt recorded (session-scoped)
            receipts = [a for (n, a) in vault.calls
                        if n == "perseus_vault_remember"
                        and a.get("category") == "hermes-capture-receipt"]
            assert receipts, "receipt must exist"
            assert any(r.get("ref_type") == "hermes_session"
                       and r.get("ref_value") == "gw-R"
                       for r in receipts[0].get("external_refs", [])), \
                "receipt must be session-scoped"
            # 8) checkpoints consumed
            assert all(vault.status_of("checkpoint", k) == "deprecated" for k in cks)
            # bonus: second session-end after full success -> no re-distill
            p3 = mod.PerseusVaultProvider()
            p3._load_config = lambda: p3._config.update(
                {"api_key": "tok", "url": "http://fake"})
            p3.initialize("20261107proc3", gateway_session_key="gw-R", resumed=True)
            p3._client = vault
            p3.on_session_end([])
            assert vault.count("perseus_vault_capture") == 1, \
                "fully-completed recovery must not re-distill"
        finally:
            mod.MCPClient = orig_mcp
    check("F full 8-step post-restart recovery (real initialize path)", f)

    # ---- G (R3): >20 checkpoints fully paginated; pending resume across
    #      session-ends; every item captured EXACTLY once ----
    def g():
        vault = FakeVault()
        p = make_provider(mod, session_label="gw-G")
        p._client = vault
        keys = []
        for i in range(25):
            out = p.on_pre_compress([
                {"role": "user", "content": "item %d" % i},
                {"role": "assistant",
                 "content": "HECHO-%02d %s" % (i, "x" * 400)}])
            keys.append(out.split(": ", 1)[1])
        assert len(set(keys)) == 25
        dlist, dstate = p._draft_checkpoints()
        assert dstate == "complete" and len(dlist) == 25, \
            "no silent truncation at 20; state must be COMPLETE (R3)"
        st = None
        for _round in range(10):
            pr = make_provider(mod, session_label="gw-G")
            pr._client = vault
            pr.on_session_end([])
            st = [vault.status_of("checkpoint", k) for k in keys]
            if all(s == "deprecated" for s in st):
                break
        assert st and all(s == "deprecated" for s in st), \
            "pending checkpoints must all be recovered, none lost"
        allcaps = "\n".join(vault.captured)
        for i in range(25):
            assert allcaps.count("HECHO-%02d" % i) == 1, \
                "each checkpoint distilled exactly once"
        # receipts pagination >100 (no silent 100-window)
        for i in range(120):
            vault.store[("hermes-capture-receipt", "rx-%d" % i)] = {
                "status": "active",
                "body": {"consumed_checkpoint": "ckpt-nonexistent-%d" % i},
                "args": {"external_refs": [{"ref_type": "hermes_session",
                                            "ref_value": "gw-G"}]}}
        pn = make_provider(mod, session_label="gw-G")
        pn._client = vault
        got, rstate = pn._consumed_checkpoint_keys()
        assert rstate == "complete", "full pagination must report COMPLETE"
        assert len(got) == 145, \
            "receipt lookups must paginate beyond 100 (R3)"
    check("G full pagination: 25 drafts + 120 receipts, each distilled once", g)

    # ---- H (R1): capture truncation consumes ONLY processed items ----
    def h():
        vault = FakeVault()
        p = make_provider(mod, session_label="gw-H")
        p._client = vault
        keys = []
        # each evidence ~2030 chars (1000/msg cap) -> chunk ~2070:
        # only ~3 fit under the 8000-char capture budget
        for i in range(6):
            out = p.on_pre_compress([
                {"role": "user", "content": "usr-%d %s" % (i, "u" * 990)},
                {"role": "assistant",
                 "content": "HECHO-H%d %s" % (i, "Z" * 990)}])
            keys.append(out.split(": ", 1)[1])
        p2 = make_provider(mod, session_label="gw-H")
        p2._client = vault
        p2.on_session_end([])
        st = [vault.status_of("checkpoint", k) for k in keys]
        done = [k for k, s in zip(keys, st) if s == "deprecated"]
        pend = [k for k, s in zip(keys, st) if s == "draft"]
        assert 0 < len(done) < 6 and len(pend) == 6 - len(done), \
            "partial capture: some processed, rest MUST remain draft"
        rec = {json.loads(a["body_json"])["consumed_checkpoint"]
               for (n, a) in vault.calls
               if n == "perseus_vault_remember"
               and a.get("category") == "hermes-capture-receipt"}
        assert rec == set(done), \
            "receipts only for checkpoints actually in the persisted capture"
    check("H partial capture consumes ONLY processed checkpoints", h)

    # ---- I (R2): receipt write rejected (explicit, then partial) ----
    def i_():
        vault = FakeVault()
        p = make_provider(mod, session_label="gw-I")
        p._client = vault
        k1 = p.on_pre_compress([
            {"role": "user", "content": "a1"},
            {"role": "assistant", "content": "ALPHA-1 uno"}]
        ).split(": ", 1)[1]
        k2 = p.on_pre_compress([
            {"role": "user", "content": "a2"},
            {"role": "assistant", "content": "ALPHA-2 dos"}]
        ).split(": ", 1)[1]
        vault.reject_receipts = 1  # first receipt REJECTED, second accepted
        p2 = make_provider(mod, session_label="gw-I")
        p2._client = vault
        p2.on_session_end([])
        assert vault.status_of("checkpoint", k1) == "draft", \
            "rejected receipt -> NOT consumed (stays available)"
        assert vault.status_of("checkpoint", k2) == "deprecated", \
            "confirmed receipt -> consumed (partial success handled per item)"
        # replay: k1 re-distilled (bounded duplicate), now accepted
        p3 = make_provider(mod, session_label="gw-I")
        p3._client = vault
        p3.on_session_end([])
        assert vault.status_of("checkpoint", k1) == "deprecated"
        assert vault.count("perseus_vault_capture") == 2, \
            "only the unconfirmed item is re-distilled"
    check("I receipt rejection explicit + partial receipt failure per-item", i_)

    # ---- J: capture failure consumes NOTHING ----
    def j():
        vault = FakeVault()
        p = make_provider(mod, session_label="gw-J")
        p._client = vault
        k = p.on_pre_compress([
            {"role": "user", "content": "pendiente J"},
            {"role": "assistant", "content": "HECHO-J uno"}]).split(": ", 1)[1]
        vault.reject_capture = True
        p._turn_buffer.append({"user": "buffer", "assistant": "tambien"})
        p.on_session_end([])
        assert vault.status_of("checkpoint", k) == "draft"
        assert not vault.captured
        assert not any(n == "perseus_vault_remember"
                       and (a.get("category") == "hermes-capture-receipt"
                            or a.get("status") == "deprecated")
                       for (n, a) in vault.calls), \
            "failed capture: no receipts, no consumption marking"
        # a later attempt succeeds and completes the flow
        vault.reject_capture = False
        p2 = make_provider(mod, session_label="gw-J")
        p2._client = vault
        p2.on_session_end([])
        assert vault.status_of("checkpoint", k) == "deprecated"
    check("J capture failure consumes nothing; later attempt completes", j)

    # ---- K (concurrency contract): host serializes session-end per
    #      session; worst admissible overlap = a second lifecycle that
    #      crashed BEFORE marking must NOT re-distill. True simultaneous
    #      threads are the host's contract (documented), not testable
    #      at provider level; this pins the guard that contract relies on. ----
    def k():
        vault = FakeVault()
        p1 = make_provider(mod, session_label="gw-K")
        p1._client = vault
        k1 = p1.on_pre_compress([
            {"role": "user", "content": "kv"},
            {"role": "assistant", "content": "HECHO-K durable"}]
        ).split(": ", 1)[1]
        p1._turn_buffer.append({"user": "u", "assistant": "a"})
        p1.on_session_end([])
        assert vault.status_of("checkpoint", k1) == "deprecated"
        # simulate a second, overlapping session-end whose process died
        # BEFORE marking: status back to draft, receipt intact
        vault.force_status("checkpoint", k1, "draft")
        p2 = make_provider(mod, session_label="gw-K")
        p2._client = vault
        p2.on_session_end([])
        assert vault.count("perseus_vault_capture") == 1, \
            "overlap after confirmed persistence must never re-distill"
        assert vault.status_of("checkpoint", k1) == "deprecated", \
            "and must complete the marking"
    check("K host-serialization contract guard (no re-distill on overlap)", k)

    # ---- L (R3 fail-safe): a partial receipt read must NOT masquerade
    #      as complete; the cycle aborts, nothing is distilled or
    #      consumed, and a later clean read completes exactly-once ----
    def l_():
        vault = FakeVault()
        p = make_provider(mod, session_label="gw-L")
        p._client = vault
        keys = []
        for i in range(60):   # >1 page of receipts after round 1
            out = p.on_pre_compress([
                {"role": "user", "content": "l%d" % i},
                {"role": "assistant", "content": "HECHO-L%02d hecho" % i}])
            keys.append(out.split(": ", 1)[1])
        p.on_session_end([])  # round 1: all fit, 60 receipts persisted
        assert all(vault.status_of("checkpoint", k) == "deprecated"
                   for k in keys)
        # simulate: one checkpoint left draft (mark never happened) and
        # the SECOND receipts page now fails to read (60 receipts:
        # call 0 = pre-capture lookup in round 1, call 1 = page 1 of
        # the replay, call 2 = page 2 -> fails)
        pending = keys[55]
        vault.force_status("checkpoint", pending, "draft")
        vault.fail_recall_call_index = {"hermes-capture-receipt": 2}
        caps_before = vault.count("perseus_vault_capture")
        p2 = make_provider(mod, session_label="gw-L")
        p2._client = vault
        p2.on_session_end([])
        assert vault.count("perseus_vault_capture") == caps_before, \
            "incomplete receipt read must NOT distill (fail-closed)"
        assert vault.status_of("checkpoint", pending) == "draft", \
            "incomplete receipt read must NOT consume anything"
        # clean read afterwards: the pending item completes exactly once
        vault.fail_recall_call_index = {}
        vault._recall_counts = {}
        p3 = make_provider(mod, session_label="gw-L")
        p3._client = vault
        p3.on_session_end([])
        assert vault.status_of("checkpoint", pending) == "deprecated"
        assert vault.count("perseus_vault_capture") == caps_before, \
            "clean retry completes the marking WITHOUT re-distilling \n" \
            "(the intact receipt proves round-1 persistence)"
    check("L partial receipt read fails the cycle closed, then resumes", l_)

    # ---- N (R3 tri-state): a RAISED exception on a page (transport
    #      failure, vs L's ok:false rejection) also yields FAILED and
    #      must abort the cycle, then recover cleanly. ----
    def n():
        vault = FakeVault()
        p = make_provider(mod, session_label="gw-N")
        p._client = vault
        k0 = p.on_pre_compress([
            {"role": "user", "content": "n0"},
            {"role": "assistant", "content": "HECHO-N0 base"}]
        ).split(": ", 1)[1]
        p.on_session_end([])
        assert vault.status_of("checkpoint", k0) == "deprecated"
        for i in range(60):
            vault.store[("hermes-capture-receipt", "nrx-%d" % i)] = {
                "status": "active",
                "body": {"consumed_checkpoint": "ckpt-n-%d" % i},
                "args": {"external_refs": [{"ref_type": "hermes_session",
                                            "ref_value": "gw-N"}]}}
        k2 = p.on_pre_compress([
            {"role": "user", "content": "n2"},
            {"role": "assistant", "content": "HECHO-N2 pendiente"}]
        ).split(": ", 1)[1]
        pn = make_provider(mod, session_label="gw-N")
        pn._client = vault
        vault.raise_recall_call_index = {"hermes-capture-receipt": 1}
        _, st_exc = pn._consumed_checkpoint_keys()
        assert st_exc == "failed", "raised page must report FAILED"
        vault.raise_recall_call_index = {}
        vault._recall_counts = {}
        vault.fail_recall_call_index = {"hermes-capture-receipt": 1}
        _, st_rej = pn._consumed_checkpoint_keys()
        assert st_rej == "failed", "rejected page must report FAILED"
        vault.fail_recall_call_index = {}
        vault._recall_counts = {}
        caps_before = vault.count("perseus_vault_capture")
        vault.raise_recall_call_index = {"hermes-capture-receipt": 1}
        pn.on_session_end([])
        assert vault.count("perseus_vault_capture") == caps_before, \
            "raised page must NOT distill"
        assert vault.status_of("checkpoint", k2) == "draft", \
            "raised page must NOT consume"
        vault.raise_recall_call_index = {}
        vault._recall_counts = {}
        p2 = make_provider(mod, session_label="gw-N")
        p2._client = vault
        p2.on_session_end([])
        assert vault.status_of("checkpoint", k2) == "deprecated"
    check("N raised page = FAILED; aborts cycle, then resumes clean", n)

    # ---- O (return-contract regression): an empty session label is
    #      INVALID context (initialize() always gets session_id from the
    #      host); every helper path must keep the tuple contract and the
    #      cycle must fail closed — never raise ValueError on unpack. ----
    def o():
        vault = FakeVault()
        p = make_provider(mod, session_label="gw-O")
        p._client = vault
        p._session_label = ""           # invalid context (defensive path)
        dlist, dst = p._draft_checkpoints()      # must not raise
        assert dlist == [] and dst == "failed"
        keys, rstate = p._consumed_checkpoint_keys()
        assert keys == set() and rstate == "failed"
        p.on_session_end([])           # must return quietly, fail-closed
        assert vault.count("perseus_vault_capture") == 0, \
            "invalid context must not distill anything"
    check("O empty-label keeps tuple contract and fails closed", o)

    # ---- M (R3 tri-state): the defensive cap yields TRUNCATED, which
    #      must be treated as failure: cycle aborts, nothing consumed.
    #      The cap is a protection, never a license to process. ----
    def m():
        vault = FakeVault()
        p = make_provider(mod, session_label="gw-M")
        p._client = vault
        k0 = p.on_pre_compress([
            {"role": "user", "content": "m0"},
            {"role": "assistant", "content": "HECHO-M0 base"}]
        ).split(": ", 1)[1]
        p.on_session_end([])
        assert vault.status_of("checkpoint", k0) == "deprecated"
        # pad receipts above the (temporarily lowered) safety cap
        for i in range(99):
            vault.store[("hermes-capture-receipt", "mrx-%d" % i)] = {
                "status": "active",
                "body": {"consumed_checkpoint": "ckpt-pad-%d" % i},
                "args": {"external_refs": [{"ref_type": "hermes_session",
                                            "ref_value": "gw-M"}]}}
        k2 = p.on_pre_compress([
            {"role": "user", "content": "m2"},
            {"role": "assistant", "content": "HECHO-M2 pendiente"}]
        ).split(": ", 1)[1]
        p2 = make_provider(mod, session_label="gw-M")
        p2._client = vault
        p2._RECALL_MAX_ITEMS = 50          # force TRUNCATED (100 receipts)
        _, rstate = p2._consumed_checkpoint_keys()
        assert rstate == "truncated", \
            "cap hit must report TRUNCATED, not a usable partial"
        caps_before = vault.count("perseus_vault_capture")
        p2.on_session_end([])
        assert vault.count("perseus_vault_capture") == caps_before, \
            "TRUNCATED must NOT distill"
        assert vault.status_of("checkpoint", k2) == "draft", \
            "TRUNCATED must NOT consume anything"
        # restore a complete read: item completes normally
        p3 = make_provider(mod, session_label="gw-M")
        p3._client = vault
        p3.on_session_end([])
        assert vault.status_of("checkpoint", k2) == "deprecated"
    check("M cap=TRUNCATED aborts the cycle; never processed as complete", m)

    width = max(len(n) for n, _, _ in results)
    failed = 0
    for name, status, detail in results:
        print(f"{status:5s}  {name}" + (f"\n        -> {detail}" if detail else ""))
        failed += status != "PASS"
    print(f"\n{len(results) - failed}/{len(results)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
