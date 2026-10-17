"""Claude Code version warning and hiding cmux where it isn't installed."""

import asyncio
import json
import os
import subprocess

import pytest
from textual.widgets import SelectionList

from claude_launcher import cli, cmux, compat, profiles
from claude_launcher.catalog import discover
from claude_launcher.plan import Selection
from claude_launcher.tui import LauncherApp

REAL_INSTALLED_VERSION = compat.installed_version  # captured before the autouse stub replaces it


def test_parse_version():
    assert compat.parse_version("2.1.281 (Claude Code)\n") == "2.1.281"
    assert compat.parse_version("garbage") is None


def test_installed_version_uses_injected_runner():
    seen = {}

    def run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "9.0.1 (Claude Code)\n", "")

    real = REAL_INSTALLED_VERSION
    assert real("claude", run=run) == "9.0.1"
    assert seen["argv"] == ["claude", "--version"]
    assert real("claude", run=lambda a, **k: subprocess.CompletedProcess(a, 1, "", "")) is None

    def boom(argv, **kw):
        raise FileNotFoundError(argv[0])

    assert real("nope", run=boom) is None


def test_version_warning_text():
    assert compat.version_warning(compat.VERIFIED_VERSIONS[0]) is None
    w = compat.version_warning("9.9.9")
    assert "9.9.9" in w and "smoke_verify.py" in w
    assert "could not determine" in compat.version_warning(None)


def test_cli_warns_on_other_version(fake_home, monkeypatch, capsys):
    monkeypatch.setattr(compat, "installed_version", lambda b, run=None, timeout=5: "9.9.9")
    assert cli.main(["--claude-bin", "claude", "--list"]) == 0
    assert "Claude Code 9.9.9 differs" in capsys.readouterr().out
    assert cli.main(["--claude-bin", "claude", "--dry-run", "--mcp", "gdrive"]) == 0
    assert "smoke_verify.py" in capsys.readouterr().out
    assert cli.main(["--claude-bin", "claude", "--list", "--json"]) == 0
    assert any("9.9.9" in w for w in json.loads(capsys.readouterr().out)["warnings"])
    assert cli.main(["--claude-bin", "claude", "--where"]) == 0
    assert "Claude Code 9.9.9 differs" in capsys.readouterr().out


def test_cli_quiet_on_verified_version(fake_home, capsys):
    assert cli.main(["--claude-bin", "claude", "--list"]) == 0
    assert "differs" not in capsys.readouterr().out


# ---------------------------------------------------------------- cmux hidden


@pytest.fixture
def no_cmux(fake_home, monkeypatch):
    monkeypatch.setenv("CM_CMUX_WRAPPER", str(fake_home.tmp / "absent"))
    monkeypatch.delenv("CMUX_BUNDLED_CLI_PATH", raising=False)
    monkeypatch.setattr(cmux, "DEFAULT_APP_BIN", fake_home.tmp / "no-app")
    return fake_home


def test_probe_reports_not_installed(no_cmux):
    st = cmux.probe(dict(os.environ))
    assert not st.installed and not st.available


def test_installed_but_broken_is_still_shown(fake_home):
    fake_home.wrapper.write_text("#!/bin/sh\n# no template\n")
    st = cmux.probe(dict(os.environ))
    assert st.installed and not st.available


def test_list_hides_cmux_row(no_cmux, capsys):
    assert cli.main(["--claude-bin", "claude", "--list"]) == 0
    out = capsys.readouterr().out
    assert "cmux-cua" not in out and "unavailable" not in out
    assert cli.main(["--claude-bin", "claude", "--list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["cmux"]["installed"] is False


def test_list_shows_cmux_row_when_installed(fake_home, capsys):
    assert cli.main(["--claude-bin", "claude", "--list"]) == 0
    assert "cmux-cua" in capsys.readouterr().out


def test_profile_with_cmux_on_machine_without_cmux(no_cmux):
    cat = discover(no_cmux.project)
    sel, notes = profiles.reconcile(Selection(local=["gdrive"], cmux=True), cat)
    assert sel.cmux is False and notes == ["cmux is not installed here"]


def _drive(app, steps):
    async def go():
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            await steps(app, pilot)

    asyncio.run(go())
    return app.return_value


def test_tui_hides_cmux_group_and_still_works(no_cmux):
    cat = discover(no_cmux.project)
    app = LauncherApp(cat, Selection(cmux=True), claude_bin="claude")

    async def steps(app, pilot):
        assert app.query_one("#cmux", SelectionList).display is False
        labels = [str(w.render()) for w in app.query("Label") if w.display]
        assert "cmux computer use" not in labels
        local = app.query_one("#local", SelectionList)
        local.focus()
        local.highlighted = [s.name for s in cat.local].index("gdrive")
        await pilot.press("space")
        await pilot.press("ctrl+n")  # select none must not trip over the hidden group
        await pilot.press("space")
        await pilot.press("enter", "enter")  # the first Enter only asks for a session name

    result = _drive(app, steps)
    assert result[0] == "launch"
    assert result[1].local == ["gdrive"] and result[1].cmux is False


def test_tui_shows_cmux_group_when_installed(catalog):
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        assert app.query("#cmux")
        await pilot.press("escape")

    assert _drive(app, steps) is None


# ---------------------------------------------------------------- ${VAR} references (health check)


def test_expand_refs_matches_claude_semantics():
    from claude_launcher.health import expand_refs

    env = {"SET": "v", "EMPTY": ""}
    assert expand_refs("${SET}", env) == "v"
    assert expand_refs("${NOPE:-d}", env) == "d" and expand_refs("${EMPTY:-d}", env) == "d"
    assert expand_refs("${NOPE}", env) == "${NOPE}"  # left literal, like Claude Code 2.1.281
    assert expand_refs("${NOPE:-}", env) == ""


def test_agentmemory_url_resolves_references(monkeypatch):
    from claude_launcher.catalog import LocalServer
    from claude_launcher.health import agentmemory_url

    srv = LocalServer("agentmemory", {"command": "npx", "args": ["-y", "@agentmemory/mcp"],
                                      "env": {"AGENTMEMORY_URL": "${AGENTMEMORY_URL:-http://localhost:3111}"}}, "x")
    assert agentmemory_url(srv, {}) == "http://localhost:3111"
    assert agentmemory_url(srv, {"AGENTMEMORY_URL": "http://127.0.0.1:4000/"}) == "http://127.0.0.1:4000"
    other = LocalServer("gdrive", {"command": "npx", "args": ["gdrive"]}, "x")
    assert agentmemory_url(other, {}) is None


# ---------------------------------------------------------------- smoke script scenarios


def _smoke():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke_verify.py"
    spec = importlib.util.spec_from_file_location("smoke_verify", path)
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = mod  # dataclasses look their module up while the class is created
    spec.loader.exec_module(mod)
    return mod


def test_smoke_scenarios_come_from_the_catalog(catalog, fake_home):
    from pathlib import Path

    smoke = _smoke()
    built = {sc.name: sc.build(catalog) for sc in smoke.scenarios(fake_home.project, fake_home.home)}
    first = catalog.local[0].name
    assert built["none"] == Selection()
    assert built["first local server"].local == [first]
    hooked = next(s.name for s in catalog.local if s.settings_path)
    assert hooked == first and built["first local server with companion hooks"] is None  # already covered
    catalog.local.reverse()
    resorted = {sc.name: sc.build(catalog) for sc in smoke.scenarios(fake_home.project, fake_home.home)}
    assert resorted["first local server with companion hooks"].local == [hooked]
    catalog.local.reverse()
    assert built["two local servers"].local == [s.name for s in catalog.local[:2]]
    assert built["first claude.ai connector (deny-list)"].connectors == [
        next(c.name for c in catalog.connectors if c.verified)]
    assert built["all claude.ai connectors + cmux when available"].cmux is True
    assert built["first plugin only"].plugins == [catalog.plugins[0].id]
    src = Path(smoke.__file__).read_text()
    for personal in ("gdrive", "Todoist", "Slack", "prompt-tools", '"agentmemory"'):
        assert personal not in src


def test_smoke_scenarios_skip_missing_ingredients(no_cmux):
    from claude_launcher.catalog import Catalog

    smoke = _smoke()
    empty = Catalog(cwd=no_cmux.project)
    built = {sc.name: sc.build(empty) for sc in smoke.scenarios(no_cmux.project, no_cmux.home)}
    assert built["none"] == Selection()
    assert all(v is None for k, v in built.items() if "none" not in k)


def test_smoke_hook_expectation_counts_companion_hooks():
    smoke = _smoke()
    doc = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "a"}, {"command": "b"}]}],
                     "Stop": [{"hooks": [{"command": "c"}]}]}}
    assert smoke.session_start_hooks(doc) == 2 and smoke.session_start_hooks({}) == 0


# ---------------------------------------------------------------- review regressions


def test_tui_checks_claude_version_in_background(catalog, monkeypatch):
    monkeypatch.setattr(compat, "installed_version", lambda b, run=None, timeout=5: "9.9.9")
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert any("9.9.9" in str(n.message) for n in app._notifications)
        await pilot.press("escape")

    _drive(app, steps)


def test_smoke_only_keeps_both_baselines_and_discovers_once_per_cwd(fake_home, monkeypatch, capsys):
    import sys

    smoke = _smoke()
    calls = []
    real = smoke.discover
    monkeypatch.setattr(smoke, "discover", lambda cwd: calls.append(cwd) or real(cwd))
    monkeypatch.setattr(sys, "argv", ["smoke_verify.py", "--list-scenarios", "--only", "connector",
                                      "--project", str(fake_home.project), "--claude-bin", "claude"])
    assert smoke.main() == 0
    out = capsys.readouterr().out
    assert "- none:" in out and "- ~ cwd: none" in out and "- ~ cwd: first connector" in out
    assert "first local server" not in out
    assert sorted(calls) == sorted([fake_home.project, fake_home.home])


def test_smoke_hook_check_never_uses_its_own_count_as_baseline(catalog, fake_home, monkeypatch):
    from claude_launcher.probe import ProbeResult

    smoke = _smoke()
    slack = "claude.ai Slack"
    res = ProbeResult(returncode=0, model="haiku", servers=[(slack, "connected")],
                      tools=[smoke.prefix_for(catalog, slack) + "x"], hooks_started={"SessionStart": 3})
    monkeypatch.setattr(smoke.probe, "run", lambda *a, **k: res)
    conn = smoke.Scenario("first connector", fake_home.home, lambda c: None)
    baselines: dict[str, int] = {}
    assert smoke.run_one(conn, "claude", baselines, catalog, Selection(connectors=[slack])).baseline_hooks is None
    assert baselines == {}
    none = smoke.Scenario("~ cwd: none", fake_home.home, lambda c: None, baseline=True)
    smoke.run_one(none, "claude", baselines, catalog, Selection())
    assert baselines == {str(fake_home.home): 3}
    assert smoke.run_one(conn, "claude", baselines, catalog, Selection(connectors=[slack])).baseline_hooks == 3
