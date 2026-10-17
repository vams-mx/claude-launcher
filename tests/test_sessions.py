"""Persistent, content-addressed session files and their 60-day cleanup."""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_launcher import cli, sessions
from claude_launcher.launcher import cleanup_sessions, launch
from claude_launcher.plan import Selection
from tests.conftest import CUA_TOKEN, SECRET, FakeProc
from tests.test_config import bare_home  # noqa: F401 - fixture

DAY = 86400
REAL_RUN, REAL_POPEN = subprocess.run, subprocess.Popen  # captured before the autouse tripwire replaces them


def _sessions(home: Path) -> Path:
    return home / ".claude-launcher" / "sessions"


def _make(root: Path, name: str, age_days: float, files=("mcp.json", "settings.json")) -> Path:
    d = root / name
    d.mkdir(parents=True)
    for f in files:
        (d / f).write_text("{}")
    t = time.time() - age_days * DAY
    os.utime(d, (t, t))
    return d


def _sid(n: int) -> str:
    return f"{n:032x}"


# ---------------------------------------------------------------- hashing


def test_id_is_a_hash_of_the_canonical_documents():
    a = sessions.session_id({"mcpServers": {"a": {"command": "x"}, "b": {}}}, {"enabledPlugins": {"p": True}})
    b = sessions.session_id({"mcpServers": {"b": {}, "a": {"command": "x"}}}, {"enabledPlugins": {"p": True}})
    assert a == b and sessions.is_session_name(a)  # key order doesn't matter
    assert a != sessions.session_id({"mcpServers": {"a": {"command": "y"}, "b": {}}}, {"enabledPlugins": {"p": True}})
    # the two documents are hashed separately: moving content between them changes the id
    assert sessions.session_id({}, {"x": 1}) != sessions.session_id({"x": 1}, {})
    assert sessions.doc_bytes({"b": 1, "a": "é"}) == b'{\n  "a": "\\u00e9",\n  "b": 1\n}\n'


def test_ensure_creates_private_files_atomically_and_reuses(fake_home):
    mcp, settings = {"mcpServers": {"s": {"command": "c"}}}, {"hooks": {}}
    d = sessions.ensure(mcp, settings)
    assert d == sessions.session_path(mcp, settings) and d.parent == _sessions(fake_home.home)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700 and stat.S_IMODE(d.parent.stat().st_mode) == 0o700
    for name, doc in (("mcp.json", mcp), ("settings.json", sessions.with_touch_hook(settings, d))):
        assert json.loads((d / name).read_text()) == doc
        assert stat.S_IMODE((d / name).stat().st_mode) == 0o600
    inode = (d / "mcp.json").stat().st_ino
    (d / "settings.json").chmod(0o644)  # wrong mode: rewritten
    assert sessions.ensure(mcp, settings) == d
    assert (d / "mcp.json").stat().st_ino == inode  # identical: untouched
    assert stat.S_IMODE((d / "settings.json").stat().st_mode) == 0o600
    assert sorted(p.name for p in d.iterdir()) == ["mcp.json", "settings.json"]  # no temp files left


def test_ensure_replaces_a_symlink_squatting_on_the_id(fake_home, tmp_path):
    mcp, settings = {"mcpServers": {}}, {}
    target = tmp_path / "outside"
    target.mkdir()
    root = _sessions(fake_home.home)
    root.mkdir(parents=True)
    (root / sessions.session_id(mcp, settings)).symlink_to(target)
    d = sessions.ensure(mcp, settings)
    assert not d.is_symlink() and d.is_dir()
    assert not any(target.iterdir())  # nothing written through the link


# ---------------------------------------------------------------- cleanup


def test_cleanup_removes_only_stale_cm_session_dirs(fake_home, tmp_path):
    root = _sessions(fake_home.home)
    old = _make(root, _sid(1), 61)
    recent = _make(root, _sid(2), 59)
    foreign_name = _make(root, "my-notes", 400)  # not a session id
    foreign_file = _make(root, _sid(3), 400, files=("mcp.json", "notes.txt"))  # holds something cm never writes
    nested = _make(root, _sid(4), 400)
    (nested / "sub").mkdir()
    os.utime(nested, (time.time() - 400 * DAY,) * 2)
    outside = _make(tmp_path / "elsewhere", _sid(5), 400)
    link = root / _sid(6)
    link.symlink_to(outside)  # a symlink is never followed
    plain = root / _sid(7)
    plain.write_text("x")  # a file named like an id
    leftover = _make(root, _sid(8), 70, files=("mcp.json", ".settings.json.ab_12.tmp"))

    removed = sessions.cleanup(60)
    assert sorted(removed) == [_sid(1), _sid(8)]
    assert not old.exists() and not leftover.exists()
    for kept in (recent, foreign_name, foreign_file, nested, link, plain):
        assert kept.exists() or kept.is_symlink()
    assert (outside / "mcp.json").is_file()


def test_cleanup_keeps_the_current_dir_and_handles_odd_roots(fake_home, tmp_path):
    root = _sessions(fake_home.home)
    assert sessions.cleanup(60) == []  # no sessions dir yet
    current = _make(root, _sid(1), 100)
    assert sessions.cleanup(60, keep=current) == []
    assert sessions.cleanup(None) == [] and current.exists()  # retention 0: keep forever
    # a sessions dir that is itself a symlink is not cleaned at all
    real = tmp_path / "real-sessions"
    root.rename(real)
    root.symlink_to(real)
    assert sessions.cleanup(60) == [] and (real / _sid(1)).exists()


def test_cleanup_uses_last_use_not_creation(fake_home):
    root = _sessions(fake_home.home)
    d = _make(root, _sid(1), 100)
    assert sessions.cleanup(60, now=time.time() - 50 * DAY) == []  # 50 days ago it was only 50 days old
    assert sessions.cleanup(60) == [_sid(1)] and not d.exists()


@pytest.mark.parametrize("value,expected,warns", [
    (None, 60.0, False), (90, 90.0, False), (0.5, 0.5, False), (0, None, False),
    (-1, 60.0, True), ("30", 60.0, True), (True, 60.0, True), (float("nan"), 60.0, True),
])
def test_retention_config(value, expected, warns):
    cfg = {} if value is None else {"session_retention_days": value}
    w: list[str] = []
    assert sessions.retention_days(cfg, w) == expected
    assert bool(w) == warns


def test_launch_cleans_up_old_dirs_and_keeps_recent_ones(catalog, fake_popen, fake_home):
    root = _sessions(fake_home.home)
    old, recent = _make(root, _sid(1), 61), _make(root, _sid(2), 30)
    popen = fake_popen()
    assert launch(catalog, Selection(local=["gdrive"]), claude_bin="claude", popen=popen) == 0
    assert not old.exists() and recent.exists()
    mcp = Path(next(a.split("=", 1)[1] for a in popen.calls[0].argv if a.startswith("--mcp-config=")))
    assert mcp.is_file()


def test_retention_from_config_json(catalog, fake_popen, fake_home, capsys):
    cfg = fake_home.home / ".claude-launcher" / "config.json"
    doc = json.loads(cfg.read_text())
    root = _sessions(fake_home.home)
    d = _make(root, _sid(1), 100)
    cfg.write_text(json.dumps({**doc, "session_retention_days": 365}))
    launch(catalog, Selection(), claude_bin="claude", popen=fake_popen())
    assert d.exists()
    cfg.write_text(json.dumps({**doc, "session_retention_days": "soon"}))
    launch(catalog, Selection(), claude_bin="claude", popen=fake_popen())
    assert not d.exists()  # invalid -> default 60
    assert "session_retention_days must be a number" in capsys.readouterr().err


def test_cleanup_problems_never_fail_a_launch(catalog, fake_popen, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(sessions, "cleanup", boom)
    assert cleanup_sessions() == []
    assert launch(catalog, Selection(), claude_bin="claude", popen=fake_popen(rc=5)) == 5


# ---------------------------------------------------------------- preview, --where, secrets


def test_dry_run_shows_the_real_paths_and_writes_nothing(fake_home, capsys):
    assert cli.main(["--claude-bin", "claude", "--mcp", "gdrive", "--cmux", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "~/.claude-launcher/sessions/" in out and "(new; kept after exit for cmux restore;" in out
    assert "cm-session-" not in out
    assert not _sessions(fake_home.home).exists()
    assert cli.main(["--claude-bin", "claude", "--mcp", "gdrive", "--cmux", "--dry-run", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["session_dir"].startswith(str(_sessions(fake_home.home)))
    assert f"--mcp-config={doc['session_dir']}/mcp.json" in doc["argv"]
    assert CUA_TOKEN not in json.dumps(doc)


def test_where_shows_the_sessions_dir(fake_home, capsys):
    _make(_sessions(fake_home.home), _sid(1), 1)
    assert cli.main(["--claude-bin", "claude", "--where"]) == 0
    out = capsys.readouterr().out
    assert "sessions      ~/.claude-launcher/sessions  (1 kept; removed after 60 days unused; per machine" in out
    assert cli.main(["--claude-bin", "claude", "--where", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["sessions"] == 1 and doc["session_retention_days"] == 60


def test_persisted_files_hold_no_environment_secrets(catalog, fake_popen, fake_home, monkeypatch):
    monkeypatch.setenv("SOME_API_TOKEN", "env-secret-value-777")
    launch(catalog, Selection(local=["agentmemory", "gdrive"], cmux=True, connectors=["claude.ai Slack"]),
           claude_bin="claude", popen=fake_popen())
    blob = "".join(p.read_text() for p in _sessions(fake_home.home).rglob("*.json"))
    for secret in (CUA_TOKEN, SECRET, "SUPER_SECRET_PASSWORD", "env-secret-value-777", str(os.getpid())):
        assert secret not in blob


# ---------------------------------------------------------------- fresh machine


def test_fresh_machine_without_personal_cmux_or_agentmemory(bare_home, capsys, monkeypatch):  # noqa: F811
    """No ~/.personal, no ~/.claude*, no cmux, no AgentMemory: defaults work and messages are clear."""
    assert cli.main(["setup", "init"]) == 0
    assert "created ~/.claude-launcher/" in capsys.readouterr().out
    assert cli.main(["--claude-bin", "claude", "--where"]) == 0
    out = capsys.readouterr().out
    assert "1. ~/.claude-launcher/mcps  [0 server file(s)]" in out and "(none)" in out
    assert "sessions      ~/.claude-launcher/sessions  (0 kept" in out
    assert cli.main(["--claude-bin", "claude", "--list"]) == 0
    assert "cmux-cua" not in capsys.readouterr().out  # no cmux row where cmux isn't installed
    assert cli.main(["--claude-bin", "claude", "--cmux", "--dry-run"]) == 2
    assert "cmux computer-use unavailable: cmux-claude-wrapper not found (cmux not installed?)" in \
        capsys.readouterr().err
    assert cli.main(["--claude-bin", "claude", "--mcp", "agentmemory"]) == 2
    assert "unknown MCP server(s): agentmemory (see cm --list)" in capsys.readouterr().err

    calls = []

    def popen(argv, **kw):
        calls.append(argv)
        return FakeProc(argv)

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert cli.main(["--claude-bin", "claude", "--no-mcp"]) == 0
    assert calls[0][:2] == ["claude", "--strict-mcp-config"]
    d = Path(calls[0][2].split("=", 1)[1]).parent
    assert d.parent == _sessions(bare_home) and (d / "settings.json").is_file()


# ---------------------------------------------------------------- F1: "last used" hook


def _touch_groups(settings: dict) -> list:
    return [g for g in settings.get("hooks", {}).get("SessionStart", []) if "touch -c" in json.dumps(g)]


def test_touch_hook_is_written_and_not_part_of_the_id(catalog, fake_popen, fake_home):
    popen = fake_popen()
    launch(catalog, Selection(local=["gdrive"]), claude_bin="claude", popen=popen)
    settings_path = Path(popen.calls[0].argv[popen.calls[0].argv.index("--settings") + 1])
    d = settings_path.parent
    written = json.loads(settings_path.read_text())
    assert _touch_groups(written) == [sessions.touch_hook_group(d)]
    base = sessions.without_touch_hook(written)
    assert "hooks" not in base  # gdrive has no companion hooks: only cm's own hook was there
    mcp = json.loads((d / "mcp.json").read_text())
    assert sessions.session_id(mcp, written) == sessions.session_id(mcp, base) == d.name
    assert sessions.with_touch_hook(sessions.with_touch_hook(base, d), d) == written  # idempotent
    assert sessions.without_touch_hook({"hooks": {}}) == {} == sessions.without_touch_hook({"hooks": {"SessionStart": []}})


def test_touch_hook_merges_after_companion_session_start_hooks(catalog, fake_popen):
    popen = fake_popen()
    launch(catalog, Selection(local=["agentmemory"]), claude_bin="claude", popen=popen)
    settings_path = Path(popen.calls[0].argv[popen.calls[0].argv.index("--settings") + 1])
    written = json.loads(settings_path.read_text())
    starts = written["hooks"]["SessionStart"]
    assert starts[0] == {"hooks": [{"type": "command", "command": "node session-start.mjs"}]}  # AgentMemory's first
    assert starts[1] == sessions.touch_hook_group(settings_path.parent) and len(starts) == 2
    assert written["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "node stop.mjs"}]}]


def test_touch_hook_is_silent_quoted_and_never_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)
    d = tmp_path / "it's a \"dir\" with $HOME; rm -rf x"
    d.mkdir()
    os.utime(d, (1, 1))
    cmd = sessions.touch_hook_group(d)["hooks"][0]["command"]
    assert cmd.endswith(sessions.TOUCH_MARKER)
    p = REAL_RUN(["/bin/sh", "-c", cmd], input="{}", capture_output=True, text=True, timeout=10)
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "") and d.stat().st_mtime > 1000
    gone = tmp_path / "gone"
    cmd = sessions.touch_hook_group(gone)["hooks"][0]["command"]
    p = REAL_RUN(["/bin/sh", "-c", cmd], input="{}", capture_output=True, text=True, timeout=10)
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "") and not gone.exists()  # -c: never recreated


def test_preview_does_not_count_cms_own_hook(fake_home, capsys):
    assert cli.main(["--claude-bin", "claude", "--mcp", "gdrive", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Hooks added" not in out  # cm's own hook isn't reported as a companion hook…
    assert "each start or resume marks it used" in out  # …the session line says what it does
    assert '"SessionStart": "1 hook group(s)"' in out


# ---------------------------------------------------------------- F3: cursor session per launch


def test_cursor_session_env_only_without_a_surface(catalog, fake_home, monkeypatch):
    from claude_launcher.plan import build_plan

    p = build_plan(catalog, Selection(cmux=True), claude_bin="claude", owner_pid=4321)
    assert "CM_CUA_SESSION" not in p.env_overrides
    assert p.mcp_doc["mcpServers"]["cmux-cua"]["env"]["CMUX_CUA_DEFAULT_SESSION"] == "cmux-${CMUX_SURFACE_ID:-cm}"
    monkeypatch.delenv("CMUX_SURFACE_ID")
    monkeypatch.setenv("CMUX_SOCKET_PATH", str(fake_home.tmp / "cmux.sock"))
    a = build_plan(catalog, Selection(cmux=True), claude_bin="claude", owner_pid=4321)
    b = build_plan(catalog, Selection(cmux=True), claude_bin="claude", owner_pid=8765)
    assert a.mcp_doc["mcpServers"]["cmux-cua"]["env"]["CMUX_CUA_DEFAULT_SESSION"] == "cmux-${CM_CUA_SESSION:-cm}"
    assert (a.env_overrides["CM_CUA_SESSION"], b.env_overrides["CM_CUA_SESSION"]) == ("4321", "8765")
    assert a.mcp_path == b.mcp_path and a.mcp_doc == b.mcp_doc  # the file stays stable, the value is per launch


# ---------------------------------------------------------------- F4: lock


def test_cleanup_skips_while_another_cm_holds_the_shared_lock(fake_home):
    import fcntl

    root = _sessions(fake_home.home)
    old = _make(root, _sid(1), 100)
    fd = os.open(root / sessions.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)  # another cm inside ensure()
        assert sessions.cleanup(60) == [] and old.exists()
    finally:
        os.close(fd)
    assert sessions.cleanup(60) == [_sid(1)] and not old.exists()
    assert (root / sessions.LOCK_NAME).is_file()  # the lock file itself is never cleaned up


def test_ensure_takes_the_shared_lock(fake_home, monkeypatch):
    import fcntl

    calls = []
    real = fcntl.flock
    monkeypatch.setattr(fcntl, "flock", lambda fd, op: (calls.append(op), real(fd, op))[1])
    sessions.ensure({"mcpServers": {}}, {})
    assert calls == [fcntl.LOCK_SH | fcntl.LOCK_NB]


# ---------------------------------------------------------------- F7


@pytest.mark.skipif(sys.platform != "darwin", reason="Time Machine exclusion is macOS only")
def test_sessions_dir_is_excluded_from_time_machine(fake_home):
    import ctypes
    import plistlib

    d = sessions.ensure({"mcpServers": {}}, {})
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                              ctypes.c_uint32, ctypes.c_int]
    libc.getxattr.restype = ctypes.c_ssize_t
    buf = ctypes.create_string_buffer(256)
    n = libc.getxattr(os.fsencode(d.parent), sessions.BACKUP_XATTR, buf, 256, 0, 0)
    assert n > 0 and plistlib.loads(buf.raw[:n]) == "com.apple.backupd"


def test_backup_exclusion_failure_is_silent(fake_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    sessions.ensure({"mcpServers": {}}, {})  # no xattr attempt, no error
    monkeypatch.setattr(sys, "platform", "darwin")
    import ctypes

    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: (_ for _ in ()).throw(OSError("no libc")))
    sessions.ensure({"mcpServers": {"x": {"command": "y"}}}, {})


# ---------------------------------------------------------------- F9


def test_cm_sessions_dir_override(catalog, fake_popen, fake_home, tmp_path, monkeypatch):
    monkeypatch.setenv("CM_SESSIONS_DIR", str(tmp_path / "local-sessions"))
    popen = fake_popen()
    launch(catalog, Selection(), claude_bin="claude", popen=popen)
    assert Path(popen.calls[0].argv[2].split("=", 1)[1]).parent.parent == tmp_path / "local-sessions"
    assert not _sessions(fake_home.home).exists()


@pytest.mark.parametrize("sub,expected", [
    ("Library/Mobile Documents/com~apple~CloudDocs/cm", "~/Library/Mobile Documents"),
    ("Library/CloudStorage/GoogleDrive-x/cm", "~/Library/CloudStorage"), ("Dropbox/cm", "~/Dropbox"),
    ("Google Drive/cm", "~/Google Drive"), ("OneDrive/sessions", "~/OneDrive"), (".claude-launcher/sessions", None),
    ("Dropboxes/cm", None),
])
def test_synced_folder_detection(fake_home, sub, expected):
    assert sessions.synced_folder(fake_home.home / sub) == expected


def test_where_warns_about_a_synced_sessions_dir(fake_home, capsys, monkeypatch):
    synced = fake_home.home / "Dropbox" / "cm-home"
    synced.mkdir(parents=True)
    monkeypatch.setenv("CM_CONFIG_DIR", str(synced))
    assert cli.main(["--claude-bin", "claude", "--where"]) == 0
    out = capsys.readouterr().out
    assert "inside a synced folder (~/Dropbox)" in out and "Set CM_SESSIONS_DIR to a local directory" in out
    local = fake_home.tmp / "local"
    monkeypatch.setenv("CM_SESSIONS_DIR", str(local))
    assert cli.main(["--claude-bin", "claude", "--where", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["sessions_synced_folder"] is None and doc["sessions_dir_from"] == "CM_SESSIONS_DIR"
    # a symlink into a synced folder counts too
    link = fake_home.tmp / "link-sessions"
    link.symlink_to(synced)
    assert sessions.synced_folder(link) == "~/Dropbox"


# ---------------------------------------------------------------- R2: absolute paths


@pytest.mark.parametrize("var,value,sub", [("CM_CONFIG_DIR", ".cm", ".cm/sessions"),
                                           ("CM_SESSIONS_DIR", "rel/sessions", "rel/sessions")])
def test_relative_dirs_become_absolute_in_argv_and_hook(fake_home, monkeypatch, tmp_path, var, value, sub):
    start = tmp_path / "started-here"
    start.mkdir()
    monkeypatch.chdir(start)
    monkeypatch.setenv(var, value)
    if var == "CM_CONFIG_DIR":
        (start / ".cm").mkdir()
        (start / ".cm" / "config.json").write_text(json.dumps({"mcp_folders": [str(fake_home.home / "extra-mcps")]}))
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: (calls.append(argv), FakeProc(argv))[1])
    assert cli.main(["--claude-bin", "claude", "--cwd", str(fake_home.project), "--mcp", "agentmemory"]) == 0
    argv = calls[0]
    mcp = argv[2].split("=", 1)[1]
    settings = argv[argv.index("--settings") + 1]
    d = start / sub / Path(mcp).parent.name
    assert mcp == f"{d}/mcp.json" and settings == f"{d}/settings.json"
    written = json.loads(Path(settings).read_text())
    assert written["hooks"]["SessionStart"][-1] == sessions.touch_hook_group(d)
    assert str(d) in json.dumps(written) and Path(mcp).is_absolute()


# ---------------------------------------------------------------- R5: lock wait


def test_ensure_never_hangs_on_a_stuck_exclusive_lock(fake_home, monkeypatch):
    import fcntl

    root = _sessions(fake_home.home)
    root.mkdir(parents=True)
    fd = os.open(root / sessions.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    monkeypatch.setattr(sessions, "LOCK_WAIT", 0.2)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # e.g. a cm stopped (Ctrl+Z) in the middle of cleanup
        t = time.monotonic()
        d = sessions.ensure({"mcpServers": {}}, {})
        assert time.monotonic() - t < 2 and (d / "mcp.json").is_file()  # gave up waiting, went on unlocked
    finally:
        os.close(fd)


def test_config_comment_documents_retention_and_hooks_caveat():
    from claude_launcher.config import COMMENT, example_config

    assert all(isinstance(line, str) for line in COMMENT)  # one string per line, never a tuple
    text = "\n".join(example_config()["_comment"])
    assert "session_retention_days" in text and "disableAllHooks" in text and "use 0 there" in text


# ---------------------------------------------------------------- S6: stopped cleanup


def test_cleanup_resumed_after_its_time_budget_deletes_nothing(fake_home, monkeypatch):
    root = _sessions(fake_home.home)
    old = [_make(root, _sid(i), 100) for i in (1, 2)]
    clock = iter([1000.0] + [1000.0 + sessions.LOCK_WAIT] * 10)  # t0, then "resumed" well past LOCK_WAIT / 2
    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    assert sessions.cleanup(60) == []
    assert all(d.exists() and (d / "mcp.json").is_file() for d in old)
    monkeypatch.setattr(time, "monotonic", real_monotonic)
    assert sorted(sessions.cleanup(60)) == [_sid(1), _sid(2)]  # within budget: removed as usual


def test_cleanup_stops_midway_when_the_budget_runs_out(fake_home, monkeypatch):
    root = _sessions(fake_home.home)
    for i in (1, 2, 3):
        _make(root, _sid(i), 100)
    clock = iter([0.0, 0.0] + [sessions.LOCK_WAIT] * 10)  # t0, first delete in budget, then out of time
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    assert len(sessions.cleanup(60)) == 1 and sessions.count() == 2
