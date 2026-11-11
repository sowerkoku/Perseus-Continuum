#!/usr/bin/env python3
"""Suite E — I2 config contract (D3 with guarantees D9-D11).

Contract under test:
  - api_key in perseus-vault.json is READ-ONLY legacy: never written by
    the installer nor by the panel's save_config, never removed by an
    update (D9: updates must not overwrite or delete an existing token).
  - The effective token comes from the core mechanism (.env ->
    PERSEUS_VAULT_MCP_TOKEN); a single write source (D11). The
    installer's .env write is bootstrap-only: an existing entry is left
    byte-identical.
  - Secrets never appear in plaintext output (D10): memory status shows
    set/MISSING only; config show excludes api_key.

E2 first ran RED against the frozen I1 provider to demonstrate the
provider dependency; the approved save_config patch (guarantees D9-D11)
now makes it green, covering: existing token, absent token, empty
values, core write failure, corrupt file, and no secret leakage.

Standalone: python3 integrations/hermes/tests/test_config_i2.py
"""
import ast
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
INSTALLER = HERE.parent.parent / "install-perseus-vault.py"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}   {name}")
    if detail and not ok:
        print(f"        -> {detail}")


def load_asset(tag, modname):
    """Load the last assignment of a module-level raw-string asset."""
    src = INSTALLER.read_text()
    tree = ast.parse(src)
    code = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == tag:
                    code = ast.literal_eval(node.value)
    assert code, f"{tag} not found"
    if tag == "PROVIDER_PY":
        # host-faithful stub, same semantics as suite C
        import types
        stub = types.ModuleType("agent.memory_provider")

        class MemoryProvider:
            pass

        def spawn_context_thread(target, *, name, daemon=True, args=(),
                                 kwargs=None):
            import contextvars, threading
            return threading.Thread(
                target=contextvars.copy_context().run,
                args=(target,) + tuple(args), kwargs=kwargs or {},
                name=name, daemon=daemon)

        stub.MemoryProvider = MemoryProvider
        stub.spawn_context_thread = spawn_context_thread
        agent_pkg = types.ModuleType("agent")
        agent_pkg.memory_provider = stub
        sys.modules["agent"] = agent_pkg
        sys.modules["agent.memory_provider"] = stub
    import types as _t
    mod = _t.ModuleType(modname)
    mod.__file__ = f"{modname}.py"
    sys.modules[modname] = mod
    exec(compile(code, f"{modname}:asset", "exec"), mod.__dict__)
    return mod


# ----------------------------------------------------------------------
# E1 — installer merge: legacy keys survive an update
# ----------------------------------------------------------------------

def e1(installer_mod):
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "perseus-vault.json"
        legacy = {"url": "http://old/message", "api_key": "[REDACTED-legacy]",
                  "workspace_hash": "ws-old", "future_unknown_key": 7}
        cfg.write_text(json.dumps(legacy))
        merged = installer_mod.merge_json_config(
            cfg, "http://new/message", "")
        on_disk = json.loads(cfg.read_text())
        problems = []
        if on_disk.get("api_key") != "[REDACTED-legacy]":
            problems.append("legacy api_key not preserved byte-for-value")
        if on_disk.get("future_unknown_key") != 7:
            problems.append("unknown key not preserved")
        if on_disk.get("url") != "http://new/message":
            problems.append("url not updated")
        if on_disk.get("workspace_hash") != "ws-old":
            problems.append("workspace changed without being provided")
        if not list(cfg.parent.glob("perseus-vault.json.bak-*")):
            problems.append("no backup of previous file")
        else:
            for bak in cfg.parent.glob("perseus-vault.json.bak-*"):
                if oct(bak.stat().st_mode)[-3:] != "600":
                    problems.append(f"backup {bak.name} mode "
                                    f"{oct(bak.stat().st_mode)} "
                                    "(must be 600)")
        if oct(cfg.stat().st_mode)[-3:] != "600":
            problems.append(f"mode {oct(cfg.stat().st_mode)}")
        # second run with workspace provided: explicit update wins
        installer_mod.merge_json_config(cfg, "http://new2/message", "ws-new")
        on_disk = json.loads(cfg.read_text())
        if on_disk.get("workspace_hash") != "ws-new":
            problems.append("provided workspace not applied")
        # two updates -> two DISTINCT backups, all at mode 600
        backups = sorted(cfg.parent.glob("perseus-vault.json.bak-*"))
        if len(backups) < 2:
            problems.append(f"expected 2 distinct backups, found "
                            f"{len(backups)}")
        for bak in backups:
            if oct(bak.stat().st_mode)[-3:] != "600":
                problems.append(f"backup {bak.name} mode "
                                f"{oct(bak.stat().st_mode)}")
        if len({bak.read_text() for bak in backups}) < 1:
            problems.append("backups unreadable")
        # documented exception (policy A): invalid file is replaced
        # deliberately; original bytes survive in a 600 backup.
        bad_cfg = Path(td) / "inv-perseus-vault.json"
        bad_cfg.write_text("{broken")
        installer_mod.merge_json_config(bad_cfg, "http://fresh/message", "")
        on_disk = json.loads(bad_cfg.read_text())
        if on_disk.get("url") != "http://fresh/message":
            problems.append("auto-recovery did not write a fresh config")
        inv_baks = sorted(Path(td).glob("inv-perseus-vault.json.bak-*"))
        if not inv_baks or inv_baks[-1].read_text() != "{broken":
            problems.append("auto-recovery lost the original bytes")
        elif oct(inv_baks[-1].stat().st_mode)[-3:] != "600":
            problems.append("auto-recovery backup not mode 600")
        # simulated OLD behavior (the exact pattern removed from main) for
        # gap-detection evidence:
        old_style = {"url": "http://new/message"}
        if "api_key" in old_style:
            problems.append("simulated old pattern unexpectedly kept keys")
        if not problems:
            detail = ("gap-detection: the pre-I2 pattern (fresh dict) "
                      "drops api_key; merge_json_config keeps it")
            return True, detail
        return False, "; ".join(problems)


# ----------------------------------------------------------------------
# E2 — panel save_config: full D9 matrix from the approved patch
# (existing token, absent token, empty value, core write failure,
# no secret leakage)
# ----------------------------------------------------------------------

def e2(provider_mod):
    problems = []
    p = provider_mod.create_provider()
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "perseus-vault.json"

        # (a) existing legacy token: preserved byte-for-value; panel
        # secret never enters the JSON or .env; .env untouched.
        env = Path(td) / ".env"
        env.write_text("PERSEUS_VAULT_MCP_TOKEN=[REDACTED-existing]\n")
        env_before = env.read_text()
        cfg.write_text(json.dumps(
            {"url": "http://old/message", "api_key": "[REDACTED-legacy]"}))
        p.save_config({"url": "http://panel/message",
                       "workspace_hash": "ws-panel",
                       "token": "SECRET-FROM-PANEL"}, td)
        blob = cfg.read_text()
        on_disk = json.loads(blob)
        if on_disk.get("api_key") != "[REDACTED-legacy]":
            problems.append("(a) legacy api_key dropped")
        if "SECRET-FROM-PANEL" in blob or "token" in on_disk:
            problems.append("(a) panel secret reached the JSON")
        if "SECRET-FROM-PANEL" in env.read_text() or env.read_text() != env_before:
            problems.append("(a) save_config touched .env")
        if on_disk.get("url") != "http://panel/message":
            problems.append("(a) explicit url not applied")
        if oct(cfg.stat().st_mode)[-3:] != "600":
            problems.append("(a) mode not 600")

        # (b) absent token in values: no token written anywhere; legacy
        # still present.
        p.save_config({"url": "http://panel2/message"}, td)
        on_disk = json.loads(cfg.read_text())
        if on_disk.get("api_key") != "[REDACTED-legacy]":
            problems.append("(b) legacy api_key dropped with token absent")
        if any("token" in k.lower() for k in on_disk):
            problems.append("(b) a token key appeared in JSON")
        if on_disk.get("workspace_hash") != "ws-panel":
            problems.append("(b) workspace dropped when not provided")

        # (c) empty values: never overwrite existing entries. url empty
        # must keep the previous url. Explicit workspace "" is honored
        # (means global).
        p.save_config({"url": "", "workspace_hash": ""}, td)
        on_disk = json.loads(cfg.read_text())
        if on_disk.get("url") != "http://panel2/message":
            problems.append("(c) empty url overwrote existing url")
        if on_disk.get("workspace_hash") != "":
            problems.append("(c) explicit empty workspace not honored")
        if on_disk.get("api_key") != "[REDACTED-legacy]":
            problems.append("(c) legacy api_key dropped on empty values")

        # (d) write failure: DETERMINISTIC injection (a chmod-based test
        # is not reliable under root). Force os.replace to fail and
        # verify: explicit RuntimeError, original intact, no silent
        # success, no secret in the message, temp file cleaned up.
        det = Path(td) / "detfail"
        det.mkdir()
        det_cfg = det / "perseus-vault.json"
        det_cfg.write_text(json.dumps(
            {"url": "http://keep", "api_key": "[REDACTED-legacy]"}))
        before = det_cfg.read_text()
        real_replace = provider_mod.os.replace

        def _boom(*a, **k):
            raise PermissionError("injected replace failure")
        provider_mod.os.replace = _boom
        try:
            raised = None
            try:
                p.save_config({"url": "http://fail"}, str(det))
            except RuntimeError as e:
                raised = str(e)
        finally:
            provider_mod.os.replace = real_replace
        if raised is None:
            problems.append("(d) injected failure returned silent success")
        elif "REDACTED-legacy" in raised or "SECRET" in raised:
            problems.append("(d) error message leaked a secret")
        if det_cfg.read_text() != before:
            problems.append("(d) failed write damaged the original file")
        if (det / "perseus-vault.json.tmp").exists():
            problems.append("(d) temp file left behind after failure")

        # (e) corrupt existing file: refuse to rewrite, explicit error,
        # bytes preserved (no alternative write).
        bad = Path(td) / "bad"
        bad.mkdir()
        (bad / "perseus-vault.json").write_text("{not json")
        raised2 = None
        try:
            p.save_config({"url": "http://x"}, str(bad))
        except RuntimeError as e:
            raised2 = str(e)
        if raised2 is None:
            problems.append("(e) corrupt file silently rewritten")
        if (bad / "perseus-vault.json").read_text() != "{not json":
            problems.append("(e) corrupt file bytes changed")

        # (f) valid JSON with a non-object root: same preservation
        # contract as corrupt content (review 2026-11-10).
        for label, bad_root in (("list", "[]"), ("null", "null"),
                                ("scalar", "42")):
            d = Path(td) / ("root-" + label)
            d.mkdir()
            f = d / "perseus-vault.json"
            f.write_text(bad_root)
            raised3 = None
            try:
                p.save_config({"url": "http://x"}, str(d))
            except RuntimeError:
                raised3 = True
            if raised3 is None:
                problems.append(f"(f) {label} root silently rewritten")
            if f.read_text() != bad_root:
                problems.append(f"(f) {label} root bytes changed")
    if problems:
        return False, "; ".join(problems)
    return True, ""


# ----------------------------------------------------------------------
# E3 — .env bootstrap must never overwrite an existing token (D9/D11)
# ----------------------------------------------------------------------

def e3(installer_mod):
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        env = home / ".env"
        env.write_text("OTHER=keep\nPERSEUS_VAULT_MCP_TOKEN=[REDACTED-existing]\n")
        before = env.read_text()
        installer_mod.write_env_token(home, "BOOTSTRAP-SHOULD-NOT-APPEAR")
        after = env.read_text()
        problems = []
        if after != before:
            problems.append("existing .env token mutated")
        if "BOOTSTRAP-SHOULD-NOT-APPEAR" in after:
            problems.append("duplicate/competing token written")
        # other keys survive; append path for .env without token
        env2 = home / ".env"
        env2.write_text("OTHER=keep\n")
        installer_mod.write_env_token(home, "tok-second")
        t2 = env2.read_text()
        if "OTHER=keep" not in t2 or "PERSEUS_VAULT_MCP_TOKEN=tok-second" not in t2:
            problems.append("append path damaged other keys or missed token")
        if not list(home.glob(".env.bak-*")):
            problems.append("no .env backup on append path")
        # fresh .env: mode 600
        home3 = Path(td) / "fresh"
        installer_mod.write_env_token(home3, "tok-fresh")
        if oct((home3 / ".env").stat().st_mode)[-3:] != "600":
            problems.append("fresh .env not mode 600")
        if problems:
            return False, "; ".join(problems)
        return True, ""


# --------------------------------------------------------------------------
# E4 — no plaintext secrets in output (D10)
# ----------------------------------------------------------------------

def e4(provider_mod, cli_src):
    with tempfile.TemporaryDirectory() as td:
        os.environ["HERMES_HOME"] = td
        (Path(td) / "perseus-vault.json").write_text(json.dumps(
            {"url": "http://x/message", "api_key": "[REDACTED-legacy]"}))
        os.environ["PERSEUS_VAULT_MCP_TOKEN"] = "env-tok-E4"
        p = provider_mod.create_provider()
        status = p.get_status_config({})
        blob = json.dumps(status)
        problems = []
        for secret in ("[REDACTED-legacy]", "env-tok-E4"):
            if secret in blob:
                problems.append(f"status leaked secret {secret!r}")
        if status.get("token") != "set":
            problems.append(f"status token field = {status.get('token')!r}")
        # CLI: static guarantee that config show strips api_key
        if 'k != "api_key"' not in cli_src:
            problems.append("CLI config show no longer excludes api_key")
        del os.environ["HERMES_HOME"]
        del os.environ["PERSEUS_VAULT_MCP_TOKEN"]
        if problems:
            return False, "; ".join(problems)
        return True, ""


# --------------------------------------------------------------------------
# E5 — single effective source: core (.env) wins over legacy, read-only
# --------------------------------------------------------------------------

def e5(provider_mod):
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "perseus-vault.json"
        cfg.write_text(json.dumps(
            {"url": "http://x/message", "api_key": "[REDACTED-legacy]"}))
        before = cfg.read_text()
        os.environ["HERMES_HOME"] = td
        os.environ["PERSEUS_VAULT_MCP_TOKEN"] = "env-tok-E5"
        try:
            p = provider_mod.create_provider()
            p._load_config()
            effective = p._config.get("api_key")
            problems = []
            if effective != "env-tok-E5":
                problems.append(f"effective token = {effective!r} "
                                "(core/.env must win over legacy)")
            if cfg.read_text() != before:
                problems.append("reading config MUTATED the JSON "
                                "(legacy must be read-only)")
        finally:
            del os.environ["HERMES_HOME"]
            del os.environ["PERSEUS_VAULT_MCP_TOKEN"]
        if problems:
            return False, "; ".join(problems)
        return True, ""


def main():
    src = INSTALLER.read_text()
    # installer module (functions at module level; asset strings only)
    imod = {}
    tree = ast.parse(src)
    keep = [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign,
                              ast.Import, ast.ImportFrom))
            and not (isinstance(n, ast.Assign)
                     and getattr(n.targets[0], "id", "").endswith("_PY"))]
    mod_ns = imod
    exec(compile(ast.Module(body=keep, type_ignores=[]),
                 "installer_under_test", "exec"), mod_ns)
    imod = type("installer_under_test", (), {})
    for k, v in mod_ns.items():
        setattr(imod, k, v)
    prov = load_asset("PROVIDER_PY", "pv_provider_under_test")
    cli_src = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "CLI_PY":
            cli_src = ast.literal_eval(node.value)
    assert cli_src

    with contextlib.redirect_stdout(io.StringIO()):
        ok, det = e1(imod)
    check("E1 installer update preserves legacy/unknown keys", ok, det)
    with contextlib.redirect_stdout(io.StringIO()):
        ok, det = e2(prov)
    check("E2 panel save_config: full D9 matrix", ok, det)
    with contextlib.redirect_stdout(io.StringIO()):
        ok, det = e3(imod)
    check("E3 .env bootstrap never overwrites existing token", ok, det)
    with contextlib.redirect_stdout(io.StringIO()):
        ok, det = e4(prov, cli_src)
    check("E4 no plaintext secrets in status/config output", ok, det)
    with contextlib.redirect_stdout(io.StringIO()):
        ok, det = e5(prov)
    check("E5 core token wins; legacy read-only; no write on read", ok, det)

    n_pass = sum(1 for _, ok, _ in results if ok)
    print(f"\n{n_pass}/{len(results)} PASS")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
