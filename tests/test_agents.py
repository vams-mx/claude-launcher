"""The Agent selector (--agent): discovery, argv rules, the plugin an agent needs, CLI, profiles,
the last selection and the screen. Fake HOME only; claude is never started."""

import json

import pytest
from textual.widgets import Select, SelectionList, Static

from claude_launcher import cli, profiles, remember
from claude_launcher.catalog import discover
from claude_launcher.frontmatter import parse
from claude_launcher.plan import PlanError, Selection, build_plan
from claude_launcher.preview import preview_plan
from claude_launcher.tui import DEFAULT_AGENT, LauncherApp
from tests.conftest import SID, _w
from tests.test_remember import run, screen  # noqa: F401 - `screen` is a fixture
from tests.test_tui import drive
from tests.test_tui_sessions import settle


def agent_md(path, name=None, description=None, extra=""):
    fm = ["---"]
    if name is not None:
        fm.append(f"name: {name}")
    if description is not None:
        fm.append(f"description: {description}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm) + "\n" + extra + "---\nYou are an agent.\n")
    return path


@pytest.fixture
def agents(fake_home):
    h, p = fake_home.home, fake_home.project
    agent_md(h / ".claude" / "agents" / "reviewer.md", "reviewer", "Reviews code (user copy)")
    agent_md(h / ".claude" / "agents" / "writer.md", None, "'Writes docs: carefully'")  # name from the stem
    (h / ".claude" / "agents" / "broken.md").write_text("---\nname: [unclosed\nno closing marker\n")
    (h / ".claude" / "agents" / "notes.txt").write_text("not an agent")
    agent_md(p / ".claude" / "agents" / "reviewer.md", "reviewer", "Reviews code (project copy)")
    agent_md(h / "plug" / "commit" / "agents" / "helper.md", "helper", ">\n  Folded\n  description", extra="")
    # a plugin installed only for another project, with an agent
    ip = h / ".claude" / "plugins" / "installed_plugins.json"
    doc = json.loads(ip.read_text())
    doc["plugins"]["team@m"] = [{"scope": "local", "projectPath": "/elsewhere", "installPath": str(h / "plug" / "team")}]
    ip.write_text(json.dumps(doc))
    agent_md(h / "plug" / "team" / "agents" / "coordinator.md", "coordinator", "Coordinates")
    return fake_home


def test_frontmatter_parser_is_tolerant():
    assert parse("---\nname: a\ndescription: \"x: y\"\n---\nbody") == {"name": "a", "description": "x: y"}
    assert parse("---\ndescription: >\n  one\n  two\nmodel: opus\n---\n") == {"description": "one two",
                                                                            "model": "opus"}
    assert parse("no frontmatter") == {} and parse("---\nname: a\n") == {}  # unclosed
    assert parse("---\n- a list\n: odd\nname: ok\n---") == {"name": "ok"}


def test_discovery_sources_precedence_and_plugin_names(agents):
    cat = discover(agents.project)
    by = {a.name: a for a in cat.agents}
    assert by["reviewer"].source == "project" and "project copy" in by["reviewer"].description  # shadows the user one
    assert by["writer"].source == "user" and by["writer"].description == "Writes docs: carefully"
    assert by["broken"].source == "user"  # bad frontmatter: name from the file stem
    assert by["commit-commands:helper"].plugin_id == "commit-commands@official"
    assert by["commit-commands:helper"].description == "Folded description"
    assert by["team:coordinator"].plugin_id == "team@m" and not by["team:coordinator"].installed_here
    assert "notes" not in by
    assert cat.settings_agent is None


def test_the_agent_setting_names_the_default(agents):
    _w(agents.project / ".claude" / "settings.local.json",
       {"enabledPlugins": {"dbtools@x": True}, "agent": "writer"})
    cat = discover(agents.project)
    assert cat.settings_agent == "writer"
    plan = preview_plan(cat, Selection(), "claude")
    assert "--agent" not in plan.argv  # Default: claude applies the setting itself
    from claude_launcher.preview import agent_line

    assert agent_line(cat, Selection()) == "default (writer from settings)"


def test_argv_agent_goes_before_resume(agents):
    cat = discover(agents.project)
    a = build_plan(cat, Selection(agent="reviewer", session="resume", resume_id=SID[0], name="R",
                                  extra_args=["--add-dir", "x"]), claude_bin="claude").argv
    assert a[-8:] == ["--agent", "reviewer", "--add-dir", "x", "--name", "R", "--resume", SID[0]]
    assert a.index("--agent") < a.index("--add-dir") < a.index("--resume") and a[-2] == "--resume"
    assert a.count("--agent") == 1


@pytest.mark.parametrize("extra", [["--agent", "writer"], ["--agent=writer"]])
def test_an_agent_after_dash_dash_too_is_a_duplicate(agents, extra):
    cat = discover(agents.project)
    with pytest.raises(PlanError, match="agent is given twice"):
        build_plan(cat, Selection(agent="reviewer", extra_args=extra), claude_bin="claude")
    # alone it is just forwarded; --agents (definitions) never counts
    assert build_plan(cat, Selection(extra_args=extra), claude_bin="claude").argv[-len(extra):] == extra
    build_plan(cat, Selection(agent="reviewer", extra_args=["--agents", "{}"]), claude_bin="claude")


def test_a_plugin_agent_needs_its_plugin_and_this_project(agents):
    cat = discover(agents.project)
    with pytest.raises(PlanError, match="tick that plugin"):
        build_plan(cat, Selection(agent="commit-commands:helper"), claude_bin="claude")
    plan = build_plan(cat, Selection(agent="commit-commands:helper", plugins=["commit-commands@official"]),
                      claude_bin="claude")
    assert plan.settings_doc["enabledPlugins"]["commit-commands@official"] is True
    with pytest.raises(PlanError, match="installed for another project only"):
        build_plan(cat, Selection(agent="team:coordinator"), claude_bin="claude")


def test_an_unknown_agent_warns_and_a_bad_name_is_refused(agents):
    cat = discover(agents.project)
    plan = build_plan(cat, Selection(agent="ghost"), claude_bin="claude")
    assert plan.argv[plan.argv.index("--agent") + 1] == "ghost"
    assert any("agent 'ghost' was not found" in w for w in plan.warnings)
    with pytest.raises(PlanError, match="invalid agent name"):
        build_plan(cat, Selection(agent="-x"), claude_bin="claude")


def test_cli_agent_flag_ticks_the_plugin(agents, capsys):
    assert cli.main(["--claude-bin", "claude", "--agent", "commit-commands:helper", "--plugins", "none",
                     "--dry-run", "--json"]) == 0
    out = capsys.readouterr()
    doc = json.loads(out.out)
    assert doc["argv"][doc["argv"].index("--agent") + 1] == "commit-commands:helper"
    assert doc["settings"]["enabledPlugins"]["commit-commands@official"] is True
    assert "enabled it for this session" in out.err
    assert cli.main(["--claude-bin", "claude", "--agent", "default", "--dry-run", "--json"]) == 0
    assert "--agent" not in json.loads(capsys.readouterr().out)["argv"]


def test_cli_blocked_agent_and_duplicate(agents, capsys):
    assert cli.main(["--claude-bin", "claude", "--agent", "team:coordinator", "--dry-run"]) == 2
    assert "installed for another project only" in capsys.readouterr().err
    assert cli.main(["--claude-bin", "claude", "--agent", "reviewer", "--dry-run", "--", "--agent", "x"]) == 2
    assert "agent is given twice" in capsys.readouterr().err


def test_profiles_and_the_last_selection_keep_the_agent(agents, screen):  # noqa: F811
    cat = discover(agents.project)
    profiles.save("rev", Selection(agent="reviewer"))
    assert profiles.load("rev").agent == "reviewer"
    sel, notes = profiles.reconcile(Selection(agent="gone"), cat)
    assert sel.agent is None and "agent 'gone' no longer found" in notes[0]
    sel, notes = profiles.reconcile(Selection(agent="commit-commands:helper"), cat)
    assert sel.plugins == ["commit-commands@official"] and "ticked it" in notes[0]
    sel, notes = profiles.reconcile(Selection(agent="team:coordinator"), cat)
    assert sel.agent is None and "another project" in notes[0]

    screen.result = Selection(agent="writer", name="n")
    assert run() == 0
    entry, _ = remember.load(agents.project)
    assert entry.selection.agent == "writer"
    screen.result = None
    assert run() == 0
    assert screen.opened[-1][0].agent == "writer"


def test_screen_agent_box(agents):
    _w(agents.project / ".claude" / "settings.local.json",
       {"enabledPlugins": {"dbtools@x": True}, "agent": "writer"})
    cat = discover(agents.project)
    app = LauncherApp(cat, Selection(name="n"), claude_bin="claude")

    async def steps(app, pilot):
        box = app.query_one("#agent", Select)
        assert box.value == DEFAULT_AGENT
        assert "Default (writer from settings)" in str(box._options[0][0])
        values = [v for _, v in box._options]
        assert {"reviewer", "writer", "commit-commands:helper", "team:coordinator"} <= set(values)
        pv = lambda: str(app.query_one("#preview", Static).render())  # noqa: E731
        assert "Agent: default (writer from settings)" in pv()

        box.value = "commit-commands:helper"
        await settle(pilot)
        assert "commit-commands@official" in app.query_one("#plugins", SelectionList).selected  # ticked for it
        assert "--agent commit-commands:helper" in pv() and "needs plugin commit-commands@official" in pv()

        box.value = "team:coordinator"
        await settle(pilot)
        assert not app.plan_ok and "installed for another project only" in pv()
        app.action_do_launch()
        await pilot.pause()
        assert app.return_value is None  # blocked

        box.value = "reviewer"
        await settle(pilot)
        box.focus()
        await pilot.pause()
        await pilot.press("enter")  # a closed list: Enter launches

    result = drive(app, steps)
    assert result[0] == "launch" and result[1].agent == "reviewer"


def test_screen_loads_a_profiles_agent(agents):
    profiles.save("rev", Selection(agent="writer"))
    app = LauncherApp(discover(agents.project), Selection(), claude_bin="claude")

    async def steps(app, pilot):
        app.query_one("#profile", Select).value = "rev"
        await settle(pilot)
        assert app.query_one("#agent", Select).value == "writer" and app.sel.agent == "writer"
        await pilot.press("escape")

    drive(app, steps)


def test_list_shows_agents_and_skill_packs(agents, capsys):
    from claude_launcher import skillpacks

    d = skillpacks.packs_dir() / "web" / "skills" / "s"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n")
    assert cli.main(["--claude-bin", "claude", "--list", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    by = {a["name"]: a for a in doc["agents"]}
    assert by["reviewer"]["source"] == "project" and not by["team:coordinator"]["available"]
    assert doc["skill_packs"][0]["name"] == "web" and doc["skill_packs"][0]["skills"] == 1
    assert cli.main(["--claude-bin", "claude", "--list"]) == 0
    out = capsys.readouterr().out
    assert "skill pack" in out and "agent" in out
