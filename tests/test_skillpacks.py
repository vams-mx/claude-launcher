"""Skill packs: per-session skills via --add-dir. Discovery, argv, the layout move, `cm skills`,
profiles / last selection, costs and the screen. Fake HOME only; nothing real is installed."""

import json
import os
import shutil

import pytest
from textual.widgets import SelectionList, Static

from claude_launcher import cli, costs, launcher, profiles, remember, skillpacks
from claude_launcher.catalog import discover
from claude_launcher.plan import PlanError, Selection, build_plan
from claude_launcher.preview import preview_plan
from claude_launcher.tui import LauncherApp
from tests.conftest import SID
from tests.test_remember import run, screen  # noqa: F401 - `screen` is a fixture
from tests.test_tui import drive
from tests.test_tui_sessions import settle


def skill(d, name, description="Does a thing"):
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\n" + (f"description: {description}\n" if description else "") + "---\n# body\n"
    (d / "SKILL.md").write_text(fm)
    return d


@pytest.fixture
def packs(fake_home, monkeypatch):
    root = skillpacks.packs_dir()
    pw = root / "playwright"
    skill(pw / ".claude" / "skills" / "playwright-cli", "playwright-cli", "Drive a browser with playwright-cli")
    (pw / "pack.json").write_text(json.dumps({"description": "Browser automation", "requires": [
        {"command": "playwright-cli", "install_hint": "npm i -g @playwright/cli"}, {"command": "sh"},
        {"command": "rm -rf /"}]}))
    docs = root / "docs"
    # a pack made by cm 0.5.0-0.5.2: skills/ (the plugin layout), moved to .claude/skills at launch
    skill(docs / "skills" / "a", "alpha", "First")
    skill(docs / "skills" / "b", "beta", None)  # no description: a note
    (docs / "skills" / "not-a-skill").mkdir()
    (docs / ".claude-plugin").mkdir()
    (docs / ".claude-plugin" / "plugin.json").write_text('{"name": "docs", "description": "Old docs pack"}')
    bad = root / "broken"  # no skills at all (and an unreadable old manifest, now ignored)
    (bad / ".claude-plugin").mkdir(parents=True)
    (bad / ".claude-plugin" / "plugin.json").write_text("{nope")
    (root / "Bad Name").mkdir()
    real_which = shutil.which
    monkeypatch.setattr(skillpacks.shutil, "which", lambda c: None if c == "playwright-cli" else real_which(c))
    return fake_home


def test_discovery(packs):
    warnings = []
    found = {p.name: p for p in skillpacks.discover(warnings)}
    assert set(found) == {"playwright", "docs", "broken"}
    assert any("Bad Name" in w for w in warnings)
    pw = found["playwright"]
    assert pw.description == "Browser automation" and [s.name for s in pw.skills] == ["playwright-cli"]
    assert [r.command for r in pw.missing] == ["playwright-cli"] and pw.requires[1].found
    assert pw.warnings()[0] == "needs playwright-cli (not found): npm i -g @playwright/cli"
    assert any("no valid command" in n for n in pw.notes)  # "rm -rf /" is not a command name
    assert pw.estimated_tokens() > 0
    d = found["docs"]
    assert [s.name for s in d.skills] == ["alpha", "beta"] and any("beta has no description" in n for n in d.notes)
    assert d.legacy and d.description == "Old docs pack" and not pw.legacy
    assert found["broken"].error and "no skills found" in found["broken"].error


def test_argv_and_rules(packs):
    cat = discover(packs.project)
    a = build_plan(cat, Selection(skill_packs=["playwright", "docs"], session="resume", resume_id=SID[0],
                                  extra_args=["--verbose"]), claude_bin="claude").argv
    root = skillpacks.packs_dir()
    assert "--plugin-dir" not in a  # it makes newer Claude Code drop the installed plugins
    i = a.index("--add-dir")
    assert a[i:i + 4] == ["--add-dir", str(root / "playwright"), "--add-dir", str(root / "docs")]
    assert a.index("--verbose") > i and a[-2:] == ["--resume", SID[0]]
    plan = preview_plan(cat, Selection(skill_packs=["playwright"]), "claude")
    assert plan.mode == "strict"  # a pack has no MCP servers: strict mode stays
    assert any("needs playwright-cli (not found)" in w for w in plan.warnings)  # warned, not blocked
    with pytest.raises(PlanError, match="unknown skill pack"):
        build_plan(cat, Selection(skill_packs=["ghost"]), claude_bin="claude")
    with pytest.raises(PlanError, match="skill pack broken"):
        build_plan(cat, Selection(skill_packs=["broken"]), claude_bin="claude")


def test_launch_moves_an_old_packs_skills_to_claude_skills(packs, fake_popen):
    cat = discover(packs.project)
    popen = fake_popen()
    launcher.launch(cat, Selection(skill_packs=["docs", "playwright"]), claude_bin="claude", popen=popen)
    docs = skillpacks.packs_dir() / "docs"
    assert (docs / ".claude" / "skills" / "a" / "SKILL.md").is_file() and not (docs / "skills").exists()
    assert not (skillpacks.packs_dir() / "playwright" / ".claude-plugin").exists()  # nothing written for a new pack
    again = {p.name: p for p in skillpacks.discover()}["docs"]
    assert not again.legacy and [s.name for s in again.skills] == ["alpha", "beta"]
    assert "--add-dir" in popen.calls[-1].argv


def test_cli_skill_packs_flag(packs, capsys):
    assert cli.main(["--claude-bin", "claude", "--skill-packs", "docs", "--dry-run", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["argv"][doc["argv"].index("--add-dir") + 1].endswith("/skills/docs")
    assert cli.main(["--claude-bin", "claude", "--skill-packs", "ghost", "--dry-run"]) == 2
    assert "unknown skill pack(s): ghost" in capsys.readouterr().err


def test_profiles_and_last_selection(packs, screen):  # noqa: F811
    cat = discover(packs.project)
    profiles.save("web", Selection(skill_packs=["playwright"]))
    assert profiles.load("web").skill_packs == ["playwright"]
    sel, notes = profiles.reconcile(Selection(skill_packs=["playwright", "gone"]), cat)
    assert sel.skill_packs == ["playwright"] and "skill pack 'gone' no longer found" in notes
    screen.result = Selection(skill_packs=["docs"], name="n")
    assert run() == 0
    entry, _ = remember.load(packs.project)
    assert entry.selection.skill_packs == ["docs"]


def test_costs_know_packs(packs):
    cat = discover(packs.project)
    assert "pack:docs" in costs.item_keys(cat) and "pack:broken" not in costs.item_keys(cat)
    assert costs.selection_for(cat, "pack:docs").skill_packs == ["docs"]
    assert costs.selection_keys(cat, Selection(skill_packs=["docs"])) == ["pack:docs"]
    from claude_launcher.preview import cost_summary

    summary, missing = cost_summary(cat, Selection(skill_packs=["docs"]), {})
    assert "skill packs estimated" in summary and not missing


# ---------------------------------------------------------------------------- cm skills


def skills(*args):
    return cli.main(["skills", *args])


def test_skills_add_list_remove(fake_home, capsys, monkeypatch):
    src = fake_home.tmp / "pw-tmp" / ".claude" / "skills"
    skill(src / "playwright-cli", "playwright-cli", "Browser")
    skill(src / "other", "other", "Other")
    assert skills("add", "playwright", "--from", str(src), "--requires",
                  "playwright-cli=npm i -g @playwright/cli", "--description", "Browser automation") == 0
    pack = skillpacks.packs_dir() / "playwright"
    assert sorted(p.name for p in (pack / ".claude" / "skills").iterdir()) == ["other", "playwright-cli"]
    assert not (pack / ".claude" / "skills" / "other").is_symlink()
    assert json.loads((pack / "pack.json").read_text()) == {"description": "Browser automation", "requires": [
        {"command": "playwright-cli", "install_hint": "npm i -g @playwright/cli"}]}
    assert not (pack / ".claude-plugin").exists() and not (pack / "skills").exists()
    capsys.readouterr()

    assert skills("add", "playwright", "--from", str(src)) == 2
    assert "already exists" in capsys.readouterr().err
    assert skills("add", "Bad Name", "--from", str(src)) == 2
    assert skills("add", "empty", "--from", str(fake_home.tmp)) == 2
    assert "no skills" in capsys.readouterr().err

    one = skill(fake_home.tmp / "single", "solo", "Solo skill")
    assert skills("add", "solo", "--from", str(one), "--link") == 0
    link = skillpacks.packs_dir() / "solo" / ".claude" / "skills" / "single"
    assert link.is_symlink() and os.path.realpath(link) == os.path.realpath(one)
    capsys.readouterr()

    assert skills("list", "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert [p["name"] for p in doc["packs"]] == ["playwright", "solo"]
    assert skills("list") == 0
    assert "playwright  (2 skills" in capsys.readouterr().out

    monkeypatch.setattr(cli, "_has_tty", lambda: False)
    assert skills("remove", "solo") == 2  # no TTY: needs --yes
    monkeypatch.setattr(cli, "_has_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert skills("remove", "solo") == 1 and (skillpacks.packs_dir() / "solo").is_dir()
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert skills("remove", "solo") == 0
    assert not (skillpacks.packs_dir() / "solo").exists() and (one / "SKILL.md").is_file()  # the target stays
    assert skills("remove", "playwright", "--yes") == 0
    assert skills("remove", "playwright", "--yes") == 2


def test_skills_add_from_a_plugin_like_folder_and_bad_requires(fake_home, capsys):
    plug = fake_home.tmp / "plugin-like"
    skill(plug / "skills" / "x", "x", "X")
    assert skills("add", "plug", "--from", str(plug)) == 0
    assert (skillpacks.packs_dir() / "plug" / ".claude" / "skills" / "x" / "SKILL.md").is_file()
    assert skills("add", "bad", "--from", str(plug), "--requires", "rm -rf /") == 2
    assert not (skillpacks.packs_dir() / "bad").exists()


# ---------------------------------------------------------------------------- the screen


def test_screen_skill_packs(packs):
    app = LauncherApp(discover(packs.project), Selection(name="n"), claude_bin="claude")

    async def steps(app, pilot):
        sl = app.query_one("#packs", SelectionList)
        opts = {o.value: o for o in (sl.get_option_at_index(i) for i in range(sl.option_count))}
        rows = {v: str(o.prompt) for v, o in opts.items()}
        assert "needs playwright-cli (not found)" in rows["playwright"] and "1 skill" in rows["playwright"]
        assert "tok est." in rows["docs"] and "2 skills" in rows["docs"]
        assert opts["broken"].disabled
        sl.select("playwright")
        await settle(pilot)
        pv = str(app.query_one("#preview", Static).render())
        assert "--add-dir" in pv and "--plugin-dir" not in pv and "Skill packs: playwright (1 skills)" in pv
        assert "needs playwright-cli (not found)" in pv and app.plan_ok
        await pilot.press("ctrl+n")
        await settle(pilot)
        assert app.sel.skill_packs == []
        sl.select("docs")
        await settle(pilot)
        app.action_do_launch()

    result = drive(app, steps)
    assert result[1].skill_packs == ["docs"]


def test_ensure_layout_never_writes_through_a_linked_pack(fake_home, tmp_path):
    target = tmp_path / "myrepo"
    (target / "skills" / "s1").mkdir(parents=True)
    (target / "skills" / "s1" / "SKILL.md").write_text("---\nname: s1\ndescription: d\n---\nbody\n")
    packs = skillpacks.packs_dir()
    packs.mkdir(parents=True, exist_ok=True)
    (packs / "mine").symlink_to(target)
    pack = next(p for p in skillpacks.discover() if p.name == "mine")
    assert pack.legacy
    warning = skillpacks.ensure_layout(pack)
    assert warning and "move them to .claude/skills" in warning
    assert (target / "skills" / "s1").is_dir() and not (target / ".claude").exists()  # untouched
