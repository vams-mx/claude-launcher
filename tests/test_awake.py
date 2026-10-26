"""Keep awake (macOS caffeinate): argv, lifecycle and cleanup, platform hiding, battery detection,
profiles / remember-last round-trips. Popen is always faked: no real caffeinate or pmset runs."""

import asyncio
import json
import signal
import subprocess

import pytest
from textual.widgets import RadioButton, RadioSet, Static

from claude_launcher import awake, cli, config, profiles, remember
from claude_launcher.launcher import launch
from claude_launcher.plan import PlanError, Selection, build_plan
from claude_launcher.tui import LauncherApp
from tests.conftest import FakeProc

CAF = "/usr/bin/caffeinate"


# ---------------------------------------------------------------------------- fakes


class FakeCaf:
    def __init__(self, argv, kw, log, *, exits_itself=False, hangs=False):
        self.argv, self.kw, self.log = argv, kw, log
        self.pid = 50001
        self.returncode = 0 if exits_itself else None
        self.hangs = hangs
        self.terminated = self.killed = False
        self.reaped = exits_itself

    def poll(self):
        return self.returncode

    def terminate(self):
        self.log.append("caffeinate terminated")
        self.terminated = True
        if not self.hangs:
            self.returncode = -15

    def kill(self):
        self.log.append("caffeinate killed")
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        self.reaped = True
        return self.returncode


class Procs:
    """A Popen stand-in that tells claude and caffeinate apart by argv[0]."""

    def __init__(self, rc=0, on_wait=None, caf_error=None, exits_itself=False, hangs=False):
        self.rc, self.on_wait, self.caf_error = rc, on_wait, caf_error
        self.exits_itself, self.hangs = exits_itself, hangs
        self.log = []
        self.claude = None
        self.caf = None

    def __call__(self, argv, **kw):
        if argv[0] == CAF:
            self.log.append("caffeinate started")
            if self.caf_error:
                raise self.caf_error
            self.caf = FakeCaf(argv, kw, self.log, exits_itself=self.exits_itself, hangs=self.hangs)
            return self.caf
        self.log.append("claude started")

        def on_wait(proc):
            self.log.append("claude waited")
            if self.on_wait:
                self.on_wait(proc)

        self.claude = FakeProc(argv, self.rc, on_wait, **kw)
        return self.claude


def stopped(caf):
    return caf.poll() is not None and caf.reaped


# ---------------------------------------------------------------------------- pure helpers


def test_argv_is_a_plain_list_with_the_flags_and_the_claude_pid():
    assert awake.caffeinate_argv("ims", 1234) == [CAF, "-ims", "-w", "1234"]
    assert awake.caffeinate_argv("dims", 7) == [CAF, "-dims", "-w", "7"]
    with pytest.raises(ValueError):
        awake.caffeinate_argv("off", 1)
    with pytest.raises(ValueError):
        awake.caffeinate_argv("ims; rm -rf /", 1)
    assert awake.describe("ims") == "caffeinate -ims -w <claude pid> (ends with claude)"


@pytest.mark.parametrize("value,expected,noted", [
    ("off", "off", False), ("ims", "ims", False), ("dims", "dims", False),
    ("always", "off", True), ("IMS", "off", True), (None, "off", True), (["ims"], "off", True), (1, "off", True),
])
def test_normalize_unknown_values_fall_back_to_off_with_a_note(value, expected, noted):
    mode, note = awake.normalize(value)
    assert mode == expected and bool(note) == noted
    if noted:
        assert "unknown; using Off" in note


BATT = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t64%; discharging; 3:10 remaining present: true\n"
AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t79%; AC attached; not charging present: true\n"
DESKTOP = "Now drawing from 'AC Power'\n"


@pytest.mark.parametrize("text,expected", [
    (BATT, "battery"), (AC, "ac"), (DESKTOP, "ac"), ("Now drawing from 'UPS Power'\n", None), ("", None),
    ("garbage\n", None),
])
def test_parse_power_source(text, expected):
    assert awake.parse_power_source(text) == expected


def test_power_source_runs_only_read_only_pmset():
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, BATT, "")

    assert awake._read_power_source(run=run) == "battery"
    [(argv, kw)] = seen
    assert argv == ["/usr/bin/pmset", "-g", "batt"] and "shell" not in kw and kw["timeout"] <= 2
    assert awake._read_power_source(run=lambda a, **k: subprocess.CompletedProcess(a, 1, AC, "")) is None

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 2)

    assert awake._read_power_source(run=boom) is None

    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])

    assert awake._read_power_source(run=missing) is None


def test_power_source_never_runs_off_macos(monkeypatch):
    monkeypatch.setattr(awake, "PLATFORM", "linux")

    def run(*a, **k):
        raise AssertionError("pmset must not run off macOS")

    assert awake._read_power_source(run=run) is None


def test_plan_validates_the_mode_and_keeps_argv_and_session_files_unchanged(catalog):
    with pytest.raises(PlanError, match="keep-awake"):
        build_plan(catalog, Selection(awake="always"), claude_bin="claude")
    off = build_plan(catalog, Selection(local=["gdrive"]), claude_bin="claude")
    on = build_plan(catalog, Selection(local=["gdrive"], awake="dims"), claude_bin="claude")
    # caffeinate is not part of claude's argv (so a cmux restore doesn't restart it)
    assert on.argv == off.argv and on.mcp_path == off.mcp_path and on.env_overrides == off.env_overrides


# ---------------------------------------------------------------------------- lifecycle


def test_caffeinate_starts_after_claude_with_its_pid_and_is_stopped(catalog):
    seen = {}
    procs = Procs(on_wait=lambda p: seen.setdefault("caf_running", procs.caf.poll() is None))
    called = []
    rc = launch(catalog, Selection(awake="ims"), claude_bin="claude", popen=procs,
                on_started=lambda: called.append(list(procs.log)))
    assert rc == 0
    assert procs.log[:3] == ["claude started", "caffeinate started", "claude waited"]
    assert called == [["claude started", "caffeinate started"]]  # on_started still runs, once
    caf = procs.caf
    assert caf.argv == [CAF, "-ims", "-w", str(procs.claude.pid)]
    assert caf.kw["stdin"] is subprocess.DEVNULL and caf.kw["stdout"] is subprocess.DEVNULL
    assert caf.kw["stderr"] is subprocess.DEVNULL and caf.kw["start_new_session"] is True
    assert "shell" not in caf.kw and "env" not in caf.kw  # claude's env (tokens) isn't passed to it
    assert seen["caf_running"]  # held while claude ran
    assert stopped(caf) and procs.log[-1] == "caffeinate terminated"


def test_dims_flags(catalog):
    procs = Procs()
    launch(catalog, Selection(awake="dims"), claude_bin="claude", popen=procs)
    assert procs.caf.argv[1:3] == ["-dims", "-w"]


def test_caffeinate_that_already_exited_with_claude_is_not_signalled(catalog):
    procs = Procs(exits_itself=True)
    assert launch(catalog, Selection(awake="ims"), claude_bin="claude", popen=procs) == 0
    assert not procs.caf.terminated and "caffeinate terminated" not in procs.log


def test_nonzero_exit_code_passes_through_and_caffeinate_is_stopped(catalog):
    procs = Procs(rc=3)
    assert launch(catalog, Selection(awake="ims"), claude_bin="claude", popen=procs) == 3
    assert stopped(procs.caf)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_forwarded_signals_still_reach_claude_and_caffeinate_is_stopped(catalog, sig):
    def on_wait(proc):
        signal.getsignal(sig)(sig, None)  # cm receives it while waiting: forwarded to claude
        proc.rc = -sig

    procs = Procs(on_wait=on_wait)
    assert launch(catalog, Selection(awake="ims"), claude_bin="claude", popen=procs) == 128 + sig
    assert procs.claude.signals == [sig]
    assert stopped(procs.caf)


def test_ctrl_c_is_ignored_while_claude_runs_and_caffeinate_is_stopped(catalog):
    seen = {}

    def on_wait(proc):
        seen["int"] = signal.getsignal(signal.SIGINT)
        proc.rc = -signal.SIGINT  # claude exits on its own Ctrl+C

    procs = Procs(on_wait=on_wait)
    assert launch(catalog, Selection(awake="ims"), claude_bin="claude", popen=procs) == 130
    assert seen["int"] == signal.SIG_IGN and stopped(procs.caf)


@pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyboardInterrupt()])
def test_an_exception_while_waiting_still_stops_caffeinate(catalog, exc):
    def on_wait(proc):
        raise exc

    procs = Procs(on_wait=on_wait)
    with pytest.raises(type(exc)):
        launch(catalog, Selection(awake="ims"), claude_bin="claude", popen=procs)
    assert stopped(procs.caf)


def test_a_caffeinate_that_ignores_terminate_is_killed(catalog):
    procs = Procs(hangs=True)
    assert launch(catalog, Selection(awake="ims"), claude_bin="claude", popen=procs) == 0
    assert procs.caf.terminated and procs.caf.killed and stopped(procs.caf)


@pytest.mark.parametrize("error,text", [
    (FileNotFoundError(2, "No such file or directory"), "/usr/bin/caffeinate not found"),
    (PermissionError(13, "Permission denied"), "could not start caffeinate (Permission denied)"),
    (OSError(35, "Resource temporarily unavailable"), "could not start caffeinate"),
])
def test_missing_caffeinate_warns_once_after_claude_exits(catalog, capsys, error, text):
    during = {}
    procs = Procs(on_wait=lambda p: during.setdefault("err", capsys.readouterr().err), caf_error=error)
    assert launch(catalog, Selection(awake="ims"), claude_bin="claude", popen=procs) == 0
    assert "caffeinate" not in during["err"]  # nothing printed into claude's screen
    err = capsys.readouterr().err
    assert err.count("keep awake") == 1 and text in err and err.startswith("cm: warning: keep awake")


def test_off_starts_nothing(catalog):
    procs = Procs()
    launch(catalog, Selection(), claude_bin="claude", popen=procs)
    assert procs.log == ["claude started", "claude waited"]


def test_claude_that_cannot_start_starts_no_caffeinate(catalog, capsys):
    log = []

    def popen(argv, **kw):
        log.append(argv[0])
        raise FileNotFoundError(argv[0])

    assert launch(catalog, Selection(awake="ims"), claude_bin="/nope/claude", popen=popen) == 127
    assert log == ["/nope/claude"]


def test_linux_ignores_a_stored_value_without_spawning_or_warning(catalog, monkeypatch, capsys):
    monkeypatch.setattr(awake, "PLATFORM", "linux")
    procs = Procs()
    assert launch(catalog, Selection(awake="dims"), claude_bin="claude", popen=procs) == 0
    assert procs.caf is None and "caffeinate" not in capsys.readouterr().err


# ---------------------------------------------------------------------------- profiles / remember-last


def test_profiles_keep_the_value_and_fix_unknown_ones(fake_home, catalog):
    p = profiles.save("awake", Selection(local=["gdrive"], awake="dims"))
    assert json.loads(p.read_text())["awake"] == "dims"
    loaded, notes = profiles.reconcile(profiles.load("awake"), catalog)
    assert loaded.awake == "dims" and not notes
    for bad in ("always", 3, None):
        p.write_text(json.dumps({"local": ["gdrive"], "awake": bad}))
        loaded, notes = profiles.reconcile(profiles.load("awake"), catalog)
        assert loaded.awake == "off" and any("keep awake" in n and "using Off" in n for n in notes)


def test_remember_round_trip_and_unknown_values(fake_home):
    assert remember.save(fake_home.project, Selection(local=["x"], awake="ims")) is None
    entry, warns = remember.load(fake_home.project)
    assert not warns and entry.selection.awake == "ims" and not entry.notes
    p = remember.entry_path(remember.project_root(fake_home.project))
    doc = json.loads(p.read_text())
    doc["selection"]["awake"] = "forever"
    p.write_text(json.dumps(doc))
    entry, warns = remember.load(fake_home.project)
    assert not warns and entry.selection.awake == "off" and entry.selection.local == ["x"]
    assert any("keep awake 'forever'" in n for n in entry.notes)
    del doc["selection"]["awake"]  # a file from before keep awake existed
    p.write_text(json.dumps(doc))
    entry, _ = remember.load(fake_home.project)
    assert entry.selection.awake == "off" and not entry.notes


def test_config_comment_mentions_keep_awake_is_remembered():
    assert any("remember_last" in line and "keep awake" in line for line in config.COMMENT)


# ---------------------------------------------------------------------------- CLI


@pytest.fixture
def screen(fake_home, monkeypatch):
    import claude_launcher.tui as tui

    class S:
        opened = []
        launched = []
        result = None

    s = S()
    monkeypatch.setattr(cli, "_has_tty", lambda: True)

    def fake_tui(cat, sel, **kw):
        s.opened.append((sel, kw))
        return s.result

    def fake_launch(cat, sel, *, claude_bin, on_started=None):
        s.launched.append(sel)
        if on_started:
            on_started()
        return 0

    monkeypatch.setattr(tui, "run_tui", fake_tui)
    monkeypatch.setattr(cli, "launch", fake_launch)
    return s


def run(*args):
    return cli.main(["--claude-bin", "claude", *args])


def test_cli_headless_launch_starts_caffeinate_with_claudes_pid(fake_home, monkeypatch):
    procs = Procs()
    monkeypatch.setattr(subprocess, "Popen", procs)
    assert run("--mcp", "gdrive", "--awake", "ims") == 0
    assert procs.claude.argv[0] == "claude"
    assert procs.caf.argv == [CAF, "-ims", "-w", str(procs.claude.pid)] and stopped(procs.caf)


def test_cli_awake_alone_opens_the_screen_and_keeps_the_prefill(fake_home, screen):
    from tests.test_remember import write_last

    write_last(fake_home.project, {"local": ["gdrive"], "awake": "dims"})
    assert run() == 0
    assert screen.opened[-1][0].awake == "dims"  # remembered value pre-fills the screen
    assert run("--awake", "ims") == 0  # like --name: not a selection flag
    sel, kw = screen.opened[-1]
    assert sel.local == ["gdrive"] and sel.awake == "ims" and kw["prefill"] and not screen.launched


def test_cli_awake_overrides_a_profile(fake_home, screen):
    profiles.save("p", Selection(local=["gdrive"], awake="dims"))
    assert run("--profile", "p") == 0
    assert screen.launched[-1].awake == "dims"
    assert run("--profile", "p", "--awake", "off") == 0
    assert screen.launched[-1].awake == "off"


def test_cli_rejects_unknown_values(fake_home, capsys):
    with pytest.raises(SystemExit) as e:
        run("--awake", "always", "--mcp", "gdrive")
    assert e.value.code == 2 and "invalid choice" in capsys.readouterr().err


def test_dry_run_shows_the_line_and_the_battery_note(fake_home, monkeypatch, capsys):
    assert run("--mcp", "gdrive", "--awake", "ims", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Keep awake: caffeinate -ims -w <claude pid> (ends with claude)" in out
    assert "on battery" not in out
    monkeypatch.setattr(awake, "power_source", lambda run=None, timeout=2.0: "battery")
    assert run("--mcp", "gdrive", "--awake", "dims", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "caffeinate -dims -w <claude pid>" in out
    assert "on battery: keeps the Mac awake and drains the battery faster; -s only applies on AC power" in out
    assert run("--mcp", "gdrive", "--awake", "dims", "--dry-run", "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["awake"] == {"mode": "dims", "argv": [CAF, "-dims", "-w", "<claude pid>"], "power": "battery"}
    assert "caffeinate" not in " ".join(doc["argv"])
    assert run("--mcp", "gdrive", "--dry-run", "--json") == 0
    assert json.loads(capsys.readouterr().out)["awake"] is None


def test_dry_run_reads_power_only_when_keeping_awake(fake_home, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(awake, "power_source", lambda run=None, timeout=2.0: calls.append(1) or "ac")
    assert run("--mcp", "gdrive", "--dry-run") == 0
    assert not calls and "Keep awake" not in capsys.readouterr().out


def test_help_lists_awake_on_macos_only(fake_home, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        run("--help")
    assert "--awake" in capsys.readouterr().out
    monkeypatch.setattr(awake, "PLATFORM", "linux")
    with pytest.raises(SystemExit):
        run("--help")
    assert "--awake" not in capsys.readouterr().out


def test_linux_cli_value_is_ignored_with_a_note(fake_home, monkeypatch, capsys):
    monkeypatch.setattr(awake, "PLATFORM", "linux")
    procs = Procs()
    monkeypatch.setattr(subprocess, "Popen", procs)
    assert run("--mcp", "gdrive", "--awake", "ims") == 0
    assert procs.caf is None and procs.claude is not None
    err = capsys.readouterr().err
    assert err.count("--awake is macOS only; ignored") == 1
    assert run("--mcp", "gdrive", "--awake", "dims", "--dry-run") == 0
    assert "Keep awake" not in capsys.readouterr().out


def test_linux_ignores_a_profile_value(fake_home, monkeypatch, capsys):
    profiles.save("mac", Selection(local=["gdrive"], awake="dims"))
    monkeypatch.setattr(awake, "PLATFORM", "linux")
    procs = Procs()
    monkeypatch.setattr(subprocess, "Popen", procs)
    assert run("--profile", "mac") == 0
    assert procs.caf is None and "keep awake" not in capsys.readouterr().err.lower()
    assert json.loads((profiles.profiles_dir() / "mac.json").read_text())["awake"] == "dims"  # kept on disk
    assert run("--profile", "mac", "--dry-run") == 0
    assert "Keep awake" not in capsys.readouterr().out


# ---------------------------------------------------------------------------- the screen


def drive(app, steps, size=(160, 60)):
    async def go():
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await steps(app, pilot)

    asyncio.run(go())
    return app.return_value


def preview(app):
    return str(app.query_one("#preview", Static).render())


def test_screen_box_selects_dims_and_the_preview_shows_it(catalog):
    app = LauncherApp(catalog, Selection(name="x"), claude_bin="claude")

    async def steps(app, pilot):
        box = app.query_one("#awake", RadioSet)
        assert box.display and app.query_one("#awake_label").display
        assert [str(b.label) for b in box.query(RadioButton)] == ["Off", "System (-ims)", "System + display (-dims)"]
        assert box.pressed_button.name == "off" and "Keep awake" not in preview(app)
        [b for b in box.query(RadioButton) if b.name == "dims"][0].value = True
        await pilot.pause()
        assert "Keep awake: caffeinate -dims -w <claude pid> (ends with claude)" in preview(app)
        assert "on battery" not in preview(app)
        app.action_do_launch()

    result = drive(app, steps)
    assert result[0] == "launch" and result[1].awake == "dims"


def test_screen_battery_note_from_the_startup_read(catalog, monkeypatch):
    import threading

    threads = []

    def power(run=None, timeout=2.0):
        threads.append(threading.current_thread() is threading.main_thread())
        return "battery"

    monkeypatch.setattr(awake, "power_source", power)
    app = LauncherApp(catalog, Selection(awake="ims"), claude_bin="claude")

    async def steps(app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()
        text = preview(app)
        assert "caffeinate -ims -w <claude pid>" in text
        assert "on battery: keeps the Mac awake and drains the battery faster; -s only applies on AC power" in text
        await pilot.press("escape")

    drive(app, steps)
    assert threads == [False]  # read once, off the UI thread


def test_screen_arrows_flow_through_the_box(catalog):
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        perm = app.query_one("#perm", RadioSet)
        perm.focus()
        await pilot.pause()
        await pilot.press("down", "down")  # Standard -> Skip -> next box
        await pilot.pause()
        assert app.focused.id == "awake"
        await pilot.press("down", "down", "down")  # through its three rows, then on
        await pilot.pause()
        assert app.focused.id == "session"
        await pilot.press("up")  # back: lands on its last row
        await pilot.pause()
        assert app.focused.id == "awake" and app.focused._selected == 2
        await pilot.press("escape")

    drive(app, steps)


def test_loading_a_profile_applies_keep_awake(catalog):
    from textual.widgets import Select

    profiles.save("caf", Selection(local=["gdrive"], awake="ims"))
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        app.query_one("#profile", Select).value = "caf"
        await pilot.pause()
        assert app.query_one("#awake", RadioSet).pressed_button.name == "ims"
        assert "caffeinate -ims" in preview(app)
        await pilot.press("escape")

    drive(app, steps)


def test_screen_on_linux_has_no_box_and_carries_a_stored_value(catalog, monkeypatch):
    monkeypatch.setattr(awake, "PLATFORM", "linux")
    calls = []
    monkeypatch.setattr(awake, "power_source", lambda run=None, timeout=2.0: calls.append(1))
    app = LauncherApp(catalog, Selection(awake="dims", name="x"), claude_bin="claude")

    async def steps(app, pilot):
        assert not app.query_one("#awake").display and not app.query_one("#awake_label").display
        assert "awake" not in [w.id for w in app.screen.focus_chain]
        assert "Keep awake" not in preview(app) and "caffeinate" not in preview(app)
        perm = app.query_one("#perm", RadioSet)
        perm.focus()
        await pilot.pause()
        await pilot.press("down", "down")
        await pilot.pause()
        assert app.focused.id == "session"  # skipped straight over the hidden box
        app.action_do_launch()

    result = drive(app, steps)
    assert result[1].awake == "dims" and not calls  # kept (a profile saved here keeps it), never used
