"""The Session box's Worktree / worktree name / tmux controls."""

from __future__ import annotations

from textual.widgets import Checkbox, Input, RadioSet, Select, Static

from claude_launcher import profiles
from claude_launcher.plan import Selection
from claude_launcher.preview import preview_plan
from claude_launcher.tui import LauncherApp
from tests.conftest import SID, rec_assistant, rec_custom, rec_user, standard_sessions, write_session
from tests.test_tui import drive
from tests.test_tui_sessions import loaded, settle
from tests.test_worktree import add_worktree


def argv_of(catalog, sel):
    return preview_plan(catalog, sel, "claude").argv


def note(app) -> str:
    n = app.query_one("#wt_note", Static)
    return str(n.render()) if n.display else ""


def test_fork_into_a_renamed_worktree_builds_the_argv(catalog, fake_home):
    standard_sessions(fake_home.project)
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        wt, wt_name, tmux = (app.query_one(f"#{i}") for i in ("worktree", "worktree_name", "tmux"))
        name = app.query_one("#name", Input)
        assert not wt.disabled and wt_name.disabled and tmux.disabled  # nothing ticked yet
        app.query_one("#recent", Select).value = SID[0]
        await settle(pilot)
        app.query_one("#fork", Checkbox).value = True
        wt.value = True
        await settle(pilot)
        assert not wt_name.disabled and not tmux.disabled
        assert name.value == "FORCE Kafka reconciliation (fork)"
        assert wt_name.value == "force-kafka-reconciliation-fork"  # follows Name
        name.value = "X v2"
        await settle(pilot)
        assert wt_name.value == "x-v2"
        preview = str(app.query_one("#preview", Static).render())
        assert 'fork of FORCE Kafka reconciliation → "X v2" in worktree x-v2' in preview
        app.query_one("#session", RadioSet).focus()
        await pilot.press("enter")

    sel = drive(app, steps)[1]
    assert (sel.fork, sel.name, sel.worktree, sel.tmux) == (True, "X v2", "x-v2", False)
    argv = argv_of(catalog, sel)
    assert argv[-7:] == ["--fork-session", "--name", "X v2", "-w", "x-v2", "--resume", SID[0]]
    assert argv.count("--name") == 1


def test_an_edited_worktree_name_stops_following_name(catalog, fake_home):
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        app.query_one("#worktree", Checkbox).value = True
        await settle(pilot)
        wt_name = app.query_one("#worktree_name", Input)
        assert wt_name.value.startswith("cm-") and len(wt_name.value.split("-")) == 3  # unnamed: generated
        app.query_one("#name", Input).value = "Payments refactor"
        await settle(pilot)
        assert wt_name.value == "payments-refactor"
        wt_name.value = "my-branch"
        await settle(pilot)
        app.query_one("#name", Input).value = "Something else"
        await settle(pilot)
        assert wt_name.value == "my-branch"  # typed by hand: kept
        app.query_one("#tmux", Checkbox).value = True
        await settle(pilot)
        app.action_do_launch()

    sel = drive(app, steps)[1]
    assert (sel.worktree, sel.tmux, sel.name) == ("my-branch", True, "Something else")
    assert argv_of(catalog, sel)[-5:] == ["--name", "Something else", "-w", "my-branch", "--tmux"]


def test_tmux_counts_only_with_worktree(catalog, fake_home):
    app = LauncherApp(catalog, Selection(name="n"), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        app.query_one("#worktree", Checkbox).value = True
        await settle(pilot)
        app.query_one("#tmux", Checkbox).value = True
        await settle(pilot)
        app.query_one("#worktree", Checkbox).value = False
        await settle(pilot)
        assert app.query_one("#tmux", Checkbox).disabled
        app.action_do_launch()

    sel = drive(app, steps)[1]
    assert sel.worktree is None and not sel.tmux
    assert "--tmux" not in argv_of(catalog, sel) and "-w" not in argv_of(catalog, sel)


def test_invalid_worktree_name_is_an_error_in_the_preview(catalog, fake_home):
    app = LauncherApp(catalog, Selection(name="n"), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        app.query_one("#worktree", Checkbox).value = True
        await settle(pilot)
        app.query_one("#worktree_name", Input).value = "bad name!"
        await settle(pilot)
        assert not app.plan_ok
        assert "worktree name" in str(app.query_one("#preview", Static).render())
        app.action_do_launch()  # refuses
        await pilot.pause()
        assert app.return_value is None
        await pilot.press("escape")

    drive(app, steps)


def test_outside_a_git_repository_worktree_is_disabled_with_a_reason(fake_home, monkeypatch, tmp_path):
    from claude_launcher.catalog import discover

    plain = tmp_path / "plain"
    plain.mkdir()
    cat = discover(plain)
    app = LauncherApp(cat, Selection(name="n"), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        assert app.query_one("#worktree", Checkbox).disabled
        assert "not inside a git repository" in note(app)
        await pilot.press("escape")

    drive(app, steps)


def test_recent_shows_worktree_sessions_and_resumes_there(catalog, fake_home):
    main = fake_home.project
    a = add_worktree(main, main / ".claude" / "worktrees" / "feat-a")
    write_session(main, SID[0], [rec_user("m", 3), rec_assistant(3), rec_custom("Main thing")], hours_ago=3)
    write_session(a, SID[1], [rec_user("w", 1), rec_assistant(1), rec_custom("Worktree thing")], hours_ago=1)
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        box = app.query_one("#recent", Select)
        rows = [(str(p), v) for p, v in box._options if v is not Select.NULL]
        assert rows[0] == ("Worktree thing · 1 h ago · ⎇ feat-a", SID[1])
        assert rows[1][1] == SID[0] and "⎇" not in rows[1][0]
        box.value = SID[1]
        await settle(pilot)
        assert app.sel.launch_dir == str(a)
        assert "runs in feat-a" in str(app.query_one("#preview", Static).render())
        app.action_do_launch()

    sel = drive(app, steps)[1]
    assert sel.launch_dir == str(a) and sel.resume_id == SID[1]


def test_profiles_never_keep_worktree_or_tmux(catalog, fake_home):
    app = LauncherApp(catalog, Selection(local=["gdrive"], name="n"), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        app.query_one("#worktree", Checkbox).value = True
        await settle(pilot)
        app.query_one("#tmux", Checkbox).value = True
        await settle(pilot)
        app.query_one("#profile_name", Input).value = "wt"
        app.query_one("#save").press()
        await settle(pilot)
        await pilot.press("escape")

    drive(app, steps)
    import json

    doc = json.loads((profiles.profiles_dir() / "wt.json").read_text())
    assert not {"worktree", "tmux", "launch_dir"} & set(doc)
    assert profiles.load("wt").worktree is None


def test_existing_worktree_warns_and_needs_a_second_enter(catalog, fake_home):
    main = fake_home.project
    add_worktree(main, main / ".claude" / "worktrees" / "kept", branch="worktree-kept")
    app = LauncherApp(catalog, Selection(name="Kept"), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        app.query_one("#worktree", Checkbox).value = True
        await settle(pilot)
        assert app.query_one("#worktree_name", Input).value == "kept"
        assert "kept: exists: claude reuses it" in note(app)
        assert "exists: claude reuses it" in str(app.query_one("#preview", Static).render())
        app.query_one("#local").focus()
        await pilot.press("enter")  # first Enter: the warning becomes a confirm prompt
        await pilot.pause()
        assert app.return_value is None and app.focused.id == "worktree_name"
        assert "Enter again to launch anyway" in note(app)
        await pilot.press("enter")  # second Enter launches

    sel = drive(app, steps)[1]
    assert sel.worktree == "kept"


def test_changing_the_name_after_the_prompt_asks_again_or_launches(catalog, fake_home):
    main = fake_home.project
    (main / ".git" / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (main / ".git" / "refs" / "heads" / "worktree-left").write_text("0" * 40)
    app = LauncherApp(catalog, Selection(name="n"), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        app.query_one("#worktree", Checkbox).value = True
        await settle(pilot)
        app.query_one("#worktree_name", Input).value = "left"
        await settle(pilot)
        assert "git worktree add -B" in note(app)
        app.action_do_launch()
        await pilot.pause()
        assert app.return_value is None
        app.query_one("#worktree_name", Input).value = "fresh"  # a new name: nothing exists, no prompt
        await settle(pilot)
        assert note(app) == ""
        app.action_do_launch()

    assert drive(app, steps)[1].worktree == "fresh"


def test_a_name_claude_would_refuse_cannot_launch(catalog, fake_home):
    (fake_home.project / ".claude" / "worktrees" / "junk").mkdir(parents=True)
    app = LauncherApp(catalog, Selection(name="n"), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        app.query_one("#worktree", Checkbox).value = True
        await settle(pilot)
        app.query_one("#worktree_name", Input).value = "junk"
        await settle(pilot)
        assert not app.plan_ok and "not a git worktree" in str(app.query_one("#preview", Static).render())
        app.action_do_launch()
        await pilot.pause()
        assert app.return_value is None
        await pilot.press("escape")

    drive(app, steps)


def test_continue_disables_worktree_with_a_reason(catalog, fake_home):
    app = LauncherApp(catalog, Selection(session="continue"), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        wt = app.query_one("#worktree", Checkbox)
        assert wt.disabled and "not with Continue" in note(app)
        wt.value = True  # even if ticked, it doesn't count
        await settle(pilot)
        assert app.sel.worktree is None
        app.action_do_launch()

    sel = drive(app, steps)[1]
    assert sel.worktree is None and "-w" not in argv_of(catalog, sel)


def test_tmux_only_with_new_or_fork(catalog, fake_home, monkeypatch):
    standard_sessions(fake_home.project)
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        app.query_one("#recent", Select).value = SID[0]
        app.query_one("#worktree", Checkbox).value = True
        await settle(pilot)
        tmux = app.query_one("#tmux", Checkbox)
        assert tmux.disabled and "only for a new session or a Fork" in note(app)
        app.query_one("#fork", Checkbox).value = True
        await settle(pilot)
        assert not tmux.disabled
        tmux.value = True
        await settle(pilot)
        app.action_do_launch()

    sel = drive(app, steps)[1]
    assert sel.tmux and sel.fork
    assert argv_of(catalog, sel)[-8:] == ["--fork-session", "--name", "FORCE Kafka reconciliation (fork)", "-w",
                                          "force-kafka-reconciliation-fork", "--tmux", "--resume", SID[0]]


def test_tmux_not_installed_is_explained(catalog, fake_home, monkeypatch):
    from claude_launcher import worktree

    monkeypatch.setattr(worktree, "tmux_available", lambda: False)
    app = LauncherApp(catalog, Selection(name="n"), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        app.query_one("#worktree", Checkbox).value = True
        await settle(pilot)
        assert app.query_one("#tmux", Checkbox).disabled and "tmux: not installed" in note(app)
        await pilot.press("escape")

    drive(app, steps)
