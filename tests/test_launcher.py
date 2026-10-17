import json
import os
import signal
import stat
import tempfile
from pathlib import Path

from claude_launcher.launcher import exit_code, launch
from claude_launcher.plan import Selection
from tests.conftest import CUA_TOKEN, MEM_SECRET


def _paths(argv):
    mcp = Path(next(a.split("=", 1)[1] for a in argv if a.startswith("--mcp-config=")))
    return mcp, Path(argv[argv.index("--settings") + 1])


def test_launch_writes_private_files_and_keeps_them(catalog, fake_popen, fake_home):
    seen = {}

    def on_wait(proc):
        mcp, settings = _paths(proc.argv)
        seen["dir"] = mcp.parent
        seen["dir_mode"] = stat.S_IMODE(mcp.parent.stat().st_mode)
        seen["modes"] = {stat.S_IMODE(p.stat().st_mode) for p in (mcp, settings)}
        seen["mcp"] = json.loads(mcp.read_text())
        seen["settings"] = json.loads(settings.read_text())

    popen = fake_popen(rc=0, on_wait=on_wait)
    rc = launch(catalog, Selection(local=["agentmemory", "gdrive"], cmux=True), claude_bin="claude", popen=popen)
    assert rc == 0
    proc = popen.calls[0]
    assert isinstance(proc.argv, list) and proc.argv[0] == "claude"
    assert "shell" not in proc.kw
    assert seen["modes"] == {0o600} and seen["dir_mode"] == 0o700
    assert seen["dir"].parent == fake_home.home / ".claude-launcher" / "sessions"
    assert stat.S_IMODE(seen["dir"].parent.stat().st_mode) == 0o700
    assert set(seen["mcp"]["mcpServers"]) == {"agentmemory", "gdrive", "cmux-cua"}
    assert "hooks" in seen["settings"]
    mcp, settings = _paths(proc.argv)
    assert mcp.is_file() and settings.is_file()  # kept after claude exits (cmux restore replays the argv)
    assert json.loads(mcp.read_text()) == seen["mcp"]
    env = proc.kw["env"]
    assert env["CMUX_COMPUTER_USE_MCP_DISABLED"] == "1"
    assert env["CMUX_CUA_SOCKET_AUTH_TOKEN"] == CUA_TOKEN
    assert env["HOME"] == os.environ["HOME"]  # environment passed through
    assert proc.kw["cwd"] == catalog.cwd  # claude runs where the catalog was discovered
    assert CUA_TOKEN not in mcp.read_text() + settings.read_text()  # the token stays environment-only
    assert MEM_SECRET in mcp.read_text()  # a literal env value in a server file is copied verbatim (README)


def test_same_selection_reuses_the_same_files(catalog, fake_popen, fake_home):
    sel = Selection(local=["agentmemory"], cmux=True, connectors=["claude.ai Slack"])
    p1, p2 = fake_popen(), fake_popen()
    assert launch(catalog, sel, claude_bin="claude", popen=p1) == 0
    first = _paths(p1.calls[0].argv)
    before = [(p.stat().st_ino, p.read_bytes()) for p in first]
    os.utime(first[0].parent, (1, 1))  # pretend it was last used long ago
    assert launch(catalog, Selection.from_dict(sel.to_dict()), claude_bin="claude", popen=p2) == 0
    second = _paths(p2.calls[-1].argv)
    assert second == first
    assert [(p.stat().st_ino, p.read_bytes()) for p in second] == before  # identical content: not rewritten
    assert first[0].parent.stat().st_mtime > 1000  # reuse refreshes "last used"
    from claude_launcher import sessions

    assert sessions.count() == 1
    p3 = fake_popen()
    launch(catalog, Selection(local=["gdrive"]), claude_bin="claude", popen=p3)
    assert _paths(p3.calls[-1].argv)[0].parent != first[0].parent


def test_tampered_files_are_restored(catalog, fake_popen):
    p1 = fake_popen()
    launch(catalog, Selection(local=["gdrive"]), claude_bin="claude", popen=p1)
    mcp, settings = _paths(p1.calls[0].argv)
    good = mcp.read_bytes()
    mcp.write_text("{}")
    settings.unlink()
    elsewhere = mcp.parent.parent / "elsewhere.json"
    elsewhere.write_text("{}")
    launch(catalog, Selection(local=["gdrive"]), claude_bin="claude", popen=fake_popen())
    assert mcp.read_bytes() == good and settings.is_file()
    settings.unlink()
    settings.symlink_to(elsewhere)  # a symlink is replaced, never followed or written through
    launch(catalog, Selection(local=["gdrive"]), claude_bin="claude", popen=fake_popen())
    assert not settings.is_symlink() and elsewhere.read_text() == "{}"
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600


def test_session_files_respect_cm_config_dir(catalog, fake_popen, tmp_path, monkeypatch):
    monkeypatch.setenv("CM_CONFIG_DIR", str(tmp_path / "cmhome"))
    popen = fake_popen()
    launch(catalog, Selection(), claude_bin="claude", popen=popen)
    assert _paths(popen.calls[0].argv)[0].parent.parent == tmp_path / "cmhome" / "sessions"


def test_unwritable_sessions_dir_falls_back_to_a_temp_dir(catalog, fake_popen, fake_home, capsys):
    root = fake_home.home / ".claude-launcher" / "sessions"
    root.mkdir(mode=0o500)  # read-only: cm must not "fix" its mode, and still launch
    seen = {}

    def on_wait(proc):
        mcp, settings = _paths(proc.argv)
        seen["dir"] = mcp.parent
        seen["ok"] = mcp.is_file() and settings.is_file()
        seen["settings"] = json.loads(settings.read_text())

    try:
        popen = fake_popen(rc=3, on_wait=on_wait)
        assert launch(catalog, Selection(local=["agentmemory"]), claude_bin="claude", popen=popen) == 3
    finally:
        root.chmod(0o700)
    assert seen["ok"] and seen["dir"].name.startswith("cm-session-") and root not in seen["dir"].parents
    assert not seen["dir"].exists()  # the fallback is removed when claude exits
    assert "touch -c" not in json.dumps(seen["settings"])  # no "last used" hook for a throwaway dir
    assert set(seen["settings"]["hooks"]) == {"SessionStart", "Stop"}  # companion hooks still there
    err = capsys.readouterr().err
    assert "warning: session files won't survive a cmux restore: cannot write ~/.claude-launcher/sessions/" in err
    assert stat.S_IMODE(root.stat().st_mode) == 0o700 and not any(root.iterdir())


def test_sessions_dir_owned_by_someone_else_gives_an_actionable_message(catalog, fake_popen, fake_home, capsys,
                                                                         monkeypatch):
    root = fake_home.home / ".claude-launcher" / "sessions"
    root.mkdir()
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)  # as if root (sudo) had created it
    popen = fake_popen()
    assert launch(catalog, Selection(), claude_bin="claude", popen=popen) == 0
    monkeypatch.undo()
    err = capsys.readouterr().err
    assert "won't survive a cmux restore: ~/.claude-launcher/sessions belongs to uid" in err
    assert "was cm run with sudo?" in err and "sudo chown -R" in err and f"sudo rm -rf {root}" in err
    assert popen.calls[0].argv[2].split("=", 1)[1].startswith(tempfile.gettempdir())


def test_exit_codes(catalog, fake_popen):
    assert launch(catalog, Selection(), claude_bin="claude", popen=fake_popen(rc=3)) == 3
    assert launch(catalog, Selection(), claude_bin="claude", popen=fake_popen(rc=-signal.SIGINT)) == 130
    assert exit_code(-15) == 143


def test_files_kept_even_when_wait_raises(catalog, fake_popen):
    holder = {}

    def boom(proc):
        holder["dir"] = Path(proc.argv[proc.argv.index("--settings") + 1]).parent
        raise RuntimeError("x")

    try:
        launch(catalog, Selection(), claude_bin="claude", popen=fake_popen(on_wait=boom))
    except RuntimeError:
        pass
    assert (holder["dir"] / "settings.json").is_file()


def test_missing_binary(catalog, capsys):
    def popen(argv, **kw):
        raise FileNotFoundError(argv[0])

    assert launch(catalog, Selection(), claude_bin="/nope/claude", popen=popen) == 127
    assert "not found" in capsys.readouterr().err


def test_sigint_ignored_while_child_runs_and_restored(catalog, fake_popen):
    before = signal.getsignal(signal.SIGINT)
    seen = {}

    def on_wait(proc):
        seen["int"] = signal.getsignal(signal.SIGINT)
        seen["term"] = signal.getsignal(signal.SIGTERM)
        seen["term"](signal.SIGTERM, None)  # forwarded to the child

    popen = fake_popen(on_wait=on_wait)
    launch(catalog, Selection(), claude_bin="claude", popen=popen)
    assert seen["int"] == signal.SIG_IGN
    assert popen.calls[0].signals == [signal.SIGTERM]
    assert signal.getsignal(signal.SIGINT) == before


def test_inherited_cmux_claude_pid_is_not_passed_on_with_cmux(catalog, fake_popen, monkeypatch):
    from claude_launcher.launcher import launch
    from claude_launcher.plan import Selection

    monkeypatch.setenv("CMUX_CLAUDE_PID", "11111")  # e.g. cm started inside an outer Claude session
    popen = fake_popen()
    assert launch(catalog, Selection(cmux=True), claude_bin="claude", popen=popen) == 0
    # cm's own PID (Claude's parent) replaces it; cmux's wrapper replaces that with Claude's PID
    assert popen.calls[0].kw["env"]["CMUX_CLAUDE_PID"] == str(os.getpid())
    popen = fake_popen()
    assert launch(catalog, Selection(), claude_bin="claude", popen=popen) == 0
    assert popen.calls[-1].kw["env"]["CMUX_CLAUDE_PID"] == "11111"  # untouched without cmux
