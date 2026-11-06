"""The last session per project, resume by name, the Extra args line and Enter on closed lists.

Fake HOME and fixture transcripts only: claude is never started (the screen and `launch` are
faked as in test_remember.py, or the real screen is driven with Textual's pilot)."""

import json

import pytest
from textual.widgets import Input, RadioSet, Select, Static

from claude_launcher import cli, config, profiles, recent, remember
from claude_launcher.paths import tilde
from claude_launcher.plan import PlanError, Selection, build_plan
from claude_launcher.preview import preview_plan, session_line
from claude_launcher.tui import LauncherApp
from tests.conftest import SID, rec_assistant, rec_custom, rec_user, standard_sessions, write_session
from tests.test_remember import last_files, run, screen, write_last  # noqa: F401 - `screen` is a fixture
from tests.test_tui import drive
from tests.test_tui_sessions import loaded, settle

U = "9b2f4c1e-5d3a-4e8b-9c7d-0123456789ab"


def argv(catalog, sel):
    return preview_plan(catalog, sel, "claude").argv


def doc_of(home):
    [f] = last_files(home)
    return json.loads(f.read_text())


def talked(cwd, sid, name=None, *, future=True):
    """A transcript as claude would leave it; `future`: its activity is after "now" (i.e. after
    the launch that is being simulated)."""
    h = -0.001 if future else 1.0
    recs = [rec_user("hello there", h), rec_assistant(h)]
    if name:
        recs.append(rec_custom(name))
    return write_session(cwd, sid, recs)


def carried_on(cwd, sid):
    """New activity appended to an existing transcript (a resume or --continue)."""
    f = recent.projects_root() / recent.project_key(cwd) / f"{sid}.jsonl"
    with open(f, "a") as fh:
        for r in (rec_user("one more thing", -0.001), rec_assistant(-0.001)):
            fh.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------- plan: no --session-id


@pytest.mark.parametrize("sel", [
    Selection(), Selection(name="X"), Selection(session="continue"), Selection(session="continue", fork=True),
    Selection(session="resume", resume_id=SID[0]), Selection(session="resume", resume_id=SID[0], fork=True, name="F"),
    Selection(session="resume"),
])
def test_cm_never_passes_a_session_id(catalog, sel):
    # a cmux restore appends --resume <id>, which claude refuses next to --session-id
    assert not any(a.startswith("--session-id") for a in argv(catalog, sel))
    assert "session_id" not in Selection.__dataclass_fields__


@pytest.mark.parametrize("sel, msg", [
    (Selection(session="continue", extra_args=["-r", SID[0]]), "chosen twice"),
    (Selection(session="resume", resume_id=SID[0], extra_args=["--continue"]), "chosen twice"),
    (Selection(session="resume", resume_id=SID[0], extra_args=["--session-id", U]), "only together with Fork"),
])
def test_session_choice_rules_for_forwarded_args(catalog, sel, msg):
    with pytest.raises(PlanError, match=msg):
        build_plan(catalog, sel, claude_bin="claude")


def test_a_users_own_session_id_after_dash_dash_is_forwarded(catalog):
    a = argv(catalog, Selection(extra_args=["--session-id", U]))
    assert a[-2:] == ["--session-id", U]  # theirs, as given; cm adds none


def test_existing_session_line():
    titles = {SID[0]: "backoffice"}
    assert session_line(Selection(session="resume", resume_id=SID[0], name="backoffice"), titles) == \
        "resume backoffice (existing session)"
    assert session_line(Selection(session="resume", resume_id=SID[0], name=" BackOffice "), titles) == \
        'resume backoffice (existing session) → "BackOffice"'
    assert session_line(Selection(session="resume", resume_id=SID[0], name="other"), titles) == \
        'resume backoffice → "other"'


# ---------------------------------------------------------------------------- remember.py


def test_roundtrip_with_session_and_extra_args(fake_home):
    sel = Selection(local=["gdrive"], extra_args=["--add-dir", "../lib"])
    assert remember.save(fake_home.project, sel, remember.LastSession(U, "Release", "/wt/x")) is None
    entry, warns = remember.load(fake_home.project)
    assert not warns and entry.selection.extra_args == ["--add-dir", "../lib"]
    assert entry.session == remember.LastSession(U, "Release", "/wt/x")
    doc = doc_of(fake_home.home)
    assert doc["last_session"] == {"id": U, "name": "Release", "dir": "/wt/x"}
    assert not {"fork", "worktree", "tmux"} & set(doc["last_session"])

    assert remember.save(fake_home.project, sel, remember.LastSession(U)) is None
    assert doc_of(fake_home.home)["last_session"] == {"id": U}
    assert remember.save(fake_home.project, sel) is None
    assert "last_session" not in doc_of(fake_home.home)


@pytest.mark.parametrize("raw", ["continue", 3, {"id": "../etc"}, {"id": ""}, {"name": "x"}, []])
def test_an_odd_last_session_is_ignored_not_fatal(fake_home, raw):
    write_last(fake_home.project, {"local": ["gdrive"]}, last_session=raw)
    entry, warns = remember.load(fake_home.project)
    assert not warns and entry.session is None and entry.selection.local == ["gdrive"]


def test_bad_extra_args_make_the_entry_corrupt(fake_home):
    write_last(fake_home.project, {"local": ["gdrive"], "extra_args": "--verbose"})
    entry, warns = remember.load(fake_home.project)
    assert entry is None and "extra_args" in warns[0]


# ---------------------------------------------------------------------------- cli: the last session


class Claude:
    """What the fake screen returns, and what claude leaves behind while the fake launch runs."""


@pytest.fixture
def cm(fake_home, monkeypatch, screen):  # noqa: F811
    """`screen` (test_remember.py), plus: the screen's result may be a function of the kwargs it was
    opened with (to use the generated --session-id), and `cm.during` runs while "claude" runs."""
    import claude_launcher.tui as tui

    cm = Claude()
    cm.screen, cm.during, cm.result = screen, None, None

    def fake_tui(cat, sel, **kw):
        screen.opened.append((sel, kw))
        return cm.result(sel, kw) if callable(cm.result) else cm.result

    def fake_launch(cat, sel, *, claude_bin, on_started=None):
        screen.launched.append((sel, on_started is not None))
        if on_started:
            on_started()
        if cm.during:
            cm.during(sel)
        return 0

    monkeypatch.setattr(tui, "run_tui", fake_tui)
    monkeypatch.setattr(cli, "launch", fake_launch)
    return cm


def opened(cm):
    return cm.screen.opened[-1]


def test_a_new_session_is_found_by_the_file_it_created(fake_home, cm):
    p = fake_home.project
    standard_sessions(p)  # older sessions are there before the launch: not new
    cm.result = Selection(local=["gdrive"], name="Release prep")
    cm.during = lambda sel: talked(p, U, "Release prep")  # what claude writes while it runs
    assert run() == 0
    launched = cm.screen.launched[-1][0]
    assert "--session-id" not in argv(_cat(p), launched)
    assert doc_of(fake_home.home)["last_session"] == {"id": U, "name": "Release prep"}

    cm.result = None
    assert run() == 0
    sel, kw = opened(cm)
    assert (sel.session, sel.resume_id, sel.local) == ("resume", U, ["gdrive"])
    where = tilde(remember.project_root(p))
    assert kw["prefill"] == f"Pre-filled from your last launch in {where} · last session: Release prep (Ctrl+N clears)"
    assert kw["last_session"] == U and "session_id" not in kw


def test_a_new_session_in_a_brand_new_project_dir(fake_home, cm):
    # no ~/.claude/projects/<key> before the launch: claude creates it
    p = fake_home.project
    assert not recent.project_dirs(p)
    cm.result = Selection(name="First")
    cm.during = lambda sel: talked(p, U)
    assert run() == 0
    assert doc_of(fake_home.home)["last_session"] == {"id": U, "name": "First"}  # cm's --name when no title


def test_two_new_files_store_nothing(fake_home, cm):
    p = fake_home.project
    cm.result = Selection(name="X")

    def two(sel):
        talked(p, U, "X")
        talked(p, SID[3], "Another one, from another claude meanwhile")

    cm.during = two
    assert run() == 0
    assert "last_session" not in doc_of(fake_home.home)  # no guessing


def test_no_new_file_stores_nothing(fake_home, cm):
    cm.result = Selection(name="X")  # e.g. left before saying anything: claude keeps no transcript
    assert run() == 0
    assert "last_session" not in doc_of(fake_home.home)


def test_the_diff_ignores_subagents_and_is_read_only(fake_home):
    p = fake_home.project
    standard_sessions(p)
    before = recent.snapshot([p])
    d = recent.projects_root() / recent.project_key(p)
    stat_before = sorted((f.name, f.stat().st_mtime_ns) for f in d.iterdir())
    (d / "agent-123.jsonl").write_text("{}\n")  # a subagent transcript
    (d / U).mkdir()  # a sidecar directory
    assert recent.new_session(before) is None
    talked(p, U, "Real")
    found = recent.new_session(before)
    assert found.id == U and found.title == "Real" and found.cwd is None
    assert sorted((f.name, f.stat().st_mtime_ns) for f in d.iterdir() if f.name in dict(stat_before)) == stat_before


def test_an_incomplete_snapshot_never_guesses(fake_home, monkeypatch):
    p = fake_home.project
    monkeypatch.setattr(recent, "MAX_SNAPSHOT_FILES", 2)
    standard_sessions(p)  # 4 files: over the bound
    before = recent.snapshot([p])
    assert not before.complete
    talked(p, U)
    assert recent.new_session(before) is None


def _cat(project):
    from claude_launcher.catalog import discover

    return discover(project)


def test_a_resumed_session_is_remembered(fake_home, cm):
    standard_sessions(fake_home.project)
    cm.result = Selection(session="resume", resume_id=SID[1], name="Red build triage")
    assert run() == 0
    assert "--session-id" not in argv(_cat(fake_home.project), cm.screen.launched[-1][0])
    assert doc_of(fake_home.home)["last_session"] == {"id": SID[1], "name": "Red build triage"}
    cm.result = None
    assert run() == 0
    assert opened(cm)[0].resume_id == SID[1]


def test_continue_that_appends_to_its_transcript_stores_nothing(fake_home, cm):
    p = fake_home.project
    standard_sessions(p)
    cm.result = Selection(session="continue")
    cm.during = lambda sel: carried_on(p, SID[0])  # no new file: cm does not guess which one it was
    assert run() == 0
    assert "last_session" not in doc_of(fake_home.home)


def test_continue_that_creates_a_new_file_is_remembered(fake_home, cm):
    p = fake_home.project
    standard_sessions(p)
    cm.result = Selection(session="continue")
    cm.during = lambda sel: talked(p, U, "Kafka, continued")
    assert run() == 0
    assert doc_of(fake_home.home)["last_session"] == {"id": U, "name": "Kafka, continued"}
    cm.result = None
    assert run() == 0
    sel, kw = opened(cm)
    assert sel.session == "resume" and sel.resume_id == U
    assert "last session: Kafka, continued" in kw["prefill"]


def test_continue_without_new_activity_stores_no_session(fake_home, cm):
    standard_sessions(fake_home.project)
    cm.result = Selection(session="continue")  # nothing written while it ran (all older than the launch)
    assert run() == 0
    assert "last_session" not in doc_of(fake_home.home)
    cm.result = None
    assert run() == 0
    assert opened(cm)[0].session == "new"


def test_a_fork_stores_its_own_id_and_never_the_fork(fake_home, cm):
    p = fake_home.project
    standard_sessions(p)
    fork_id = U
    cm.result = Selection(session="resume", resume_id=SID[0], fork=True, name="Kafka v2")
    cm.during = lambda sel: talked(p, fork_id, "Kafka v2")
    assert run() == 0
    a = argv(_cat(p), cm.screen.launched[-1][0])
    assert a[-5:] == ["--fork-session", "--name", "Kafka v2", "--resume", SID[0]]
    doc = doc_of(fake_home.home)
    assert doc["last_session"] == {"id": fork_id, "name": "Kafka v2"}
    assert "fork" not in json.dumps(doc["selection"])
    cm.result = None
    assert run() == 0
    sel, _ = opened(cm)
    assert (sel.session, sel.resume_id, sel.fork) == ("resume", fork_id, False)


def test_a_vanished_session_is_dropped_with_a_note(fake_home, cm):
    write_last(fake_home.project, {"local": ["gdrive"]}, last_session={"id": U, "name": "Gone one"})
    assert run() == 0
    sel, kw = opened(cm)
    assert sel.session == "new" and sel.resume_id is None and sel.local == ["gdrive"]
    assert "last session 'Gone one' no longer exists; starting a new session" in kw["notes"]
    assert "last session" not in kw["prefill"] and kw["last_session"] is None


def test_a_session_of_another_checkout_is_resumed_there(fake_home, cm, monkeypatch):
    from claude_launcher import worktree as wt_mod

    wt = fake_home.tmp / "work" / "proj-wt"
    wt.mkdir()
    monkeypatch.setattr(wt_mod, "other_checkouts", lambda cwd: [wt_mod.Checkout(path=wt, label="proj-wt")]
                        if cwd.resolve() != wt.resolve() else [])
    talked(wt, SID[2], "In the worktree", future=False)
    write_last(fake_home.project, {}, last_session={"id": SID[2], "dir": str(wt)})
    assert run() == 0
    sel, kw = opened(cm)
    assert sel.session == "resume" and sel.resume_id == SID[2]
    assert "last session: In the worktree" in kw["prefill"]

    cm.result = sel  # launched as pre-filled: cli resumes it from that checkout and remembers the dir
    assert run() == 0
    launched = cm.screen.launched[-1][0]
    assert launched.launch_dir == str(wt)
    assert doc_of(fake_home.home)["last_session"] == {"id": SID[2], "name": "In the worktree", "dir": str(wt)}


def test_a_new_session_in_a_worktree_claude_creates(fake_home, cm, monkeypatch):
    p = fake_home.project
    wt = p / ".claude" / "worktrees" / "spike"
    cm.result = Selection(name="Spike", worktree="spike")

    def claude_w(sel):  # claude -w spike: the worktree and its session appear while claude runs
        wt.mkdir(parents=True)
        talked(wt, U, "Spike")

    cm.during = claude_w
    assert run() == 0
    assert doc_of(fake_home.home)["last_session"] == {"id": U, "name": "Spike", "dir": str(wt)}


def test_a_new_session_of_a_resumed_worktree_session_fork(fake_home, cm, monkeypatch):
    from claude_launcher import worktree as wt_mod

    wt = fake_home.tmp / "work" / "proj-wt"
    wt.mkdir()
    monkeypatch.setattr(wt_mod, "other_checkouts", lambda cwd: [wt_mod.Checkout(path=wt, label="proj-wt")]
                        if cwd.resolve() != wt.resolve() else [])
    talked(wt, SID[2], "In the worktree", future=False)
    cm.result = Selection(session="resume", resume_id=SID[2], fork=True, name="WT fork")
    cm.during = lambda sel: talked(wt, U, "WT fork")  # the fork runs in that checkout
    assert run() == 0
    assert cm.screen.launched[-1][0].launch_dir == str(wt)
    assert doc_of(fake_home.home)["last_session"] == {"id": U, "name": "WT fork", "dir": str(wt)}


def test_flags_and_profiles_keep_their_session(fake_home, cm):
    standard_sessions(fake_home.project)
    write_last(fake_home.project, {"local": ["gdrive"]}, last_session={"id": SID[1]})
    assert run("-i", "--continue") == 0  # a selection flag: the last entry isn't read at all
    assert opened(cm)[0].session == "continue"
    profiles.save("c", Selection(session="continue"))
    assert run("-i", "--profile", "c") == 0
    assert opened(cm)[0].session == "continue"
    assert run("--", "--add-dir", "x") == 0  # extra args alone keep the pre-selection
    sel, _ = opened(cm)
    assert sel.session == "resume" and sel.resume_id == SID[1] and sel.extra_args == ["--add-dir", "x"]


def test_remember_last_session_false(fake_home, cm, monkeypatch):
    p = fake_home.project
    config.save_config({"remember_last_session": False})
    monkeypatch.setattr(recent, "snapshot", lambda places: pytest.fail("no snapshot when nothing is remembered"))
    cm.result = Selection(name="X")
    cm.during = lambda sel: talked(p, SID[0], "X")
    assert run() == 0
    doc = doc_of(fake_home.home)
    assert "last_session" not in doc and doc["selection"]  # the rest is still remembered
    write_last(p, {}, last_session={"id": SID[0]})
    cm.result = None
    assert run() == 0
    assert opened(cm)[0].session == "new"


def test_remember_last_session_must_be_a_boolean(fake_home, cm, capsys):
    config.save_config({"remember_last_session": "yes"})
    cm.result = Selection(name="X")
    cm.during = lambda sel: talked(fake_home.project, U, "X")
    assert run() == 0
    assert "remember_last_session must be true or false" in capsys.readouterr().err
    assert doc_of(fake_home.home)["last_session"]["id"] == U  # warned, stays on


def test_headless_and_dry_run_never_use_it(fake_home, cm, capsys):
    p = fake_home.project
    standard_sessions(p)
    w = write_last(p, {"local": ["gdrive"]}, last_session={"id": SID[0]})
    before = w.read_bytes()
    assert run("--mcp", "gdrive") == 0
    sel = cm.screen.launched[-1][0]
    assert sel.session == "new"
    assert run("--dry-run", "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert "--session-id" not in doc["argv"] and "--resume" not in doc["argv"]
    assert w.read_bytes() == before and not cm.screen.opened


# ---------------------------------------------------------------------------- cli: resume by name


def named_sessions(p):
    write_session(p, SID[0], [rec_user("a", 30), rec_assistant(30), rec_custom("backoffice")])
    write_session(p, SID[1], [rec_user("b", 3), rec_assistant(3), rec_custom("  BackOffice ")])
    write_session(p, SID[2], [rec_user("c", 1), rec_assistant(1), {"type": "ai-title", "aiTitle": "backoffice"}])


def test_name_resumes_the_session_of_that_name(fake_home, cm):
    named_sessions(fake_home.project)
    write_last(fake_home.project, {"local": ["gdrive"]}, last_session={"id": SID[2]})
    assert run("--name", "backoffice", "--", "--add-dir", "X") == 0
    sel, kw = opened(cm)
    # the most recent one with that custom name; an AI title doesn't count, nor does the last session
    assert (sel.session, sel.resume_id, sel.name) == ("resume", SID[1], "backoffice")
    assert sel.extra_args == ["--add-dir", "X"] and sel.local == ["gdrive"]
    assert "last session" not in kw["prefill"]
    titles = {SID[1]: "BackOffice"}
    assert session_line(sel, titles) == 'resume BackOffice (existing session) → "backoffice"'
    a = argv(_cat(fake_home.project), sel)
    assert a[-6:] == ["--add-dir", "X", "--name", "backoffice", "--resume", SID[1]]


def test_name_without_a_match_is_a_new_session(fake_home, cm):
    named_sessions(fake_home.project)
    write_last(fake_home.project, {}, last_session={"id": SID[0]})
    assert run("--name", "frontoffice") == 0
    sel, _ = opened(cm)
    assert sel.session == "new" and sel.name == "frontoffice"  # not the last session either


def test_name_matches_other_checkouts_and_headless_never_resumes(fake_home, cm, monkeypatch):
    from claude_launcher import worktree as wt_mod

    wt = fake_home.tmp / "work" / "proj-wt"
    wt.mkdir()
    monkeypatch.setattr(wt_mod, "other_checkouts", lambda cwd: [wt_mod.Checkout(path=wt, label="proj-wt")]
                        if cwd.resolve() != wt.resolve() else [])
    write_session(wt, SID[3], [rec_user("x", 2), rec_assistant(2), rec_custom("backoffice")])
    assert recent.find_by_name(fake_home.project, " BACKOFFICE ").id == SID[3]
    assert run("--name", "backoffice") == 0
    assert opened(cm)[0].resume_id == SID[3]
    assert run("--mcp", "gdrive", "--name", "backoffice") == 0  # headless: today's semantics
    sel = cm.screen.launched[-1][0]
    assert sel.session == "new" and sel.name == "backoffice"


def test_find_by_name_edges(fake_home):
    assert recent.find_by_name(fake_home.project, "x") is None  # no sessions at all
    named_sessions(fake_home.project)
    assert recent.find_by_name(fake_home.project, "   ") is None
    assert recent.find_by_name(fake_home.project, "backoffice", limit=1) is None  # only the newest looked at


# ---------------------------------------------------------------------------- cli: extra args


def test_extra_args_are_remembered_and_replaced_by_cli_ones(fake_home, cm):
    cm.result = Selection(name="n", extra_args=["--add-dir", "../lib"])
    assert run() == 0
    assert doc_of(fake_home.home)["selection"]["extra_args"] == ["--add-dir", "../lib"]
    cm.result = None
    assert run() == 0
    assert opened(cm)[0].extra_args == ["--add-dir", "../lib"]
    assert run("--", "--verbose") == 0
    assert opened(cm)[0].extra_args == ["--verbose"]  # replaced for that launch
    entry, _ = remember.load(fake_home.project)
    assert entry.selection.extra_args == ["--add-dir", "../lib"]  # Esc: the file is unchanged


@pytest.mark.parametrize("extra", [["--name", "x"], ["-w", "wt"], ["--resume", SID[0]], ["-c"],
                                   ["--session-id", U]])
def test_per_launch_extra_args_are_not_remembered(fake_home, cm, extra):
    cm.result = Selection(session="new", extra_args=["--verbose", *extra])
    assert run() == 0
    assert doc_of(fake_home.home)["selection"]["extra_args"] == []
    assert cm.screen.launched[-1][0].extra_args == ["--verbose", *extra]  # launched with them all


# ---------------------------------------------------------------------------- the screen


def test_extra_args_line_is_editable_and_validated(catalog, fake_home):
    app = LauncherApp(catalog, Selection(name="n", extra_args=["--add-dir", "my dir"]), claude_bin="claude")

    async def steps(app, pilot):
        box = app.query_one("#extra_args", Input)
        assert box.value == "--add-dir 'my dir'"
        box.value = "--add-dir ../lib --verbose"
        await settle(pilot)
        assert app.sel.extra_args == ["--add-dir", "../lib", "--verbose"]
        assert "--add-dir ../lib --verbose" in str(app.query_one("#preview", Static).render())

        box.value = "--add-dir 'unclosed"
        await settle(pilot)
        pv = app.query_one("#preview", Static)
        assert not app.plan_ok and "Extra args: No closing quotation" in str(pv.render())
        box.focus()
        await pilot.press("enter")  # blocked
        await pilot.pause()
        assert app.return_value is None

        box.value = "--name other"  # Name is already set by cm
        await settle(pilot)
        assert not app.plan_ok and "named twice" in str(app.query_one("#preview", Static).render())
        app.action_do_launch()
        await pilot.pause()
        assert app.return_value is None

        box.value = "--add-dir ../lib"
        await settle(pilot)
        assert app.plan_ok
        await pilot.press("enter")  # Enter in the box launches

    result = drive(app, steps)
    assert result[0] == "launch" and result[1].extra_args == ["--add-dir", "../lib"]


@pytest.mark.parametrize("extra, session, msg", [
    ("--resume x", "continue", "chosen twice"),
    ("-w wt --tmux", None, None),  # fine on its own: cm's Worktree is off
])
def test_extra_args_session_and_worktree_duplicates(catalog, fake_home, extra, session, msg):
    app = LauncherApp(catalog, Selection(session=session or "new", name="n"), claude_bin="claude")

    async def steps(app, pilot):
        app.query_one("#extra_args", Input).value = extra
        await settle(pilot)
        if msg:
            assert not app.plan_ok and msg in str(app.query_one("#preview", Static).render())
        else:
            assert app.plan_ok and app.query_one("#worktree").disabled  # Worktree: given after --
        await pilot.press("escape")

    drive(app, steps)


def test_loading_a_profile_fills_the_extra_args_line(catalog, fake_home):
    profiles.save("w", Selection(local=["gdrive"], extra_args=["--add-dir", "../w"]))
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        app.query_one("#profile", Select).value = "w"
        await settle(pilot)
        assert app.query_one("#extra_args", Input).value == "--add-dir ../w"
        assert app.sel.extra_args == ["--add-dir", "../w"]
        await pilot.press("escape")

    drive(app, steps)


def test_the_screen_never_shows_a_session_id(catalog, fake_home):
    standard_sessions(fake_home.project)
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        pv = lambda: str(app.query_one("#preview", Static).render())  # noqa: E731
        assert "--session-id" not in pv()
        app.query_one("#recent", Select).value = SID[0]
        app.query_one("#fork").value = True
        await settle(pilot)
        assert "--session-id" not in pv() and "--fork-session" in pv() and f"--resume {SID[0]}" in pv()
        app.action_do_launch()

    sel = drive(app, steps)[1]
    assert sel.fork and sel.resume_id == SID[0]


def test_prefilled_last_session_ctrl_n_and_new(catalog, fake_home):
    standard_sessions(fake_home.project)
    note = "Pre-filled from your last launch in ~/work/proj · last session: Red build triage (Ctrl+N clears)"
    sel = Selection(local=["gdrive"], session="resume", resume_id=SID[1])

    async def ctrl_n(app, pilot):
        await loaded(app, pilot)
        await settle(pilot)
        assert app.query_one("#recent", Select).value == SID[1]
        assert app.query_one("#name", Input).value == "Red build triage"
        assert "resume Red build triage" in str(app.query_one("#preview", Static).render())
        await pilot.press("ctrl+n")
        await settle(pilot)
        assert app.query_one("#session", RadioSet).pressed_button.name == "new"
        assert app.query_one("#recent", Select).value is Select.NULL
        assert app.query_one("#name", Input).value == ""
        assert not app.query_one("#prefill", Static).display
        assert "--resume" not in str(app.query_one("#preview", Static).render())
        await pilot.press("escape")

    drive(LauncherApp(catalog, Selection.from_dict(sel.to_dict()), claude_bin="claude", prefill=note,
                      last_session=SID[1]), ctrl_n)

    async def choose_new(app, pilot):
        await loaded(app, pilot)
        await settle(pilot)
        for b in app.query_one("#session", RadioSet).query("RadioButton"):
            if b.name == "new":
                b.value = True
        await settle(pilot)
        app.query_one("#name", Input).value = "Fresh"
        await settle(pilot)
        app.action_do_launch()

    result = drive(LauncherApp(catalog, Selection.from_dict(sel.to_dict()), claude_bin="claude", prefill=note,
                               last_session=SID[1]), choose_new)[1]
    assert (result.session, result.resume_id, result.name) == ("new", None, "Fresh")


def test_ctrl_n_leaves_a_resume_i_chose_myself(catalog, fake_home):
    standard_sessions(fake_home.project)
    app = LauncherApp(catalog, Selection(session="resume", resume_id=SID[0]), claude_bin="claude",
                      last_session=SID[1])

    async def steps(app, pilot):
        await loaded(app, pilot)
        await pilot.press("ctrl+n")
        await settle(pilot)
        assert app.sel.session == "resume" and app.sel.resume_id == SID[0]
        await pilot.press("escape")

    drive(app, steps)


def test_enter_on_the_closed_recent_list_launches(catalog, fake_home):
    standard_sessions(fake_home.project)
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        box = app.query_one("#recent", Select)
        box.value = SID[1]
        await settle(pilot)
        box.focus()
        await pilot.pause()
        await pilot.press("space")  # Space opens it
        await pilot.pause()
        assert box.expanded
        await pilot.press("escape")  # closes the list, not the screen
        await pilot.pause()
        assert not box.expanded and app.return_value is None
        box.focus()
        await pilot.pause()
        await pilot.press("enter")  # closed: launches (a resume needs no name)

    result = drive(app, steps)
    assert result[0] == "launch" and result[1].resume_id == SID[1]


def test_enter_on_the_closed_recent_list_still_asks_for_a_name(catalog, fake_home):
    standard_sessions(fake_home.project)
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        app.query_one("#recent", Select).focus()
        await pilot.pause()
        await pilot.press("enter")  # New with an empty name: asks once first
        await pilot.pause()
        assert app.return_value is None and app.focused.id == "name"
        await pilot.press("escape")

    drive(app, steps)


def test_hint_line_mentions_space_for_lists(catalog, fake_home):
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        hint = str(app.query_one("#hint", Static).render())
        assert "Space toggle / open a list" in hint and "Enter launch" in hint
        await pilot.press("escape")

    drive(app, steps)


def test_config_comment_documents_remember_last_session():
    assert any("remember_last_session" in line for line in config.COMMENT)


@pytest.mark.parametrize("args", [
    ["--add-dir", "/x", "--dangerously-skip-permissions"],
    ["--allow-dangerously-skip-permissions"],
    ["--permission-mode", "bypassPermissions"],
    ["--permission-mode=acceptEdits"],
    ["--fork-session"],
])
def test_permission_and_fork_args_are_never_remembered(args):
    # like cm's own Skip permissions and Fork: never pre-filled from the last launch
    assert cli.remembered_extra_args(args) == []
    assert cli.remembered_extra_args(["--add-dir", "/x"]) == ["--add-dir", "/x"]


def test_a_fork_after_dash_dash_is_not_recorded_as_the_resumed_session():
    sel = Selection(session="resume", resume_id=SID[0], extra_args=["--fork-session"])
    assert cli.known_session(sel) is None  # the fork runs a new id: left to the file diff


def test_a_session_started_in_a_sibling_worktree_is_never_taken_for_this_launch(fake_home):
    from tests.test_worktree import add_worktree

    main = fake_home.project
    other = add_worktree(main, main / ".claude" / "worktrees" / "other")
    before = recent.snapshot([main])
    write_session(other, SID[1], [rec_user("busy elsewhere", 0.1, cwd=str(other)), rec_assistant(0.1)])
    assert recent.new_session(before) is None  # only this launch's own places count
    write_session(main, SID[2], [rec_user("mine", 0.05, cwd=str(main)), rec_assistant(0.05)])
    found = recent.new_session(before)
    assert found and found.id == SID[2]
