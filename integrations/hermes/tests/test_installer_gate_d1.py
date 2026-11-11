#!/usr/bin/env python3
"""Suite F — D1 installer gate (contract rev.5, authorized 2026-11-10).

The installer must REFUSE to modify anything unless the installed host
is exactly Hermes 0.23.1 with the critical APIs:
  G1 exact version read from the installed host itself (never inferred
     from the installer or plugin.yaml); ambiguous/unreadable = abort.
  G2 spawn_context_thread importable and callable from the real module.
  G3 on_session_switch with new_session_id, parent_session_id, reset,
     rewound EXPLICIT (never absorbed by **kwargs), via inspect.signature.
  G4 the host's real initialization path (MemoryManager.initialize_all)
     verifiably injects hermes_home and propagates it to initialize.
  G5 declared version == embedded plugin.yaml, enforced BEFORE writes.

Every check runs before any file is created or modified; inability to
verify is a failure. Test seam PERSEUS_GATE_HOST_ROOT forces strict use
of the given root (no fallback to the real host).

Method: the REAL installer runs as a subprocess against controlled
stub hosts under a temporary HERMES_HOME; a snapshot (paths + content
hashes + permission bits) must be byte-identical after any abort.

Fail-first: F0 and F6a are baseline guards (green before and after).
F1-F5, F6b, F7 are RED against the current gate-less installer and
turn green with the gate.

Standalone: python3 integrations/hermes/tests/test_installer_gate_d1.py
"""
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALLER = HERE.parent / "install-perseus-vault.py"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}   {name}")
    if detail and not ok:
        print(f"        -> {detail}")


# ---------------------------------------------------------------------
# environment helpers
# ---------------------------------------------------------------------

def snapshot(root: Path) -> dict:
    out = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = (
                hashlib.sha256(p.read_bytes()).hexdigest(),
                oct(p.stat().st_mode)[-3:])
    return out


def run_installer(home: Path, *, token="gate-test-token",
                  gate_root=None, timeout=40):
    """Run the real installer against an isolated HERMES_HOME.

    Post-write verification steps are slow by design; the gate must
    act BEFORE any of them. A timeout that left files behind counts as
    'the installer proceeded past the write point' (proceeded=True).
    """
    env = dict(os.environ)
    for leak in ("PERSEUS_VAULT_MCP_TOKEN", "MCP_PERSEUS_VAULT_API_KEY"):
        env.pop(leak, None)
    env["HERMES_HOME"] = str(home)
    env["PERSEUS_VAULT_URL"] = "http://127.0.0.1:1/message"
    if token is not None:
        env["PERSEUS_VAULT_MCP_TOKEN"] = token
    if gate_root is not None:
        env["PERSEUS_GATE_HOST_ROOT"] = str(gate_root)
    try:
        r = subprocess.run([sys.executable, str(INSTALLER)], env=env,
                           capture_output=True, text=True, timeout=timeout)
        r.proceeded_past_write = None
        return r
    except subprocess.TimeoutExpired as t:
        class R:
            returncode = 99
            stdout = (t.stdout or b"").decode() if isinstance(t.stdout, bytes) else (t.stdout or "")
            stderr = (t.stderr or b"").decode() if isinstance(t.stderr, bytes) else (t.stderr or "")
            proceeded_past_write = True
        return R()


# ---------------------------------------------------------------------
# stub host builder (exact shapes the gate must distinguish)
# ---------------------------------------------------------------------

OSS_FULL = ('    def on_session_switch(self, new_session_id, *,\n'
            '                     parent_session_id="", reset=False,\n'
            '                     rewound=False, **kwargs):\n'
            '        pass\n')
OSS_KWARGS = ('    def on_session_switch(self, new_session_id, **kwargs):\n'
              '        pass\n')


def make_stub(root: Path, *, version="0.23.1", sct=True, oss="full",
              mm_home=True, mm_exists=True):
    (root / "hermes_cli").mkdir(parents=True, exist_ok=True)
    (root / "hermes_cli" / "__init__.py").write_text(
        ('' if version is None else f'__version__ = "{version}"\n'))
    ag = root / "agent"
    ag.mkdir(exist_ok=True)
    (ag / "__init__.py").write_text("")
    mp = ['import contextvars, threading\n']
    if sct:
        mp.append('def spawn_context_thread(target, *, name, daemon=True, '
                  'args=(), kwargs=None):\n    return threading.Thread(\n'
                  '        target=contextvars.copy_context().run,\n'
                  '        args=(target,) + tuple(args), kwargs=kwargs or {},\n'
                  '        name=name, daemon=daemon)\n')
    mp.append('class MemoryProvider:\n')
    if oss == "full":
        mp.append(OSS_FULL)
    elif oss == "kwargs":
        mp.append(OSS_KWARGS)
    else:
        mp.append('    pass\n')
    (ag / "memory_provider.py").write_text("".join(mp))
    if mm_exists:
        body = ('    def initialize_all(self, session_id, **kwargs):\n'
                + ('        kwargs["hermes_home"] = "/stub"\n' if mm_home
                   else '        kwargs["other"] = "/stub"\n')
                + '        for p in self._providers:\n'
                  '            p.initialize(session_id=session_id, **kwargs)\n')
        (ag / "memory_manager.py").write_text(
            "class MemoryManager:\n" + body)
    return root


# ---------------------------------------------------------------------
# F0 baseline guard: with a COMPLETE stub host the installer proceeds
# and writes (it must before and after the gate exists).
# ---------------------------------------------------------------------

def f0(tmp):
    home = tmp / "f0-home"
    home.mkdir()
    stub = make_stub(tmp / "f0-host")
    r = run_installer(home, gate_root=stub)
    if not snapshot(home):
        return False, f"installer wrote nothing even with a full host (output tail: {r.stdout[-200:]})"
    return True, ""


# ---------------------------------------------------------------------
# F1-F5: incomplete / inconsistent hosts -> abort, zero filesystem change
# ---------------------------------------------------------------------

def expect_abort(tmp, label, stub_kwargs, want_in_msg):
    home = tmp / f"{label}-home"
    home.mkdir()
    stub = make_stub(tmp / f"{label}-host", **stub_kwargs)
    before = snapshot(home)
    r = run_installer(home, gate_root=stub)
    after = snapshot(home)
    problems = []
    if r.returncode == 0:
        problems.append("installer completed despite broken host")
    if after != before:
        problems.append(f"filesystem changed: {sorted(set(after) - set(before))[:3]}")
    out = r.stdout + r.stderr
    if want_in_msg and want_in_msg.lower() not in out.lower():
        problems.append(f"no explicit gate reason in output (looked for {want_in_msg!r})")
    return (not problems), "; ".join(problems)


def f_suite(tmp):
    yield ("F1 no spawn_context_thread -> abort, zero writes",
           lambda: expect_abort(tmp, "f1", {"sct": False},
                                "spawn_context_thread"))
    yield ("F2 no on_session_switch -> abort, zero writes",
           lambda: expect_abort(tmp, "f2", {"oss": "none"},
                                "on_session_switch"))
    yield ("F3 rewound absorbed by **kwargs -> reject",
           lambda: expect_abort(tmp, "f3", {"oss": "kwargs"},
                                "explicit"))
    yield ("F4 initialize_all without hermes_home -> reject",
           lambda: expect_abort(tmp, "f4", {"mm_home": False},
                                "hermes_home"))
    for i, (v, want) in enumerate([
            ("0.22.0", "0.22.0"), ("0.24.0", "0.24.0"),
            ("0.23.1 (custom)", "not unambiguously"),
            (None, "not unambiguously")]):
        yield (f"F5 version {v!r} -> abort",
               (lambda v=v, want=want, n=i:
                expect_abort(tmp, f"f5-{n}", {"version": v}, want)))


# ---------------------------------------------------------------------
# F6: full host passes the gate; existing controls still enforced
# ---------------------------------------------------------------------

def f6a(tmp):
    # full stub + NO token: must still abort; the INSTALLER must write
    # none of its artifacts. (The host CLI itself may bootstrap its
    # own home files when queried - that is host behavior, not the
    # installer's, and is excluded here; the gate-abort cases F1-F5/F7
    # enforce the full zero-change snapshot instead.)
    home = tmp / "f6a-home"
    home.mkdir()
    stub = make_stub(tmp / "f6a-host")
    before = snapshot(home)
    r = run_installer(home, token=None, gate_root=stub)
    after = snapshot(home)
    artifacts = [k for k in after
                 if k == ".env" or k == "perseus-vault.json"
                 or k.startswith("plugins/")
                 or k == "config.yaml" or k.startswith("config.yaml")]
    if r.returncode == 0:
        return False, "missing token accepted"
    if artifacts:
        return False, f"installer artifacts written without token: {artifacts[:3]}"
    return True, ""


def f6b():
    # gate function against the REAL installed host (read-only)
    code = (
        "import importlib.util, sys;"
        f"spec = importlib.util.spec_from_file_location('inst', {str(INSTALLER)!r});"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "v = m.check_host_contract();"
        "print('GATE_OK', v)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=180)
    if r.returncode != 0 or "GATE_OK" not in r.stdout:
        return False, (r.stdout[-200:] + r.stderr[-400:])
    if "0.23.1" not in r.stdout:
        return False, f"unexpected version: {r.stdout}"
    return True, ""


# ---------------------------------------------------------------------
# F7: ordering is real, not textual: with a broken host AND a valid
# token, the gate must be the FIRST failure reported (before prompts).
# ---------------------------------------------------------------------

def f7(tmp):
    home = tmp / "f7-home"
    home.mkdir()
    stub = make_stub(tmp / "f7-host", sct=False)
    r = run_installer(home, token="valid-looking-token", gate_root=stub)
    out = (r.stdout + r.stderr).lower()
    problems = []
    if r.returncode == 0:
        problems.append("no abort despite broken host")
    if "contract" not in out and "gate" not in out:
        problems.append("gate failure not reported first")
    if snapshot(home):
        problems.append("files written despite gate failure")
    return (not problems), "; ".join(problems)


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ok, det = f0(tmp)
        check("F0 full stub: installer writes (baseline)", ok, det)
        for name, fn in f_suite(tmp):
            try:
                ok, det = fn()
            except Exception as e:
                ok, det = False, f"unexpected: {type(e).__name__}: {e}"
            check(name, ok, det)
        ok, det = f6a(tmp)
        check("F6a full host + no token -> zero writes (controls kept)",
              ok, det)
        ok, det = f7(tmp)
        check("F7 broken host + token: gate error first, zero writes",
              ok, det)
    try:
        ok, det = f6b()
    except Exception as e:
        ok, det = False, f"unexpected: {type(e).__name__}: {e}"
    check("F6b gate against REAL installed host -> pass", ok, det)
    n = sum(1 for _, ok in results if ok)
    print(f"\n{n}/{len(results)} PASS")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
