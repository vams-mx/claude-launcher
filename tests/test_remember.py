"""The last selection per project (remember.py) and how `cm` uses it. Fake HOME only."""

import asyncio
import json
import os
import stat
import subprocess

import pytest
from textual.widgets import SelectionList, Static

from claude_launcher import cli, config, profiles, remember
from claude_launcher.paths import tilde
from claude_launcher.plan import Selection
from claude_launcher.tui import LauncherApp


# ---------------------------------------------------------------------------- helpers


@pytest.fixture
def screen(fake_home, monkeypatch):
    """`cm` with a TTY and a fake screen: records what the screen was opened with and returns
    `screen.result` (a Selection, or None for Esc). Launches go through a fake `launch` that runs
    the on_started hook like a real start."""
    import claude_launcher.tui as tui

    class S:
        opened = []  # (initial selection, kwargs)
        launched = []
        result = None

    s = S()
    monkeypatch.setattr(cli, "_has_tty", lambda: True)

    def fake_tui(cat, sel, **kw):
        s.opened.append((sel, kw))
        return s.result

    def fake_launch(cat, sel, *, claude_bin, on_started=None):
        s.launched.append((sel, on_started is not None))
        if on_started:
            on_started()
        return 0

    monkeypatch.setattr(tui, "run_tui", fake_tui)
    monkeypatch.setattr(cli, "launch", fake_launch)
    return s


def run(*args):
    return cli.main(["--claude-bin", "claude", *args])


def repo(base, *parts):
    d = base.joinpath(*parts)
    (d / ".git").mkdir(parents=True)
    return d


def last_files(home):
    d = home / ".claude-launcher" / "last"
    return sorted(d.glob("*.json")) if d.is_dir() else []


def write_last(project, selection, **extra):
    root = remember.project_root(project)
    p = remember.entry_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"format": 1, "project": str(root), "saved_at": "2026-10-17T00:00:00+00:00", "selection": selection}
    doc.update(extra)
    p.write_text(json.dumps(doc))
    return p


# ---------------------------------------------------------------------------- project key


def test_project_root_is_the_git_root_else_the_directory(fake_home):
    a = fake_home.project  # has a .git directory
    sub = a / "src" / "deep"
    sub.mkdir(parents=True)
    assert remember.project_root(a) == a.resolve()
    assert remember.project_root(sub) == a.resolve()
    plain = fake_home.tmp / "plain" / "dir"
    plain.mkdir(parents=True)
    assert remember.project_root(plain) == plain.resolve()
    assert remember.key(a.resolve()) != remember.key(plain.resolve())
    assert len(remember.key(a.resolve())) == remember.KEY_LEN


def test_project_root_follows_symlinks_to_the_same_key(fake_home):
    link = fake_home.tmp / "link-to-proj"
    link.symlink_to(fake_home.project)
    assert remember.project_root(link) == remember.project_root(fake_home.project)


def test_linked_worktree_belongs_to_its_main_checkout(fake_home):
    main = repo(fake_home.tmp, "src", "main")
    gitdir = main / ".git" / "worktrees" / "feature"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    wt = fake_home.tmp / "src" / "main-feature"
    (wt / "pkg").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {gitdir}\n")
    assert remember.project_root(wt / "pkg") == main.resolve()


def test_worktree_of_a_bare_repository_uses_the_repository(fake_home):
    bare = fake_home.tmp / "src" / "thing.git"
    gitdir = bare / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    wt = fake_home.tmp / "src" / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../thing.git/worktrees/wt\n")  # relative, like git writes it
    assert remember.project_root(wt) == bare.resolve()


def test_submodule_and_odd_git_files_are_their_own_project(fake_home):
    sup = repo(fake_home.tmp, "super")
    modgit = sup / ".git" / "modules" / "lib"
    modgit.mkdir(parents=True)  # no commondir: not a worktree
    lib = sup / "lib"
    lib.mkdir()
    (lib / ".git").write_text("gitdir: ../.git/modules/lib\n")
    assert remember.project_root(lib) == lib.resolve()
    odd = fake_home.tmp / "odd"
    odd.mkdir()
    (odd / ".git").write_text("not a gitdir line\n")
    assert remember.project_root(odd) == odd.resolve()


def test_a_dotfiles_repo_at_home_does_not_swallow_every_directory(fake_home):
    (fake_home.home / ".git").mkdir(parents=True)
    d = fake_home.home / "notes" / "x"
    d.mkdir(parents=True)
    assert remember.project_root(d) == d.resolve()
    assert remember.project_root(fake_home.home) == fake_home.home.resolve()


# ---------------------------------------------------------------------------- acceptance


def test_remembered_per_project_and_shared_by_subdirectories(fake_home, screen):
    a = fake_home.project
    screen.result = Selection(local=["agentmemory", "gdrive"], model="sonnet")
    assert run() == 0
    first_sel, first_kw = screen.opened[-1]
    assert first_sel.local == [] and first_kw["prefill"] is None
    assert screen.launched[-1][1]  # the screen's launch carries the hook

    [f] = last_files(fake_home.home)
    doc = json.loads(f.read_text())
    assert doc["project"] == str(a.resolve()) and f.name == f"{remember.key(a.resolve())}.json"

    screen.result = None  # Esc: only look at what the screen opened with
    assert run() == 0
    sel, kw = screen.opened[-1]
    assert sel.local == ["agentmemory", "gdrive"] and sel.model == "sonnet"
    assert kw["prefill"] == f"Pre-filled from your last launch in {tilde(a.resolve())} (Ctrl+N clears)"
    assert kw["profile"] is None

    sub = a / "src" / "pkg"
    sub.mkdir(parents=True)
    assert run("--cwd", str(sub)) == 0
    sel, kw = screen.opened[-1]
    assert sel.local == ["agentmemory", "gdrive"] and kw["prefill"]

    b = repo(fake_home.tmp, "work", "other")
    assert run("--cwd", str(b)) == 0
    sel, kw = screen.opened[-1]
    assert sel.local == [] and sel.model == "default" and kw["prefill"] is None


def test_file_layout_permissions_and_never_saved_fields(fake_home, screen):
    screen.result = Selection(local=["gdrive"], connectors=["claude.ai Slack"], plugins=["commit-commands@official"],
                              cmux=True, model="opus", permissions="skip", session="resume",
                              resume_id="abc-123", fork=True, name="Release prep", extra_args=["--verbose"])
    assert run() == 0
    [f] = last_files(fake_home.home)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    doc = json.loads(f.read_text())
    assert doc["selection"] == {"local": ["gdrive"], "connectors": ["claude.ai Slack"],
                                "plugins": ["commit-commands@official"], "cmux": True, "model": "opus",
                                "permissions": "standard"}
    assert not [p for p in f.parent.iterdir() if p.name.endswith(".tmp")]  # atomic write left nothing behind

    screen.result = None
    assert run() == 0
    sel, _ = screen.opened[-1]
    assert sel.permissions == "standard" and sel.session == "new"
    assert sel.resume_id is None and sel.fork is False and sel.name is None and sel.extra_args == []
    assert sel.cmux and sel.model == "opus" and sel.connectors == ["claude.ai Slack"]


def test_skip_permissions_in_a_hand_edited_file_still_prefills_standard(fake_home, screen):
    write_last(fake_home.project, {"local": ["gdrive"], "permissions": "skip"},
               permissions="skip", session="continue", resume_id="x", fork=True, name="n")
    assert run() == 0
    sel, kw = screen.opened[-1]
    assert sel.local == ["gdrive"] and sel.permissions == "standard" and sel.session == "new"
    assert sel.resume_id is None and not sel.fork and sel.name is None and kw["prefill"]


def test_respects_cm_config_dir(fake_home, screen, monkeypatch):
    cfg = fake_home.tmp / "cfg"
    monkeypatch.setenv("CM_CONFIG_DIR", str(cfg))
    screen.result = Selection(local=["gdrive"])
    assert run() == 0
    assert list((cfg / "last").glob("*.json")) and not last_files(fake_home.home)


# ---------------------------------------------------------------------------- precedence


def test_precedence_flags_profile_last_default_empty(fake_home, screen):
    profiles.save("g", Selection(local=["gdrive"], model="haiku"))
    profiles.save("m", Selection(local=["agentmemory"]))
    profiles.set_default("g")

    screen.result = None
    assert run() == 0  # no last yet: the default profile
    sel, kw = screen.opened[-1]
    assert sel.local == ["gdrive"] and kw["profile"] == "g" and kw["prefill"] is None

    screen.result = Selection(local=["agentmemory", "gdrive"], model="sonnet")
    assert run() == 0  # launched from the screen: remembered

    screen.result = None
    assert run() == 0  # last beats the default profile
    sel, kw = screen.opened[-1]
    assert sel.local == ["agentmemory", "gdrive"] and sel.model == "sonnet" and kw["prefill"]

    assert run("-i", "--profile", "m") == 0  # --profile beats last
    sel, kw = screen.opened[-1]
    assert sel.local == ["agentmemory"] and sel.model == "default" and kw["prefill"] is None
    assert kw["profile"] == "m"

    assert run("-i", "--profile", "m", "--model", "opus") == 0  # flags override profile fields
    assert screen.opened[-1][0].model == "opus"

    assert run("-i", "--mcp", "gdrive") == 0  # explicit flags beat last (and the default profile)
    sel, kw = screen.opened[-1]
    assert sel.local == ["gdrive"] and sel.model == "default" and kw["prefill"] is None

    profiles.set_default(None)
    assert remember.forget(fake_home.project)[2]
    assert run() == 0  # neither: empty
    sel, kw = screen.opened[-1]
    assert sel.local == [] and kw["prefill"] is None and kw["profile"] is None


def test_name_and_passthrough_keep_the_prefill(fake_home, screen):
    write_last(fake_home.project, {"local": ["gdrive"]})
    assert run("--name", "From flag", "--", "--verbose") == 0
    sel, kw = screen.opened[-1]
    assert sel.local == ["gdrive"] and sel.name == "From flag" and sel.extra_args == ["--verbose"]
    assert kw["prefill"]


def test_headless_never_reads_or_writes(fake_home, screen, capsys):
    profiles.save("g", Selection(local=["gdrive"]))
    assert run("--mcp", "agentmemory") == 0
    assert run("--profile", "g") == 0
    assert run("--no-mcp", "--continue") == 0
    assert not last_files(fake_home.home)
    assert all(not hook for _, hook in screen.launched)  # no hook passed to headless launches

    p = write_last(fake_home.project, {"local": ["agentmemory"]})
    before = p.read_bytes()
    assert run("--profile", "g") == 0
    assert screen.launched[-1][0].local == ["gdrive"]  # not the last selection
    assert p.read_bytes() == before
    assert not screen.opened  # never opened the screen

    assert run("--dry-run", "--json") == 0  # a preview isn't the screen either
    doc = json.loads(capsys.readouterr().out)
    assert doc["mcp"]["mcpServers"] == {}
    assert p.read_bytes() == before


def test_escape_writes_nothing(fake_home, screen):
    screen.result = None
    assert run() == 0
    assert not last_files(fake_home.home) and not screen.launched


def test_claude_not_started_writes_nothing(fake_home, monkeypatch, fake_popen):
    import claude_launcher.tui as tui

    monkeypatch.setattr(cli, "_has_tty", lambda: True)
    monkeypatch.setattr(tui, "run_tui", lambda cat, sel, **kw: Selection(local=["gdrive"]))

    def missing(*a, **k):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(subprocess, "Popen", missing)
    assert run() == 127
    assert not last_files(fake_home.home)

    popen = fake_popen(rc=3)  # started (and exited 3): remembered
    monkeypatch.setattr(subprocess, "Popen", popen)
    assert run() == 3
    assert popen.calls and len(last_files(fake_home.home)) == 1


def test_remember_last_false_turns_it_off(fake_home, screen, capsys):
    p = write_last(fake_home.project, {"local": ["gdrive"]})
    before = p.read_bytes()
    config.save_config({"remember_last": False})
    screen.result = Selection(local=["agentmemory"])
    assert run() == 0
    sel, kw = screen.opened[-1]
    assert sel.local == [] and kw["prefill"] is None
    assert screen.launched[-1][1] is False  # no hook: nothing written
    assert p.read_bytes() == before
    other = repo(fake_home.tmp, "work", "b")
    assert run("--cwd", str(other)) == 0
    assert last_files(fake_home.home) == [p]

    config.save_config({"remember_last": "nope"})  # not a boolean: warns, stays on
    screen.result = None
    assert run() == 0
    assert "remember_last must be true or false" in capsys.readouterr().err
    assert screen.opened[-1][0].local == ["gdrive"]


def test_forget_last(fake_home, capsys):
    p = write_last(fake_home.project, {"local": ["gdrive"]})
    assert run("--forget-last") == 0
    assert f"forgot the last selection for {tilde(fake_home.project.resolve())}" in capsys.readouterr().out
    assert not p.exists()
    assert run("--forget-last") == 0
    assert "no last selection remembered" in capsys.readouterr().out


def test_forget_last_only_touches_this_project(fake_home):
    other = repo(fake_home.tmp, "work", "b")
    pa = write_last(fake_home.project, {"local": ["gdrive"]})
    pb = write_last(other, {"local": ["agentmemory"]})
    assert run("--cwd", str(other / "."), "--forget-last") == 0
    assert pa.exists() and not pb.exists()


def test_where_shows_the_file(fake_home, capsys):
    assert run("--where") == 0
    out = capsys.readouterr().out
    assert "last launch   none for" in out
    p = write_last(fake_home.project, {"local": ["gdrive"]})
    assert run("--where") == 0
    out = capsys.readouterr().out
    assert f"last launch   {tilde(p)}" in out and tilde(fake_home.project.resolve()) in out
    assert run("--where", "--json") == 0
    doc = json.loads(capsys.readouterr().out)["last_selection"]
    assert doc["exists"] and doc["enabled"] and doc["file"] == tilde(p)
    config.save_config({"remember_last": False})
    assert run("--where") == 0
    assert "remember_last is off" in capsys.readouterr().out


# ---------------------------------------------------------------------------- robustness


def test_vanished_items_are_dropped_with_a_note(fake_home, screen):
    write_last(fake_home.project, {"local": ["agentmemory", "ghost"], "connectors": ["claude.ai Slack", "claude.ai Gone"],
                                   "plugins": ["commit-commands@official", "gone@x"], "model": "turbo"})
    assert run() == 0
    sel, kw = screen.opened[-1]
    assert sel.local == ["agentmemory"] and sel.connectors == ["claude.ai Slack"]
    assert sel.plugins == ["commit-commands@official"] and sel.model == "default"
    notes = kw["notes"]
    assert "last launch: MCP server 'ghost' no longer found" in notes
    assert "last launch: connector 'claude.ai Gone' no longer found" in notes
    assert "last launch: plugin 'gone@x' not enabled/installed here" in notes
    assert "last launch: model 'turbo' is unknown; using Default" in notes
    assert kw["prefill"]


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    '{"format": 1, "project": "PROJECT", "selection": {"local": "gdrive"}}',
    '{"format": 1, "project": "PROJECT", "selection": {"cmux": "yes"}}',
    '{"format": 99, "project": "PROJECT", "selection": {}}',
    '{"format": 1, "project": "/somewhere/else", "selection": {"local": ["gdrive"]}}',
    b"\xff\xfe garbage",
])
def test_corrupt_file_is_ignored_with_one_warning(fake_home, screen, capsys, content):
    profiles.save("g", Selection(local=["gdrive"]))
    profiles.set_default("g")
    root = remember.project_root(fake_home.project)
    p = remember.entry_path(root)
    p.parent.mkdir(parents=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content.replace("PROJECT", str(root)))
    screen.result = Selection(local=["agentmemory"])
    assert run() == 0
    sel, kw = screen.opened[-1]
    assert sel.local == ["gdrive"] and kw["prefill"] is None and kw["profile"] == "g"  # the default profile
    err = capsys.readouterr().err
    assert err.count("ignoring the last selection") == 1
    assert json.loads(p.read_text())["selection"]["local"] == ["agentmemory"]  # replaced by the launch


@pytest.mark.skipif(os.getuid() == 0, reason="root reads anything")
def test_unreadable_file_warns_and_continues(fake_home, screen, capsys):
    p = write_last(fake_home.project, {"local": ["gdrive"]})
    p.chmod(0)
    try:
        assert run() == 0
    finally:
        p.chmod(0o600)
    assert screen.opened[-1][1]["prefill"] is None
    assert capsys.readouterr().err.count("cannot read the last selection") == 1


def test_write_failure_never_fails_the_launch(fake_home, screen, capsys):
    blocker = fake_home.home / ".claude-launcher" / "last"
    blocker.write_text("a file where the directory should be")
    screen.result = Selection(local=["gdrive"])
    assert run() == 0
    assert screen.launched
    err = capsys.readouterr().err
    assert err.count("could not remember this selection") == 1


def test_save_and_load_roundtrip_is_independent_of_cli(fake_home):
    sub = fake_home.project / "a"
    sub.mkdir()
    assert remember.save(sub, Selection(local=["x"], model="haiku", permissions="skip")) is None
    entry, warns = remember.load(fake_home.project)
    assert not warns and entry.selection.local == ["x"] and entry.selection.model == "haiku"
    assert entry.selection.permissions == "standard" and entry.project == fake_home.project.resolve()


def test_loose_directory_permissions_are_tightened(fake_home):
    d = remember.last_dir()
    d.mkdir(parents=True, mode=0o755)
    d.chmod(0o755)
    assert remember.save(fake_home.project, Selection()) is None
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_config_comment_documents_remember_last():
    assert any("remember_last" in line for line in config.COMMENT)


# ---------------------------------------------------------------------------- the screen


def drive(app, steps):
    async def go():
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            await steps(app, pilot)

    asyncio.run(go())
    return app.return_value


NOTE = "Pre-filled from your last launch in ~/work/proj (Ctrl+N clears)"


def test_screen_shows_the_note_and_ctrl_n_clears(catalog):
    app = LauncherApp(catalog, Selection(local=["agentmemory", "gdrive"], model="sonnet"), claude_bin="claude",
                      prefill=NOTE)

    async def steps(app, pilot):
        note = app.query_one("#prefill", Static)
        assert note.display and NOTE in str(note.render())
        assert "agentmemory, gdrive" in str(app.query_one("#preview", Static).render())  # preview as usual
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert not note.display
        assert app.query_one("#local", SelectionList).selected == []
        await pilot.press("escape")

    drive(app, steps)


def test_screen_without_prefill_has_no_note(catalog):
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        assert not app.query_one("#prefill", Static).display
        await pilot.press("escape")

    drive(app, steps)


def test_loading_a_profile_hides_the_note(catalog):
    from textual.widgets import Select

    profiles.save("work", Selection(local=["gdrive"]))
    app = LauncherApp(catalog, Selection(local=["agentmemory"]), claude_bin="claude", prefill=NOTE)

    async def steps(app, pilot):
        app.query_one("#profile", Select).value = "work"
        await pilot.pause()
        assert not app.query_one("#prefill", Static).display
        assert app.query_one("#local", SelectionList).selected == ["gdrive"]
        await pilot.press("escape")

    drive(app, steps)


def test_arrows_and_enter_still_work_with_the_note(catalog):
    app = LauncherApp(catalog, Selection(local=["gdrive"]), claude_bin="claude", prefill=NOTE)

    async def steps(app, pilot):
        assert app.focused.id == "local"
        await pilot.press("up")  # the note is not a focus stop
        assert app.focused.id == "local"
        await pilot.press("enter")  # asks for a name once
        await pilot.pause()
        assert app.focused.id == "name"
        await pilot.press("enter")

    result = drive(app, steps)
    assert result[0] == "launch" and result[1].local == ["gdrive"]


def test_worktree_and_tmux_are_never_remembered(fake_home, screen):
    screen.result = Selection(local=["gdrive"], name="Wt", worktree="wt", tmux=True)
    assert run() == 0
    assert screen.launched[-1][0].worktree == "wt"  # it was launched with them
    [f] = last_files(fake_home.home)
    doc = json.loads(f.read_text())
    assert not {"worktree", "tmux", "launch_dir"} & set(doc["selection"])
    # even a hand-edited file can't bring them back
    write_last(fake_home.project, {"local": ["gdrive"], "worktree": "x", "tmux": True, "launch_dir": "/tmp"},
               worktree="x", tmux=True)
    screen.result = None
    assert run() == 0
    sel, _ = screen.opened[-1]
    assert sel.local == ["gdrive"] and sel.worktree is None and not sel.tmux and sel.launch_dir is None
