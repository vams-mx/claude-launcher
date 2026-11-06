"""Installing plugins from cm: the marketplace catalogs (read-only), `claude plugin install`
(always mocked: no real process ever starts), `cm plugins` and the Ctrl+O screen."""

import json
import subprocess

import pytest
from textual.widgets import Button, Input, OptionList, SelectionList, Static

from claude_launcher import cli, marketplace
from claude_launcher.catalog import discover
from claude_launcher.plan import Selection
from claude_launcher.tui import InstallScreen, LauncherApp
from tests.conftest import _w
from tests.test_tui import drive
from tests.test_tui_sessions import settle


@pytest.fixture
def markets(fake_home):
    h = fake_home.home
    root = h / ".claude" / "plugins"
    _w(root / "known_marketplaces.json", {
        "official": {"source": {"source": "github"}, "installLocation": str(root / "marketplaces" / "official")},
        "gone": {"installLocation": str(root / "marketplaces" / "gone")},
        "odd": "not an object",
    })
    _w(root / "marketplaces" / "official" / ".claude-plugin" / "marketplace.json", {"name": "official", "plugins": [
        {"name": "commit-commands", "description": "Git   commit helpers", "category": "dev", "version": "1.0.0"},
        {"name": "playwright", "description": "Browser automation"},
        {"name": "bad name!", "description": "skipped"},
        "not a plugin",
        {"name": "playwright", "description": "a duplicate, skipped"},
    ]})
    # a marketplace folder known_marketplaces.json doesn't list, catalog at its root
    _w(root / "marketplaces" / "local-m" / "marketplace.json", {"plugins": [{"name": "notes"}]})
    _w(root / "marketplaces" / "broken" / ".claude-plugin" / "marketplace.json", "{nope")
    _w(root / "marketplaces" / "nolist" / ".claude-plugin" / "marketplace.json", {"plugins": {"x": 1}})
    return fake_home


def test_available_reads_catalogs_and_marks_installed(markets):
    warnings = []
    items = {x.id: x for x in marketplace.available(markets.project, warnings)}
    assert set(items) == {"commit-commands@official", "playwright@official", "notes@local-m"}
    assert items["commit-commands@official"].description == "Git commit helpers"
    assert items["playwright@official"].installed == [] and not items["playwright@official"].installed_here
    assert any("gone" in w and "no catalog" in w for w in warnings)
    assert any("broken" in w for w in warnings) and any("nolist" in w for w in warnings)
    assert items["commit-commands@official"].matches("git dev") and not items["notes@local-m"].matches("git")


def test_installed_marks_follow_the_scope(markets):
    ip = markets.home / ".claude" / "plugins" / "installed_plugins.json"
    doc = json.loads(ip.read_text())
    doc["plugins"]["playwright@official"] = [{"scope": "local", "projectPath": "/elsewhere", "installPath": "/x"}]
    doc["plugins"]["notes@local-m"] = [{"scope": "project", "projectPath": str(markets.project), "installPath": "/y"}]
    ip.write_text(json.dumps(doc))
    items = {x.id: x for x in marketplace.available(markets.project)}
    assert items["playwright@official"].installed == ["local (another project)"]
    assert not items["playwright@official"].installed_here
    assert items["notes@local-m"].installed == ["project (this project)"] and items["notes@local-m"].installed_here


class Run:
    def __init__(self, rc=0, stdout="", stderr="", exc=None):
        self.calls, self.rc, self.stdout, self.stderr, self.exc = [], rc, stdout, stderr, exc

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, self.stderr)


def test_install_argv_never_auto_accepts(fake_home):
    run = Run(stdout=json.dumps({"success": True, "plugin": "playwright@official"}) + "\n")
    r = marketplace.install("claude", "playwright@official", "project", fake_home.project, run=run, timeout=5)
    assert r.ok and not r.needs_confirmation
    argv, kw = run.calls[0]
    assert argv == ["claude", "plugin", "install", "playwright@official", "--scope", "project", "--json"]
    assert not {"-y", "--yes", "--accept-command"} & set(argv)
    assert kw["stdin"] is subprocess.DEVNULL and kw["timeout"] == 5 and kw["cwd"] == str(fake_home.project)
    assert "shell" not in kw and "CLAUDECODE" not in kw["env"]


def test_install_that_needs_a_confirmation_is_left_to_the_user(fake_home):
    shown = {"success": False, "shownCommand": {"command": "npx some-installer", "sha256": "ab" * 32}}
    run = Run(rc=1, stdout=json.dumps(shown) + "\n")
    r = marketplace.install("claude", "playwright@official", "user", fake_home.project, run=run)
    assert not r.ok and r.needs_confirmation and r.command == "npx some-installer"
    advice = marketplace.advice(r)
    assert "claude plugin install playwright@official --scope user" in advice and "--json" not in advice
    assert "in a terminal" in advice
    # a human-readable refusal without JSON counts too
    r = marketplace.install("claude", "p@m", "user", fake_home.project,
                            run=Run(rc=1, stderr="pass -y or --accept-command to accept"))
    assert r.needs_confirmation


def test_install_failures_never_raise(fake_home):
    r = marketplace.install("claude", "p@m", "user", fake_home.project, run=Run(rc=1, stderr="not found"))
    assert not r.ok and not r.needs_confirmation and "not found" in r.output
    r = marketplace.install("claude", "p@m", "user", fake_home.project,
                            run=Run(exc=subprocess.TimeoutExpired("claude", 1)), timeout=1)
    assert not r.ok and "did not finish" in r.output
    r = marketplace.install("claude", "p@m", "user", fake_home.project, run=Run(exc=FileNotFoundError(2, "nope")))
    assert not r.ok and "cannot run" in r.output
    with pytest.raises(ValueError):
        marketplace.install_argv("claude", "no-marketplace", "user")
    with pytest.raises(ValueError):
        marketplace.install_argv("claude", "p@m", "global")
    with pytest.raises(ValueError):
        marketplace.install_argv("claude", "--yes@m", "user")


# ---------------------------------------------------------------------------- cm plugins


def test_cm_plugins_available(markets, capsys):
    assert cli.main(["plugins", "available", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [p["id"] for p in doc["plugins"]] == ["notes@local-m", "commit-commands@official", "playwright@official"]
    assert cli.main(["plugins", "available", "--search", "browser"]) == 0
    out = capsys.readouterr().out
    assert "playwright@official" in out and "commit-commands" not in out


def test_cm_plugins_install(markets, capsys, monkeypatch):
    run = Run(stdout=json.dumps({"success": True}))
    monkeypatch.setattr(subprocess, "run", run)
    assert cli.main(["plugins", "install", "playwright@official", "--scope", "local", "--claude-bin", "/bin/claude",
                     "--cwd", str(markets.project)]) == 0
    assert run.calls[0][0] == ["/bin/claude", "plugin", "install", "playwright@official", "--scope", "local", "--json"]
    assert "installed playwright@official" in capsys.readouterr().err

    monkeypatch.setattr(subprocess, "run", Run(rc=1, stdout=json.dumps({"shownCommand": {"command": "curl x"}})))
    assert cli.main(["plugins", "install", "playwright@official", "--claude-bin", "claude"]) == 1
    err = capsys.readouterr().err
    assert "run `claude plugin install playwright@official --scope user` in a terminal" in err

    assert cli.main(["plugins", "install", "bogus"]) == 2
    monkeypatch.setattr(subprocess, "run", Run(stdout="{}"))
    assert cli.main(["plugins", "install", "unknown@official", "--claude-bin", "claude"]) == 0
    assert "not in the catalog" in capsys.readouterr().err


# ---------------------------------------------------------------------------- the screen (Ctrl+O)


def test_ctrl_o_installs_and_the_plugins_list_follows(markets, monkeypatch):
    calls = []

    def fake_install(claude_bin, plugin_id, scope, cwd, **kw):
        calls.append((plugin_id, scope))
        # what claude would do: record the install and enable the plugin in that scope's settings
        ip = markets.home / ".claude" / "plugins" / "installed_plugins.json"
        doc = json.loads(ip.read_text())
        doc["plugins"][plugin_id] = [{"scope": "user", "installPath": str(markets.home / "plug" / "pw")}]
        ip.write_text(json.dumps(doc))
        s = markets.home / ".claude" / "settings.json"
        sd = json.loads(s.read_text())
        sd["enabledPlugins"][plugin_id] = True
        s.write_text(json.dumps(sd))
        return marketplace.InstallResult(True, "✔ installed", argv=marketplace.install_argv(claude_bin, plugin_id,
                                                                                            scope))

    monkeypatch.setattr(marketplace, "install", fake_install)
    app = LauncherApp(discover(markets.project), Selection(), claude_bin="claude")

    async def steps(app, pilot):
        assert "Ctrl+O install plugins" in str(app.query_one("#hint", Static).render())
        await pilot.press("ctrl+o")
        await settle(pilot, 8)
        scr = app.screen
        assert isinstance(scr, InstallScreen)
        ol = scr.query_one("#install_list", OptionList)
        assert ol.option_count == 3
        scr.query_one("#install_search", Input).value = "browser"
        await settle(pilot)
        assert ol.option_count == 1 and scr._chosen().id == "playwright@official"
        go = scr.query_one("#install_go", Button)
        go.press()
        await settle(pilot)
        assert not calls and "Confirm: install playwright" in str(go.label)  # the first press only shows it
        assert "claude plugin install playwright@official --scope user --json" in str(
            scr.query_one("#install_out", Static).render())
        go.press()
        for _ in range(50):
            await pilot.pause(0.02)
            if calls and not scr.busy:
                break
        await settle(pilot)
        assert calls == [("playwright@official", "user")]
        assert "installed playwright@official" in str(scr.query_one("#install_out", Static).render())
        await pilot.press("escape")
        for _ in range(50):
            await pilot.pause(0.02)
            if app.cat.plugin("playwright@official"):
                break
        await settle(pilot)
        assert not isinstance(app.screen, InstallScreen)
        sl = app.query_one("#plugins", SelectionList)
        assert "playwright@official" in [sl.get_option_at_index(i).value for i in range(sl.option_count)]
        await pilot.press("escape")

    drive(app, steps)


def test_ctrl_o_shows_a_confirmation_it_will_not_give(markets, monkeypatch):
    def needs(claude_bin, plugin_id, scope, cwd, **kw):
        return marketplace.InstallResult(False, '{"shownCommand": {"command": "npx x"}}', True, "npx x",
                                         marketplace.install_argv(claude_bin, plugin_id, scope))

    monkeypatch.setattr(marketplace, "install", needs)
    app = LauncherApp(discover(markets.project), Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await pilot.press("ctrl+o")
        await settle(pilot, 8)
        scr = app.screen
        ol = scr.query_one("#install_list", OptionList)
        ol.focus()
        await pilot.pause()
        await pilot.press("enter")  # Enter on a row arms it (not the launcher's Enter)
        await settle(pilot)
        assert app.return_value is None and scr.armed is not None
        await pilot.press("enter")  # focus is on the Confirm button now: runs
        for _ in range(50):
            await pilot.pause(0.02)
            if "terminal" in str(scr.query_one("#install_out", Static).render()):
                break
        out = str(scr.query_one("#install_out", Static).render())
        assert "npx x" in out and "in a terminal" in out
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, InstallScreen) and app.return_value is None
        await pilot.press("escape")

    drive(app, steps)
