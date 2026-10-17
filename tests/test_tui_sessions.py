"""The Session box: Recent selector, Fork, Name and the empty-name nudge."""

import json

from textual.widgets import Checkbox, Input, RadioSet, Select, Static

from claude_launcher import config as config_mod
from claude_launcher import profiles
from claude_launcher.plan import Selection
from claude_launcher.preview import preview_plan
from claude_launcher.tui import NAME_NUDGE, LauncherApp
from tests.conftest import SID, rec_assistant, rec_user, standard_sessions, write_session
from tests.test_tui import drive


async def loaded(app, pilot):
    for _ in range(100):
        if app.recent is not None:
            break
        await pilot.pause(0.02)
    await pilot.pause()
    assert app.recent is not None, "the recent-sessions worker never finished"


async def settle(pilot, n=4):
    for _ in range(n):
        await pilot.pause()


def argv_of(catalog, sel):
    return preview_plan(catalog, sel, "claude").argv


def test_lists_the_last_three_by_activity_with_their_names(catalog, fake_home):
    standard_sessions(fake_home.project)
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        box = app.query_one("#recent", Select)
        assert not box.disabled
        rows = [(str(prompt), value) for prompt, value in box._options if value is not Select.NULL]
        assert [v for _, v in rows] == SID[:3]
        assert rows[0][0] == "FORCE Kafka reconciliation · 2 h ago"
        assert rows[1][0] == "Red build triage · 5 h ago"
        assert rows[2][0].startswith("please summarise the release notes") and rows[2][0].endswith("· 1 d ago")
        assert box.value is Select.NULL  # nothing chosen until I choose
        await pilot.press("escape")

    drive(app, steps)


def test_recent_plus_fork_plus_name_builds_the_argv(catalog, fake_home):
    standard_sessions(fake_home.project)
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        fork = app.query_one("#fork", Checkbox)
        name = app.query_one("#name", Input)
        assert fork.disabled  # New: nothing to fork
        app.query_one("#recent", Select).value = SID[0]
        await settle(pilot)
        assert app.query_one("#session", RadioSet).pressed_button.name == "resume"
        assert app.query_one("#resume_id", Input).value == SID[0]
        assert name.value == "FORCE Kafka reconciliation"  # resume: its current name
        assert not fork.disabled
        fork.value = True
        await settle(pilot)
        assert name.value == "FORCE Kafka reconciliation (fork)"
        preview = str(app.query_one("#preview", Static).render())
        assert 'fork of FORCE Kafka reconciliation → "FORCE Kafka reconciliation (fork)"' in preview
        name.value = "Kafka retry experiment"
        await settle(pilot)
        app.query_one("#session", RadioSet).focus()
        await pilot.press("enter")  # a name is set: launches on the first Enter

    result = drive(app, steps)
    assert result[0] == "launch"
    sel = result[1]
    assert (sel.session, sel.resume_id, sel.fork, sel.name) == ("resume", SID[0], True, "Kafka retry experiment")
    assert argv_of(catalog, sel)[-5:] == ["--fork-session", "--name", "Kafka retry experiment", "--resume", SID[0]]


def test_fork_without_a_title_and_typed_names_are_kept(catalog, fake_home):
    standard_sessions(fake_home.project)
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        name = app.query_one("#name", Input)
        app.query_one("#recent", Select).value = SID[2]  # named only by its first prompt
        await settle(pilot)
        assert name.value == ""  # a shortened prompt is not a name to keep
        app.query_one("#fork", Checkbox).value = True
        await settle(pilot)
        assert name.value == f"fork of {SID[2][:8]}"
        name.value = "my own"
        await settle(pilot)
        app.query_one("#recent", Select).value = SID[1]  # another session: a typed name stays
        await settle(pilot)
        assert name.value == "my own"
        await pilot.press("escape")

    drive(app, steps)


def test_switching_back_to_new_clears_the_choice_and_the_prefilled_name(catalog, fake_home):
    standard_sessions(fake_home.project)
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        app.query_one("#recent", Select).value = SID[1]
        await settle(pilot)
        assert app.query_one("#name", Input).value == "Red build triage"
        for b in app.query_one("#session", RadioSet).query("RadioButton"):
            if b.name == "new":
                b.value = True
        await settle(pilot)
        assert app.query_one("#recent", Select).value is Select.NULL
        assert app.query_one("#name", Input).value == ""
        assert app.query_one("#fork", Checkbox).disabled
        assert app.sel.session == "new" and "--resume" not in argv_of(catalog, app.sel)
        await pilot.press("escape")

    drive(app, steps)


def test_continue_enables_fork_and_typing_a_listed_id_selects_it(catalog, fake_home):
    standard_sessions(fake_home.project)
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        fork = app.query_one("#fork", Checkbox)
        for b in app.query_one("#session", RadioSet).query("RadioButton"):
            if b.name == "continue":
                b.value = True
        await settle(pilot)
        assert not fork.disabled
        fork.value = True
        await settle(pilot)
        assert app.sel.fork and "--fork-session" in argv_of(catalog, app.sel)
        for b in app.query_one("#session", RadioSet).query("RadioButton"):
            if b.name == "resume":
                b.value = True
        await settle(pilot)
        assert fork.disabled and not app.sel.fork  # the picker: nothing to fork yet
        app.query_one("#resume_id", Input).value = SID[1]
        await settle(pilot)
        assert app.query_one("#recent", Select).value == SID[1] and not fork.disabled
        await pilot.press("escape")

    drive(app, steps)


def test_new_session_asks_for_a_name_once(catalog, fake_home):
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        note = app.query_one("#name_note", Static)
        assert not note.display
        app.query_one("#local").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.return_value is None  # not launched
        assert app.focused.id == "name" and note.display and str(note.render()) == NAME_NUDGE
        await pilot.press("enter")  # second Enter: launch without a name

    result = drive(app, steps)
    assert result[0] == "launch" and result[1].name is None
    assert "--name" not in argv_of(catalog, result[1])


def test_new_session_with_a_name_launches_at_once(catalog, fake_home):
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        app.query_one("#name", Input).value = "  Quarterly report  "
        app.query_one("#local").focus()
        await pilot.press("enter")

    result = drive(app, steps)
    assert result[1].name == "Quarterly report"
    assert argv_of(catalog, result[1])[-2:] == ["--name", "Quarterly report"]


def test_typing_in_the_name_box_after_the_nudge_names_it(catalog, fake_home):
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        app.action_do_launch()
        await pilot.pause()
        assert app.focused.id == "name"
        await pilot.press(*"Late name")
        await pilot.pause()
        assert not app.query_one("#name_note", Static).display
        await pilot.press("enter")

    assert drive(app, steps)[1].name == "Late name"


def test_continue_needs_no_name(catalog, fake_home):
    app = LauncherApp(catalog, Selection(session="continue"), claude_bin="claude")

    async def steps(app, pilot):
        app.action_do_launch()  # Continue: the name is optional, no nudge

    result = drive(app, steps)
    assert result[0] == "launch" and result[1].session == "continue" and result[1].name is None


def test_empty_state(catalog, fake_home):
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        box = app.query_one("#recent", Select)
        assert box.disabled and box.prompt == "Recent: none in this project yet"
        await pilot.press("escape")

    drive(app, steps)


def test_unavailable_state(catalog, fake_home):
    write_session(fake_home.project, SID[0], [{"type": "v9-event"}])
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        box = app.query_one("#recent", Select)
        assert box.disabled and "unavailable" in box.prompt
        await pilot.press("escape")

    drive(app, steps)


def test_recent_sessions_is_configurable(catalog, fake_home):
    standard_sessions(fake_home.project)
    config_mod.save_config({"recent_sessions": 2})
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        values = [v for _, v in app.query_one("#recent", Select)._options if v is not Select.NULL]
        assert values == SID[:2]
        await pilot.press("escape")

    drive(app, steps)

    config_mod.save_config({"recent_sessions": 0})
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def hidden(app, pilot):
        await pilot.pause()
        assert not app.query_one("#recent", Select).display and app.recent is None
        await pilot.press("escape")

    drive(app, hidden)


def test_recent_is_a_box_in_the_arrow_flow_and_enter_opens_it(catalog, fake_home):
    standard_sessions(fake_home.project)
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        session = app.query_one("#session", RadioSet)
        session.focus()
        await pilot.pause()
        for _ in range(len(list(session.query("RadioButton")))):
            await pilot.press("down")
            await pilot.pause()
        assert app.focused.id == "recent"
        box = app.query_one("#recent", Select)
        await pilot.press("down")  # a closed Select passes Down on (Fork is disabled for New)
        await pilot.pause()
        assert not box.expanded and app.focused.id == "name"
        await pilot.press("up")
        await pilot.pause()
        assert app.focused.id == "recent"
        await pilot.press("enter")  # opens instead of launching
        await pilot.pause()
        assert box.expanded and app.return_value is None
        await pilot.press("down", "down", "enter")  # blank, first, second -> the second session
        await settle(pilot)
        assert box.value == SID[1] and app.return_value is None
        assert app.query_one("#name", Input).value == "Red build triage"
        await pilot.press("escape")

    drive(app, steps)


def test_initial_resume_id_from_the_cli_is_shown_and_named(catalog, fake_home):
    standard_sessions(fake_home.project)
    # the oldest session is not in the last 3, but its name is still found for the preview and Name
    app = LauncherApp(catalog, Selection(session="resume", resume_id=SID[3]), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        assert app.query_one("#recent", Select).value is Select.NULL
        assert app.query_one("#name", Input).value == "Oldest named"
        assert "resume Oldest named" in str(app.query_one("#preview", Static).render())
        await pilot.press("escape")

    drive(app, steps)

    app = LauncherApp(catalog, Selection(session="resume", resume_id=SID[0], fork=True, name="given"),
                      claude_bin="claude")

    async def listed(app, pilot):
        await loaded(app, pilot)
        await settle(pilot)
        assert app.query_one("#recent", Select).value == SID[0]
        assert app.query_one("#fork", Checkbox).value and app.query_one("#name", Input).value == "given"
        app.action_do_launch()

    sel = drive(app, listed)[1]
    assert (sel.fork, sel.name, sel.resume_id) == (True, "given", SID[0])


def test_profiles_saved_from_the_screen_keep_no_session_choice(catalog, fake_home):
    standard_sessions(fake_home.project)
    app = LauncherApp(catalog, Selection(local=["gdrive"]), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        app.query_one("#recent", Select).value = SID[0]
        await settle(pilot)
        app.query_one("#fork", Checkbox).value = True
        await settle(pilot)
        app.query_one("#profile_name", Input).value = "work"
        app.query_one("#save").press()
        await settle(pilot)
        await pilot.press("escape")

    drive(app, steps)
    doc = json.loads((profiles.profiles_dir() / "work.json").read_text())
    assert "resume_id" not in doc and "fork" not in doc and "name" not in doc
    assert doc["local"] == ["gdrive"]


def test_reading_happens_off_the_ui_thread(catalog, fake_home, monkeypatch):
    import threading

    from claude_launcher import recent

    write_session(fake_home.project, SID[0], [rec_user("x"), rec_assistant()])
    threads = []
    real = recent.list_recent

    def spy(*a, **k):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(*a, **k)

    monkeypatch.setattr(recent, "list_recent", spy)
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await loaded(app, pilot)
        await pilot.press("escape")

    drive(app, steps)
    assert threads == [False]
