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
import stat
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
    """Full-tree capture: regular files (content + mode), directories
    (mode), symlinks (target + mode), other entries (mode). Used to
    assert the absence of side effects; it must also be sensitive to
    chmod, symlink retargeting and new/removed directories."""
    out = {}
    if not root.exists():
        return out
    # review 9: the root itself is part of what must not change, and
    # full permission bits (incl. setuid/setgid/sticky) are kept:
    # S_IMODE, not oct(...)[-3:].
    out[""] = ("ROOT", stat.S_IMODE(root.stat().st_mode))

    def walk(d: Path):
        for e in os.scandir(d):
            rel = str(Path(e.path).relative_to(root))
            mode = stat.S_IMODE(e.stat(follow_symlinks=False).st_mode)
            if e.is_symlink():
                out[rel] = ("LINK", os.readlink(e.path), mode)
            elif e.is_dir(follow_symlinks=False):
                out[rel] = ("DIR", mode)
                walk(Path(e.path))
            elif e.is_file(follow_symlinks=False):
                out[rel] = ("FILE", hashlib.sha256(
                    Path(e.path).read_bytes()).hexdigest(), mode)
            else:
                out[rel] = ("SPECIAL", mode)
    walk(root)
    return out


def run_installer(home: Path, *, token="gate-test-token",
                  gate_root=None, path_prepend=None, timeout=40):
    """Run the real installer against an isolated HERMES_HOME.

    Post-write verification steps are slow by design; the gate must
    act BEFORE any of them. A timeout that left files behind counts as
    'the installer proceeded past the write point' (proceeded=True).
    """
    env = dict(os.environ)
    for leak in ("PERSEUS_VAULT_MCP_TOKEN", "MCP_PERSEUS_VAULT_API_KEY",
                 "PERSEUS_GATE_HOST_ROOT"):
        env.pop(leak, None)
    env["HERMES_HOME"] = str(home)
    env["PERSEUS_VAULT_URL"] = "http://127.0.0.1:1/message"
    if path_prepend:
        env["PATH"] = str(path_prepend) + os.pathsep + env.get("PATH", "")
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
            # review 9: a timeout is NOT evidence about the gate: it
            # is flagged explicitly so every rejection test fails on
            # it. Whether it passed the write point is unknown here;
            # only the filesystem snapshot can show actual writes.
            returncode = 99
            timed_out = True
            stdout = (t.stdout or b"").decode() if isinstance(t.stdout, bytes) else (t.stdout or "")
            stderr = (t.stderr or b"").decode() if isinstance(t.stderr, bytes) else (t.stderr or "")
            proceeded_past_write = None
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
              mm_home=True, mm_exists=True, mm_mode=None):
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
        if mm_mode is None:
            mm_mode = "real" if mm_home else "wrongkey"
        helper = ('    def _each_provider(self, label, call):\n'
                  '        for provider in self._providers:\n'
                  '            try:\n'
                  '                call(provider)\n'
                  '            except Exception:\n'
                  '                pass\n')
        helper_store = ('    def _each_provider(self, label, call):\n'
                        '        self._pending = call\n')
        guard = ('        if "hermes_home" not in kwargs:\n'
                 '            from hermes_constants_stub import get_hermes_home\n'
                 '            kwargs["hermes_home"] = "/stub"\n')
        call_cb = ('        self._each_provider("initialize failed",\n'
                   '            lambda p: p.initialize(session_id=session_id,\n'
                   '                                  **kwargs))\n')
        if mm_mode == "real":
            # exact Hermes 0.23.1 shape (positive control)
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb) + helper
        elif mm_mode == "wrongkey":
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    '        if "hermes_dir" not in kwargs:\n'
                    '            kwargs["hermes_dir"] = "/stub"\n'
                    + call_cb) + helper
        elif mm_mode == "unused":
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    '        cfg = {"hermes_home": "/stub"}\n'
                    '        for p in self._providers:\n'
                    '            p.initialize(session_id=session_id, **cfg)\n')
        elif mm_mode == "lateassign":
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    '        cfg = {}\n'
                    '        for p in self._providers:\n'
                    '            p.initialize(session_id=session_id, **cfg)\n'
                    '        cfg["hermes_home"] = "/stub"\n')
        elif mm_mode == "nestedfn":
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    '        cfg = {}\n'
                    '        def prepare():\n'
                    '            cfg["hermes_home"] = "/stub"\n'
                    '        for p in self._providers:\n'
                    '            p.initialize(session_id=session_id, **cfg)\n')
        elif mm_mode == "iffalse":
            # negative-table row 1: assignment under `if False`
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    '        if False:\n'
                    '            kwargs["hermes_home"] = "/stub"\n'
                    '        for p in self._providers:\n'
                    '            p.initialize(session_id=session_id, **kwargs)\n')
        elif mm_mode == "condflag":
            # negative-table row 2: assignment in an unguaranteed
            # conditional branch (guard is not key-membership)
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    '        if self.inject_home:\n'
                    '            kwargs["hermes_home"] = "/stub"\n'
                    + call_cb) + helper
        elif mm_mode == "loopout":
            # negative-table row 3: assignment inside a loop, call
            # outside the loop (loop may not execute)
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    '        for _ in ():\n'
                    '            kwargs["hermes_home"] = "/stub"\n'
                    + call_cb) + helper
        elif mm_mode == "reassign":
            # negative-table row 4: dict rebound without the key
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + '        kwargs = {}\n' + call_cb) + helper
        elif mm_mode == "deletion":
            # negative-table row 5: del kwargs[key] before the call
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + '        del kwargs["hermes_home"]\n'
                    + call_cb) + helper
        elif mm_mode == "lambdastored":
            # negative-table row 6: lambda stored, never passed to
            # _each_provider
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard
                    + '        cb = lambda p: p.initialize(session_id'
                    '=session_id, **kwargs)\n'
                    '        self._add_provider_cb(cb)\n') + helper
        elif mm_mode == "badsync":
            # helper exists but STORES the callback instead of
            # invoking it synchronously
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb) + helper_store
        elif mm_mode == "guardlate":
            # negative (review 4, defect 1): guard AFTER the helper call
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + call_cb + guard) + helper
        elif mm_mode == "helperdef":
            # negative (defect 2): helper call inside a nested def
            # that is never invoked
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard
                    + '        def deferred():\n'
                    '            self._each_provider("initialize failed",\n'
                    '                lambda p: p.initialize(\n'
                    '                    session_id=session_id, **kwargs))\n') + helper
        elif mm_mode == "helperiffalse":
            # negative (defect 2): helper call under `if False`
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard
                    + '        if False:\n'
                    '            self._each_provider("initialize failed",\n'
                    '                lambda p: p.initialize(\n'
                    '                    session_id=session_id, **kwargs))\n') + helper
        elif mm_mode == "otherrecv":
            # negative (defect 3): initialize() received by something
            # that is not the lambda parameter
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard
                    + '        self._each_provider("initialize failed",\n'
                    '            lambda p: other.initialize(\n'
                    '                session_id=session_id, **kwargs))\n') + helper
        elif mm_mode == "twocalls":
            # negative (defect 3): two initialize() calls in the
            # callback; only one unpacks kwargs
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard
                    + '        self._each_provider("initialize failed",\n'
                    '            lambda p: (p.initialize(session_id=session_id,\n'
                    '                                   **kwargs),\n'
                    '                       p.initialize(session_id="second")))\n') + helper
        elif mm_mode == "delitem":
            # negative (defect 5): entry removed via __delitem__
            # after the guard, before the call
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard
                    + '        kwargs.__delitem__("hermes_home")\n'
                    + call_cb) + helper
        elif mm_mode == "setdefault":
            # negative (review 5, point 1): mutating call disguised
            # as a read
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard
                    + '        kwargs.setdefault("hermes_home", "/otro")\n'
                    + call_cb) + helper
        elif mm_mode == "alias":
            # negative (point 1): mutation through an alias of kwargs
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard
                    + '        cfg = kwargs\n'
                    '        cfg.clear()\n'
                    + call_cb) + helper
        elif mm_mode == "helpercond":
            # negative (point 2): callback under `if False` inside
            # the helper loop
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb
                    + '    def _each_provider(self, label, call):\n'
                      '        for provider in self._providers:\n'
                      '            if False:\n'
                      '                call(provider)\n')
        elif mm_mode == "helpercondvar":
            # negative (point 2): callback under a variable condition
            # inside the helper loop
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb
                    + '    def _each_provider(self, label, call):\n'
                      '        for provider in self._providers:\n'
                      '            if self.enabled:\n'
                      '                call(provider)\n')
        elif mm_mode == "dualhelper":
            # negative (point 3): two definitions of _each_provider;
            # the second (non-synchronous) would win in the class
            # namespace
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb) + helper + helper_store
        elif mm_mode == "deferred_helper":
            # negative (review 6, point 2): the helper call lives
            # inside a lambda assigned to a name; it is never invoked.
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard
                    + '        deferred = lambda: self._each_provider(\n'
                      '            "initialize failed",\n'
                      '            lambda p: p.initialize(\n'
                      '                session_id=session_id, **kwargs))\n'
                    ) + helper
        elif mm_mode == "deferred_cb":
            # negative (review 6, point 1): the callback call lives
            # inside a lambda assigned inside the loop; never invoked.
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb
                    + '    def _each_provider(self, label, call):\n'
                      '        for provider in self._providers:\n'
                      '            deferred = lambda: call(provider)\n')
        elif mm_mode == "ifexp_cb":
            # negative (review 6, point 1): callback call under a
            # conditional expression; the branch is not executed.
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb
                    + '    def _each_provider(self, label, call):\n'
                      '        for provider in self._providers:\n'
                      '            result = call(provider) if False else None\n')
        elif mm_mode == "dualinit":
            # negative (review 6, point 3): two initialize_all
            # definitions; the second would win in the class namespace
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb
                    + '    def initialize_all(self, **kwargs):\n'
                      '        pass\n') + helper
        elif mm_mode == "asyncinit":
            # negative (review 6, point 3): async initialize_all is
            # not the executed synchronous shape
            body = ('    async def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb) + helper
        elif mm_mode == "init_lambda":
            # negative (review 7): the only initialize() lives inside
            # a nested lambda returned by the callback; never invoked.
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard
                    + '        self._each_provider("initialize failed",\n'
                      '            lambda p: (lambda: p.initialize(\n'
                      '                session_id=session_id, **kwargs)))\n'
                    ) + helper
        elif mm_mode == "init_ifexp":
            # negative (review 7): initialize() under a conditional
            # expression inside the callback; the branch never runs.
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard
                    + '        self._each_provider("initialize failed",\n'
                      '            lambda p: (p.initialize(\n'
                      '                session_id=session_id, **kwargs)\n'
                      '                if False else None))\n'
                    ) + helper
        elif mm_mode == "loop_empty":
            # negative (review 8, point 1): loop over an empty tuple;
            # the direct call position is real but never iterates.
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb
                    + '    def _each_provider(self, label, call):\n'
                      '        for provider in ():\n'
                      '            call(provider)\n')
        elif mm_mode == "loop_after_return":
            # negative (review 8, point 1): loop after an
            # unconditional return; unreachable.
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb
                    + '    def _each_provider(self, label, call):\n'
                      '        return []\n'
                      '        for provider in self._providers:\n'
                      '            call(provider)\n')
        elif mm_mode == "loop_foreign":
            # negative (review 8, point 1): loop over an iterable
            # that is not the providers list.
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb
                    + '    def _each_provider(self, label, call):\n'
                      '        for provider in self._others:\n'
                      '            call(provider)\n')
        elif mm_mode == "return_before_guard":
            # negative (review 8, point 2): unconditional return
            # before the guard; neither guard nor call executes.
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    '        return\n'
                    + guard + call_cb) + helper
        elif mm_mode == "return_between":
            # negative (review 8, point 2): return between the guard
            # and the helper call.
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + '        return\n' + call_cb) + helper
        elif mm_mode == "raise_before_guard":
            # negative (review 9, point 3): raise before the guard:
            # equivalent unreachability to return_before_guard
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    '        raise RuntimeError("no")\n'
                    + guard + call_cb) + helper
        elif mm_mode == "raise_between":
            # negative: raise between guard and helper call
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + '        raise RuntimeError("stop")\n'
                    + call_cb) + helper
        elif mm_mode == "raise_before_loop":
            # negative: raise before the loop in the helper body
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb
                    + '    def _each_provider(self, label, call):\n'
                      '        raise RuntimeError("stop")\n'
                      '        for provider in self._providers:\n'
                      '            call(provider)\n')
        elif mm_mode == "iter_badcond":
            # negative (review 10, G4-B2): IfExp with an unverified
            # condition and a foreign else-branch.
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb
                    + '    def _each_provider(self, label, call):\n'
                      '        for provider in (self._providers\n'
                      '                          if False\n'
                      '                          else empty_providers):\n'
                      '            call(provider)\n')
        elif mm_mode == "loop_return":
            # negative (G4-B1): return inside the loop body before
            # the call.
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb
                    + '    def _each_provider(self, label, call):\n'
                      '        for provider in self._providers:\n'
                      '            return []\n'
                      '            call(provider)\n')
        elif mm_mode == "try_return":
            # negative (G4-B1): return at the start of a try body:
            # the function exits; the call after the try never runs.
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb
                    + '    def _each_provider(self, label, call):\n'
                      '        for provider in self._providers:\n'
                      '            try:\n'
                      '                return []\n'
                      '            except Exception:\n'
                      '                pass\n'
                      '            call(provider)\n')
        elif mm_mode == "loop_rebind":
            # negative (G4-B3): the loop variable is rebound before
            # the call: call(provider) no longer denotes a provider.
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + call_cb
                    + '    def _each_provider(self, label, call):\n'
                      '        for provider in self._providers:\n'
                      '            provider = None\n'
                      '            call(provider)\n')
        elif mm_mode == "store_clear":
            # negative (G4-B4): the guard store's VALUE mutates
            # kwargs (clear() empties the dict).
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    '        if "hermes_home" not in kwargs:\n'
                    '            kwargs["hermes_home"] = kwargs.clear()\n'
                    + call_cb) + helper
        elif mm_mode == "store_pop":
            # negative (G4-B4): the store's VALUE pops from kwargs.
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    '        if "hermes_home" not in kwargs:\n'
                    '            kwargs["hermes_home"] = kwargs.pop("x")\n'
                    + call_cb) + helper
        elif mm_mode == "nocall":
            body = ('    def initialize_all(self, session_id, **kwargs):\n'
                    + guard + '        self._ready = True\n') + helper
        else:
            raise ValueError(f"unknown mm_mode {mm_mode!r}")
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
    # review 10 (F-B1): snapshot() truthiness is not an assertion;
    # verify the concrete artifacts the installer must write.
    expect = [home / "plugins" / "perseus-vault" / "plugin.yaml",
              home / "plugins" / "perseus-vault" / "provider.py"]
    missing = [str(p) for p in expect if not p.is_file()]
    if missing:
        return False, (f"installer artifacts missing with a full host: "
                       f"{missing} (output tail: {r.stdout[-200:]})")
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
    if getattr(r, "timed_out", False):
        problems.append("TIMEOUT: not evidence that the gate aborted")
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


def _pyc_index(root: Path):
    idx = {}
    for p in root.rglob("*.pyc"):
        st = p.stat()
        idx[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return idx


def f6b():
    # gate function against the REAL installed host (read-only), and
    # the gate's imports must not touch the host's bytecode caches:
    # full .pyc index (size + mtime) identical across the run, with
    # PYTHONDONTWRITEBYTECODE absent from the probe environment.
    find_root = (
        "import importlib.util;"
        f"spec = importlib.util.spec_from_file_location('inst', {str(INSTALLER)!r});"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "print(m._discover_host_root())")
    env = dict(os.environ)
    env.pop("PERSEUS_GATE_HOST_ROOT", None)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    r0 = subprocess.run([sys.executable, "-c", find_root], env=env,
                        capture_output=True, text=True, timeout=120)
    if r0.returncode != 0:
        return False, f"host root lookup failed: {r0.stderr[-300:]}"
    root = Path(r0.stdout.strip())
    before = _pyc_index(root)
    code = (
        "import importlib.util, sys;"
        f"spec = importlib.util.spec_from_file_location('inst', {str(INSTALLER)!r});"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "v = m.check_host_contract();"
        "print('GATE_OK', v)")
    r = subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True, timeout=180)
    after = _pyc_index(root)
    if r.returncode != 0 or "GATE_OK" not in r.stdout:
        return False, (r.stdout[-200:] + r.stderr[-400:])
    if "0.23.1" not in r.stdout:
        return False, f"unexpected version: {r.stdout}"
    if before != after:
        new = sorted(set(after) - set(before))
        touched = sorted(k for k in set(before) & set(after)
                         if before[k] != after[k])
        return False, (f"gate touched host bytecode caches: new={new[:3]} "
                       f"modified={touched[:3]}")
    return True, ""


# ---------------------------------------------------------------------
# F7: ordering is real, not textual: with a broken host AND a valid
# token, the gate must be the FIRST failure reported (before prompts).
# ---------------------------------------------------------------------

def f7(tmp):
    home = tmp / "f7-home"
    home.mkdir()
    stub = make_stub(tmp / "f7-host", sct=False)
    # review 9: snapshot() now always carries the ROOT entry, so
    # emptiness is not the right assertion: compare against the
    # actual pre-run baseline of this same (empty) home.
    before = snapshot(home)
    r = run_installer(home, token="valid-looking-token", gate_root=stub)
    out = (r.stdout + r.stderr).lower()
    problems = []
    if getattr(r, "timed_out", False):
        problems.append("TIMEOUT: not evidence that the gate aborted")
    if r.returncode == 0:
        problems.append("no abort despite broken host")
    if "contract" not in out and "gate" not in out:
        problems.append("gate failure not reported first")
    if snapshot(home) != before:
        problems.append("files written despite gate failure")
    return (not problems), "; ".join(problems)


# ---------------------------------------------------------------------
# G4 semantic-propagation cases: one per defect class.
# Negatives must FAIL the gate even when the text mentions hermes_home
# and an initialize() call (the old substring check passed them).
# ---------------------------------------------------------------------

def g4_suite(tmp):
    yield ("G4a no initialize() call -> abort",
           lambda: expect_abort(tmp, "g4a", {"mm_mode": "nocall"},
                                "propagates"))
    yield ("G4b key never reaches the call -> abort",
           lambda: expect_abort(tmp, "g4b", {"mm_mode": "wrongkey"},
                                "propagates"))
    yield ("G4c local hermes_home not passed -> abort",
           lambda: expect_abort(tmp, "g4c", {"mm_mode": "unused"},
                                "propagates"))
    yield ("G4e assignment AFTER the call -> abort (execution order)",
           lambda: expect_abort(tmp, "g4e", {"mm_mode": "lateassign"},
                                "propagates"))
    yield ("G4f assignment in never-run nested def -> abort (scope)",
           lambda: expect_abort(tmp, "g4f", {"mm_mode": "nestedfn"},
                                "propagates"))
    # minimum negative table from the review:
    yield ("G4n1 assignment under if False -> abort",
           lambda: expect_abort(tmp, "g4n1", {"mm_mode": "iffalse"},
                                "propagates"))
    yield ("G4n2 unguaranteed conditional branch -> abort",
           lambda: expect_abort(tmp, "g4n2", {"mm_mode": "condflag"},
                                "propagates"))
    yield ("G4n3 loop-only assignment, call outside loop -> abort",
           lambda: expect_abort(tmp, "g4n3", {"mm_mode": "loopout"},
                                "propagates"))
    yield ("G4n4 dict rebound without the key -> abort",
           lambda: expect_abort(tmp, "g4n4", {"mm_mode": "reassign"},
                                "propagates"))
    yield ("G4n5 del kwargs[key] before the call -> abort",
           lambda: expect_abort(tmp, "g4n5", {"mm_mode": "deletion"},
                                "propagates"))
    yield ("G4n6 lambda stored, not executed by _each_provider -> abort",
           lambda: expect_abort(tmp, "g4n6", {"mm_mode": "lambdastored"},
                                "propagates"))
    yield ("G4n7 helper stores callback (not synchronous) -> abort",
           lambda: expect_abort(tmp, "g4n7", {"mm_mode": "badsync"},
                                "propagates"))
    # negative table from the fourth review (recognizer defects):
    yield ("G4n8 guard AFTER the helper call -> abort (ordering)",
           lambda: expect_abort(tmp, "g4n8", {"mm_mode": "guardlate"},
                                "propagates"))
    yield ("G4n9 helper call inside never-run nested def -> abort",
           lambda: expect_abort(tmp, "g4n9", {"mm_mode": "helperdef"},
                                "propagates"))
    yield ("G4n10 helper call under if False -> abort",
           lambda: expect_abort(tmp, "g4n10", {"mm_mode": "helperiffalse"},
                                "propagates"))
    yield ("G4n11 initialize() on foreign receiver -> abort",
           lambda: expect_abort(tmp, "g4n11", {"mm_mode": "otherrecv"},
                                "propagates"))
    yield ("G4n12 two initialize() calls in callback -> abort",
           lambda: expect_abort(tmp, "g4n12", {"mm_mode": "twocalls"},
                                "propagates"))
    yield ("G4n13 kwargs.__delitem__ after guard -> abort",
           lambda: expect_abort(tmp, "g4n13", {"mm_mode": "delitem"},
                                "propagates"))
    # negative table from the fifth review:
    yield ("G4n14 kwargs.setdefault -> abort (mutation, not read)",
           lambda: expect_abort(tmp, "g4n14", {"mm_mode": "setdefault"},
                                "propagates"))
    yield ("G4n15 alias of kwargs then clear() -> abort",
           lambda: expect_abort(tmp, "g4n15", {"mm_mode": "alias"},
                                "propagates"))
    yield ("G4n16 callback under if False inside helper loop -> abort",
           lambda: expect_abort(tmp, "g4n16", {"mm_mode": "helpercond"},
                                "propagates"))
    yield ("G4n17 callback under variable condition -> abort",
           lambda: expect_abort(tmp, "g4n17", {"mm_mode": "helpercondvar"},
                                "propagates"))
    yield ("G4n18 two _each_provider definitions -> abort",
           lambda: expect_abort(tmp, "g4n18", {"mm_mode": "dualhelper"},
                                "propagates"))
    # negative table from the sixth review:
    yield ("G4n19 helper call deferred inside an assigned lambda -> abort",
           lambda: expect_abort(tmp, "g4n19",
                                {"mm_mode": "deferred_helper"},
                                "propagates"))
    yield ("G4n20 callback call deferred in lambda inside loop -> abort",
           lambda: expect_abort(tmp, "g4n20", {"mm_mode": "deferred_cb"},
                                "propagates"))
    yield ("G4n21 callback call under conditional expression -> abort",
           lambda: expect_abort(tmp, "g4n21", {"mm_mode": "ifexp_cb"},
                                "propagates"))
    yield ("G4n22 two initialize_all definitions -> abort",
           lambda: expect_abort(tmp, "g4n22", {"mm_mode": "dualinit"},
                                "not unique"))
    yield ("G4n23 async initialize_all -> abort",
           lambda: expect_abort(tmp, "g4n23", {"mm_mode": "asyncinit"},
                                "not unique"))
    # negative table from the seventh review:
    yield ("G4n24 initialize() nested in a lambda returned by the callback -> abort",
           lambda: expect_abort(tmp, "g4n24", {"mm_mode": "init_lambda"},
                                "propagates"))
    yield ("G4n25 initialize() under IfExp inside the callback -> abort",
           lambda: expect_abort(tmp, "g4n25", {"mm_mode": "init_ifexp"},
                                "propagates"))
    # negative table from the eighth review:
    yield ("G4n26 helper loop over empty tuple -> abort",
           lambda: expect_abort(tmp, "g4n26", {"mm_mode": "loop_empty"},
                                "propagates"))
    yield ("G4n27 helper loop after return (unreachable) -> abort",
           lambda: expect_abort(tmp, "g4n27",
                                {"mm_mode": "loop_after_return"},
                                "propagates"))
    yield ("G4n28 helper loop over foreign iterable -> abort",
           lambda: expect_abort(tmp, "g4n28", {"mm_mode": "loop_foreign"},
                                "propagates"))
    yield ("G4n29 return before guard in initialize_all -> abort",
           lambda: expect_abort(tmp, "g4n29",
                                {"mm_mode": "return_before_guard"},
                                "propagates"))
    yield ("G4n30 return between guard and helper call -> abort",
           lambda: expect_abort(tmp, "g4n30", {"mm_mode": "return_between"},
                                "propagates"))
    # raise equivalents (review 9, point 3): same unreachability,
    # different terminator
    yield ("G4n31 raise before guard in initialize_all -> abort",
           lambda: expect_abort(tmp, "g4n31",
                                {"mm_mode": "raise_before_guard"},
                                "propagates"))
    yield ("G4n32 raise between guard and helper call -> abort",
           lambda: expect_abort(tmp, "g4n32",
                                {"mm_mode": "raise_between"},
                                "propagates"))
    yield ("G4n33 raise before loop in helper -> abort",
           lambda: expect_abort(tmp, "g4n33",
                                {"mm_mode": "raise_before_loop"},
                                "propagates"))
    # negative table from the tenth review (G4-B1..B4):
    yield ("G4n34 iter IfExp with unverified condition -> abort",
           lambda: expect_abort(tmp, "g4n34", {"mm_mode": "iter_badcond"},
                                "propagates"))
    yield ("G4n35 return inside loop body before call -> abort",
           lambda: expect_abort(tmp, "g4n35", {"mm_mode": "loop_return"},
                                "propagates"))
    yield ("G4n36 return at start of try body -> abort",
           lambda: expect_abort(tmp, "g4n36", {"mm_mode": "try_return"},
                                "propagates"))
    yield ("G4n37 loop variable rebound before call -> abort",
           lambda: expect_abort(tmp, "g4n37", {"mm_mode": "loop_rebind"},
                                "propagates"))
    yield ("G4n38 guard store RHS kwargs.clear() -> abort",
           lambda: expect_abort(tmp, "g4n38", {"mm_mode": "store_clear"},
                                "propagates"))
    yield ("G4n39 guard store RHS kwargs.pop() -> abort",
           lambda: expect_abort(tmp, "g4n39", {"mm_mode": "store_pop"},
                                "propagates"))


def g4_positive(tmp, mode, label_home):
    # positive control: the EXACT Hermes 0.23.1 structure must be
    # accepted (stub mirror); the real installed host is verified
    # separately by F6b.
    home = tmp / f"{label_home}-home"
    home.mkdir()
    stub = make_stub(tmp / f"{label_home}-host", mm_mode=mode)
    r = run_installer(home, gate_root=stub)
    # review 10 (F-B1): passing the gate is demonstrated by the
    # installation proceeding and writing its artifacts, not by a
    # non-empty snapshot dict (which always carries the ROOT entry).
    missing = [str(p) for p in
               (home / "plugins" / "perseus-vault" / "plugin.yaml",
                home / "plugins" / "perseus-vault" / "provider.py")
               if not p.is_file()]
    if missing:
        return False, (f"gate rejected real-shape mirror {mode}: "
                       f"artifacts missing {missing} "
                       f"(tail: {(r.stdout + r.stderr)[-200:]})")
    return True, ""


def g4d(tmp):
    return g4_positive(tmp, "real", "g4h")


# ---------------------------------------------------------------------
# L: launcher discovery determinism. Pure gate probes (no installer
# side effects): PATH is prepended with a controlled chain directory.
# ---------------------------------------------------------------------

def gate_probe(path_prepend=None, timeout=120):
    code = (
        "import importlib.util, sys;"
        f"spec = importlib.util.spec_from_file_location('inst', {str(INSTALLER)!r});"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "d = m._discover_host_root();"
        "v = m.check_host_contract();"
        "print('GATE_OK', v, str(d))")
    env = dict(os.environ)
    env.pop("PERSEUS_GATE_HOST_ROOT", None)
    if path_prepend:
        env["PATH"] = str(path_prepend) + os.pathsep + env.get("PATH", "")
    return subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=timeout)


def l1(tmp):
    # 3-hop chain with RELATIVE exec targets: each target must resolve
    # against the directory of the script being read (p.parent). The
    # third hop lives under bin/sub/deeper, NOT under bin/ - resolving
    # from the entry wrapper's directory would miss it.
    host = make_stub(tmp / "l1-host")
    chain = tmp / "l1-chain"
    (chain / "bin" / "sub" / "deeper").mkdir(parents=True)
    hops = {chain / "bin" / "hermes": '#!/bin/sh\nexec sub/l2 "$@"\n',
            chain / "bin" / "sub" / "l2": '#!/bin/sh\nexec deeper/l3 "$@"\n'}
    for p, txt in hops.items():
        p.write_text(txt)
        p.chmod(0o755)
    l3f = chain / "bin" / "sub" / "deeper" / "l3"
    l3f.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        f"sys.path.insert(0, {str(host)!r})\nprint('stub')\n")
    l3f.chmod(0o755)
    r = gate_probe(path_prepend=chain / "bin")
    out = r.stdout + r.stderr
    if f"GATE_OK 0.23.1 {host}" not in r.stdout:
        return False, f"chain not resolved via p.parent to the stub: {out[-250:]}"
    return True, ""


def l2(tmp):
    # quoted exec target (shell layer only; the Python path literal
    # is clean here, and the quote-joined literal gets its own case
    # in L4, per review 9: one property per test)
    host = make_stub(tmp / "l2-host")
    chain = tmp / "l2-chain"
    (chain / "bin" / "inner").mkdir(parents=True)
    w1 = chain / "bin" / "hermes"
    w1.write_text("#!/bin/sh\nexec 'inner/l2' \"$@\"\n")
    w1.chmod(0o755)
    inner = chain / "bin" / "inner" / "l2"
    inner.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        f"sys.path.insert(0, {str(host)!r})\nprint('stub')\n")
    inner.chmod(0o755)
    r = gate_probe(path_prepend=chain / "bin")
    if f"GATE_OK 0.23.1 {host}" not in r.stdout:
        return False, f"quoted exec/quote-join not resolved to the stub: {(r.stdout + r.stderr)[-250:]}"
    return True, ""


def l4(tmp):
    # review 9: separate case for the SHELL quote-joined sys.path
    # literal, exactly as the installed Hermes launcher writes it:
    # exec python3 -I -c '... sys.path.insert(0, '"'"'PATH'"'"') ...'
    # The discoverer must extract the bare path from that form.
    host = make_stub(tmp / "l4-host")
    chain = tmp / "l4-chain"
    (chain / "bin").mkdir(parents=True)
    w = chain / "bin" / "hermes"
    joined = "'\"'\"'" + str(host) + "'\"'\"'"
    # review 10 (F-B2): the whole program is ONE single-quoted -c
    # argument, exactly like the installed launcher; inner program
    # quotes avoided so the only quoting is the path's '"'"' join.
    prog = ("'import sys\nsys.path.insert(0, " + joined
            + ")\nprint(\"stub\")\n'")
    w.write_text("#!/bin/sh\nexec /usr/bin/python3 -I -c "
                 + prog + ' "$@"\n')
    w.chmod(0o755)
    r = gate_probe(path_prepend=chain / "bin")
    if f"GATE_OK 0.23.1 {host}" not in r.stdout:
        return False, f"quote-joined sys.path literal not resolved: {(r.stdout + r.stderr)[-250:]}"
    return True, ""


def l3(tmp):
    # bounded: a chain deeper than the cap aborts explicitly (no open
    # recursion, no heuristic picking of another installation)
    chain = tmp / "l3-chain"
    (chain / "bin").mkdir(parents=True)
    names = ["hermes"] + [f"w{i}" for i in range(2, 11)]
    for i, name in enumerate(names):
        nxt = names[i + 1] if i + 1 < len(names) else "dead-end"
        p = chain / "bin" / name
        p.write_text(f'#!/bin/sh\nexec {nxt} "$@"\n')
        p.chmod(0o755)
    r = gate_probe(path_prepend=chain / "bin")
    if r.returncode == 0:
        return False, "deep chain accepted"
    if "chain too deep" not in (r.stdout + r.stderr):
        return False, f"no explicit depth error: {(r.stdout + r.stderr)[-250:]}"
    return True, ""


def gro(tmp):
    # The gate's OWN validation must be read-only w.r.t. the host:
    # importing host modules must not create __pycache__/.pyc inside
    # the host root. Fresh stub host = no bytecode caches at all;
    # the probe runs with PYTHONDONTWRITEBYTECODE EXPLICITLY ABSENT,
    # so the guarantee must come from the gate itself.
    host = make_stub(tmp / "gro-host")
    probe = (
        "import os, importlib.util;"
        "assert 'PYTHONDONTWRITEBYTECODE' not in os.environ," \
        " 'external setting leaked';"
        f"spec = importlib.util.spec_from_file_location('inst', {str(INSTALLER)!r});"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "v = m.check_host_contract();"
        "print('GATE_OK', v)")
    env = dict(os.environ)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    env["PERSEUS_GATE_HOST_ROOT"] = str(host)
    before = snapshot(host)
    r = subprocess.run([sys.executable, "-c", probe], env=env,
                       capture_output=True, text=True, timeout=120)
    after = snapshot(host)
    if "GATE_OK" not in r.stdout:
        return False, f"gate failed on valid stub: {(r.stdout + r.stderr)[-250:]}"
    if after != before:
        new = sorted(set(after) - set(before))
        return False, f"gate wrote {len(new)} files into the host root: {new[:4]}"
    return True, ""


# ---------------------------------------------------------------------
# SN: snapshot-mechanism self-check (test machinery, not installer
# behavior): must be sensitive to chmod, symlink retargeting and new
# directories, and must record directories/links at all.
# ---------------------------------------------------------------------

def sn1(tmp):
    root = tmp / "sn1"
    root.mkdir()
    # Both targets exist BEFORE the first snapshot: the symlink
    # retarget check below then varies ONLY the link destination
    # (same content on both files, so content hashing alone cannot
    # be what detects the change).
    (root / "f.txt").write_text("a")
    (root / "same.txt").write_text("a")
    (root / "d").mkdir()
    (root / "link").symlink_to(root / "f.txt")
    s1 = snapshot(root)
    problems = []
    if not any(v[0] == "LINK" for v in s1.values()):
        problems.append("symlink not captured")
    if not any(v[0] == "DIR" for v in s1.values()):
        problems.append("directory not captured")
    (root / "f.txt").chmod(0o600)
    s2 = snapshot(root)
    if s2 == s1:
        problems.append("chmod invisible to snapshot")
    # only the symlink destination changes between s2 and s3
    (root / "link").unlink()
    (root / "link").symlink_to(root / "same.txt")
    s3 = snapshot(root)
    if s3 == s2:
        problems.append("symlink retarget invisible (identical content)")
    s4 = snapshot(root)
    (root / "newemptydir").mkdir()
    if snapshot(root) == s4:
        problems.append("new empty directory invisible")
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
        for name, fn in g4_suite(tmp):
            try:
                ok, det = fn()
            except Exception as e:
                ok, det = False, f"unexpected: {type(e).__name__}: {e}"
            check(name, ok, det)
        ok, det = g4d(tmp)
        check("G4h exact Hermes 0.23.1 real shape (mirror) -> accepted",
              ok, det)
        ok, det = gro(tmp)
        check("GRO gate imports write NO bytecode into the host root",
              ok, det)
        for name, fn in (("L1 relative exec chain resolves via p.parent", l1),
                         ("L2 quoted exec + quote-joined path literal", l2),
                         ("L3 chain deeper than cap -> explicit abort", l3),
                         ("L4 quote-joined sys.path literal (host form)", l4),
                         ("SN1 snapshot sensitivity (dir/link/chmod)", sn1)):
            try:
                ok, det = fn(tmp)
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
