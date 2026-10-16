import json

import pytest

from claude_launcher import cli, profiles
from claude_launcher.plan import Selection
from tests.conftest import CUA_TOKEN, MEM_SECRET, SECRET


@pytest.fixture
def launched(monkeypatch):
    calls = []

    def fake_launch(cat, sel, *, claude_bin):
        calls.append(sel)
        return 0

    monkeypatch.setattr(cli, "launch", fake_launch)
    return calls


def run(*args):
    return cli.main(["--claude-bin", "claude", *args])


def test_headless_mcp_selection(fake_home, launched):
    assert run("--mcp", "agentmemory,gdrive") == 0
    assert launched[0].local == ["agentmemory", "gdrive"]
    assert launched[0].connectors == [] and launched[0].plugins == [] and not launched[0].cmux


def test_connectors_flag(fake_home, launched):
    run("--connectors", "Slack,todoist")
    assert launched[-1].connectors == ["claude.ai Slack", "claude.ai Todoist"]
    run("--connectors", "all")
    assert set(launched[-1].connectors) == {"claude.ai Slack", "claude.ai Todoist", "claude.ai Interactive Brokers (IBKR)"}
    run("--connectors", "none")
    assert launched[-1].connectors == []


def test_no_mcp_continue_resume_passthrough(fake_home, launched):
    run("--no-mcp", "--continue")
    assert launched[-1].session == "continue" and launched[-1].local == []
    run("--resume")
    assert launched[-1].session == "resume" and launched[-1].resume_id is None
    run("--resume", "abc-123", "--model", "haiku", "--", "--verbose", "--mcp", "literal")
    s = launched[-1]
    assert s.resume_id == "abc-123" and s.model == "haiku" and s.extra_args == ["--verbose", "--mcp", "literal"]


def test_profile_flag_and_overrides(fake_home, launched):
    profiles.save("memory", Selection(local=["agentmemory"], model="opus"))
    run("--profile", "memory", "--cmux")
    assert launched[-1].local == ["agentmemory"] and launched[-1].cmux and launched[-1].model == "opus"


def test_unknown_names_exit_2(fake_home, launched, capsys):
    assert run("--mcp", "nope") == 2
    assert run("--connectors", "Nope") == 2
    assert run("--profile", "nope") == 2
    assert not launched


def test_dry_run_is_redacted(fake_home, launched, capsys):
    assert run("--dry-run", "--mcp", "agentmemory,gdrive", "--cmux", "--connectors", "Slack") == 0
    out = capsys.readouterr().out
    assert not launched
    for secret in (SECRET, MEM_SECRET, CUA_TOKEN, "/secret/path.json"):
        assert secret not in out
    assert "deny-list" in out and "agentmemory" in out
    assert run("--dry-run", "--json", "--mcp", "agentmemory") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["mode"] == "strict" and MEM_SECRET not in json.dumps(doc)


def test_list_json_is_redacted(fake_home, capsys):
    assert run("--list", "--json") == 0
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert {x["name"] for x in doc["local"]} >= {"agentmemory", "gdrive"}
    for secret in (SECRET, MEM_SECRET, CUA_TOKEN, "token=abc"):
        assert secret not in out


def test_list_table(fake_home, capsys):
    assert run("--list") == 0
    out = capsys.readouterr().out
    assert "agentmemory" in out and "Todoist" in out and MEM_SECRET not in out


def test_profile_management(fake_home, capsys, launched):
    assert run("--mcp", "gdrive", "--save-profile", "g") == 0
    assert run("--set-default", "g") == 0
    assert run("--profiles") == 0
    assert "g  (default)" in capsys.readouterr().out
    # with a default profile and no flags, the screen opens pre-filled (no TTY here -> error)
    assert run() == 2
    assert run("--delete-profile", "g") == 0
    assert profiles.list_profiles() == []


def test_interactive_requires_tty(fake_home, capsys):
    assert run() == 2
    assert "no TTY" in capsys.readouterr().err


def test_interactive_path_launches_tui_result(fake_home, monkeypatch, launched):
    import claude_launcher.tui as tui

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(tui, "run_tui", lambda cat, sel, **kw: Selection(local=["gdrive"]))
    assert run() == 0
    assert launched[-1].local == ["gdrive"]
    monkeypatch.setattr(tui, "run_tui", lambda cat, sel, **kw: None)  # Esc
    n = len(launched)
    assert run() == 0 and len(launched) == n


def test_measure(fake_home, monkeypatch, capsys):
    from claude_launcher import costs as costs_mod

    monkeypatch.setattr(costs_mod, "measure", lambda cat, keys, **kw: ({k: costs_mod.Cost(10, 1) for k in keys}, 100))
    assert run("--measure", "mcp:gdrive") == 0
    assert "mcp:gdrive" in capsys.readouterr().out


def test_cwd_flag_runs_claude_in_that_directory(fake_home, fake_popen, monkeypatch):
    import subprocess

    other = fake_home.tmp / "elsewhere"
    other.mkdir()
    popen = fake_popen()
    monkeypatch.setattr(subprocess, "Popen", popen)
    assert run("--cwd", str(other), "--no-mcp") == 0
    assert popen.calls[0].kw["cwd"] == other.resolve()


def test_measure_bad_key_fails_up_front_with_a_useful_message(fake_home, capsys):
    assert run("--measure", "mcp:gdrive,gdrive") == 1
    err = capsys.readouterr().err
    assert "cannot measure 'gdrive': expected mcp:<name>" in err
    assert "real processes" not in err  # no probe was started


def test_broken_default_profile_is_reported_not_a_crash(fake_home, capsys, launched):
    path = profiles.profiles_dir() / "broken.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]")
    profiles.set_default("broken")
    assert run() == 2  # plain `cm`
    assert "profile 'broken' is not a JSON object" in capsys.readouterr().err
    assert not launched


def test_refresh_does_not_swallow_dry_run_or_save_profile(fake_home, monkeypatch, capsys, launched):
    calls = []
    monkeypatch.setattr(cli, "refresh_connectors", lambda claude_bin: calls.append(claude_bin) or [])
    assert run("--refresh", "--dry-run", "--save-profile", "after-refresh") == 0
    out = capsys.readouterr().out
    assert calls and "Isolation:" in out  # the preview was printed, not skipped
    assert profiles.list_profiles() == ["after-refresh"] and not launched
    assert run("--refresh") == 0 and not launched  # refresh alone still just refreshes
