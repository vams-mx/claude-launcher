import json
from pathlib import Path

import pytest

from claude_launcher.plan import PlanError, Selection, build_plan
from tests.conftest import CUA_TOKEN, SECRET

D = Path("/tmp/cm-session-test")


def plan_for(cat, **kw):
    return build_plan(cat, Selection(**kw), claude_bin="claude", session_dir=D, owner_pid=99)


def no_leak(plan):
    blob = json.dumps(plan.mcp_doc) + json.dumps(plan.settings_doc) + " ".join(plan.argv)
    assert SECRET not in blob
    assert CUA_TOKEN not in blob
    assert "SUPER_SECRET_PASSWORD" not in blob


def test_nothing_selected_is_strict_and_empty(catalog):
    p = plan_for(catalog)
    assert p.mode == "strict"
    assert p.argv == ["claude", "--strict-mcp-config", f"--mcp-config={D}/mcp.json", "--settings", f"{D}/settings.json"]
    assert p.mcp_doc == {"mcpServers": {}}
    assert p.settings_doc["disableClaudeAiConnectors"] is True
    assert set(p.settings_doc["enabledPlugins"].values()) == {False}
    assert "hooks" not in p.settings_doc
    assert p.env_overrides == {"CMUX_COMPUTER_USE_MCP_DISABLED": "1"}
    no_leak(p)


def test_agentmemory_and_gdrive(catalog):
    p = plan_for(catalog, local=["agentmemory", "gdrive"])
    assert p.mode == "strict"
    assert list(p.mcp_doc["mcpServers"]) == ["agentmemory", "gdrive"]
    assert p.mcp_doc["mcpServers"]["gdrive"] == catalog.local_by_name("gdrive").config
    assert set(p.settings_doc["hooks"]) == {"SessionStart", "Stop"}  # AgentMemory hooks come along
    assert p.denied == []
    no_leak(p)


def test_hooks_only_with_their_server(catalog):
    assert "hooks" not in plan_for(catalog, local=["gdrive"]).settings_doc


def test_single_connector_uses_denylist(catalog):
    p = plan_for(catalog, connectors=["claude.ai Todoist"])
    assert p.mode == "denylist"
    assert "--strict-mcp-config" not in p.argv
    assert not any(a.startswith("--mcp-config") for a in p.argv)  # nothing local selected
    denied = {d["serverName"] for d in p.settings_doc["deniedMcpServers"]}
    assert "claude.ai Todoist" not in denied
    assert {"claude.ai Slack", "claude.ai Gmail", "claude.ai Acme Analytics (ACME)",
            "gdrive", "agentmemory", "projlocal", "teamsrv", "userwide", "cmux-cua",
            "plugin:dbtools:db"} <= denied
    assert "disableClaudeAiConnectors" not in p.settings_doc
    assert any("deniedMcpServers" in w for w in p.warnings)


def test_cmux_plus_all_connectors(catalog):
    conns = [c.name for c in catalog.connectors]
    p = plan_for(catalog, connectors=conns, cmux=True)
    assert p.mode == "denylist"
    assert list(p.mcp_doc["mcpServers"]) == ["cmux-cua"]
    denied = {d["serverName"] for d in p.settings_doc["deniedMcpServers"]}
    assert not denied & set(conns)
    assert "cmux-cua" not in denied
    assert p.env_overrides["CMUX_CUA_SOCKET_AUTH_TOKEN"] == CUA_TOKEN
    assert p.secret_env == {"CMUX_CUA_SOCKET_AUTH_TOKEN"}
    no_leak(p)


def test_cmux_alone_is_strict(catalog):
    p = plan_for(catalog, cmux=True)
    assert p.mode == "strict" and list(p.mcp_doc["mcpServers"]) == ["cmux-cua"]


def test_plugin_with_mcp_switches_to_denylist(catalog):
    p = plan_for(catalog, plugins=["dbtools@x"])
    assert p.mode == "denylist"
    assert p.settings_doc["disableClaudeAiConnectors"] is True
    denied = {d["serverName"] for d in p.settings_doc["deniedMcpServers"]}
    assert "plugin:dbtools:db" not in denied and "gdrive" in denied


def test_plugins_explicit_true_false(catalog):
    p = plan_for(catalog, plugins=["prompt-tools@market"])
    assert p.settings_doc["enabledPlugins"] == {
        "commit-commands@official": False, "dbtools@x": False, "prompt-tools@market": True}


def test_model_permissions_session_and_passthrough(catalog):
    p = plan_for(catalog, model="opus", permissions="skip", session="resume", extra_args=["--verbose", "hi"])
    assert p.argv[-6:] == ["--model", "opus", "--dangerously-skip-permissions", "--verbose", "hi", "--resume"]
    p = plan_for(catalog, session="resume", resume_id="abc")
    assert p.argv[-2:] == ["--resume", "abc"]
    p = plan_for(catalog, session="continue")
    assert p.argv[-1] == "--continue"
    assert "--model" not in plan_for(catalog, model="default").argv


@pytest.mark.parametrize("kw", [
    {"local": ["nope"]}, {"connectors": ["claude.ai Nope"]}, {"plugins": ["nope@x"]},
    {"model": "gpt"}, {"permissions": "yolo"}, {"session": "later"},
])
def test_invalid_selection(catalog, kw):
    with pytest.raises(PlanError):
        plan_for(catalog, **kw)


def test_cmux_unavailable_errors(catalog):
    catalog.cmux.available = False
    catalog.cmux.reason = "nope"
    with pytest.raises(PlanError, match="cmux"):
        plan_for(catalog, cmux=True)


def test_companion_settings_merged_once(catalog, fake_home):
    # two servers sharing one companion settings file must not duplicate hooks
    srv = catalog.local_by_name("agentmemory")
    catalog.local_by_name("gdrive").settings_path = srv.settings_path
    p = plan_for(catalog, local=["agentmemory", "gdrive"])
    assert len(p.settings_doc["hooks"]["Stop"]) == 1


def test_cmux_entry_uses_claudes_pid_and_keeps_the_token_out_of_files(catalog):
    p = plan_for(catalog, cmux=True)
    entry = p.mcp_doc["mcpServers"]["cmux-cua"]["env"]
    # Claude Code expands this at launch; cmux's wrapper exports it as Claude's own PID, and cm
    # exports its own PID (Claude's parent) for direct launches. No PID is written into the file.
    assert entry["CMUX_CUA_STATE_OWNER_PID"] == "${CMUX_CLAUDE_PID:-}"
    assert p.env_overrides["CMUX_CLAUDE_PID"] == "99"
    # the owner PID (99) is never written as a value (the doc also holds pytest's tmp path, which may contain "99")
    assert not any(str(v) == "99" or str(v).endswith("-99") for v in entry.values())
    assert "--strict-mcp-config" in p.argv  # isolation is kept
    assert p.env_overrides["CMUX_COMPUTER_USE_MCP_DISABLED"] == "1"
    assert p.env_overrides["CMUX_CUA_SOCKET_AUTH_TOKEN"] == CUA_TOKEN and CUA_TOKEN not in json.dumps(p.mcp_doc)
    assert "CMUX_CUA_SOCKET_AUTH_TOKEN" not in p.mcp_doc["mcpServers"]["cmux-cua"]["env"]  # only the shim names it


def test_cmux_token_from_an_unreadable_file_falls_back_with_a_warning(catalog, fake_home, monkeypatch):
    monkeypatch.setenv("CMUX_CUA_AUTH_TOKEN_FILE", str(fake_home.tmp / "missing-token"))
    monkeypatch.setenv("CMUX_CUA_SOCKET_AUTH_TOKEN", "inherited")
    p = plan_for(catalog, cmux=True)
    assert p.env_overrides["CMUX_CUA_SOCKET_AUTH_TOKEN"] == "inherited"
    assert any("may be stale" in w for w in p.warnings)


def test_default_session_dir_is_content_addressed(catalog, fake_home):
    from claude_launcher import sessions

    p = build_plan(catalog, Selection(local=["gdrive"]), claude_bin="claude", owner_pid=1)
    d = fake_home.home / ".claude-launcher" / "sessions" / sessions.session_id(p.mcp_doc, p.settings_doc)
    assert p.mcp_path == d / "mcp.json" and p.settings_path == d / "settings.json"
    assert f"--mcp-config={d}/mcp.json" in p.argv and p.argv.count("--settings") == 1
    assert not d.exists()  # building a plan (or a preview) writes nothing
    again = build_plan(catalog, Selection(local=["gdrive"]), claude_bin="claude", owner_pid=2)
    assert again.mcp_path == p.mcp_path
    other = build_plan(catalog, Selection(local=["agentmemory"]), claude_bin="claude")
    assert other.mcp_path.parent != d


@pytest.mark.parametrize("kw", [
    {"cmux": True}, {"cmux": True, "connectors": ["claude.ai Slack"]},
    {"local": ["agentmemory", "gdrive"], "plugins": ["dbtools@x"], "cmux": True},
])
def test_documents_are_identical_across_launches(catalog, fake_home, monkeypatch, kw):
    """Same selection -> same bytes, whatever cm's PID, the cmux surface or the inherited PID."""
    from claude_launcher import sessions

    first = build_plan(catalog, Selection(**kw), claude_bin="claude", owner_pid=987651)
    monkeypatch.setenv("CMUX_CLAUDE_PID", "987653")
    monkeypatch.setenv("CMUX_SURFACE_ID", "SURFACE-OTHER")
    monkeypatch.setenv("CMUX_CUA_SOCKET_PATH", str(fake_home.tmp / "other.sock"))
    second = build_plan(catalog, Selection(**kw), claude_bin="claude", owner_pid=987652)
    for a, b in ((first.mcp_doc, second.mcp_doc), (first.settings_doc, second.settings_doc)):
        assert sessions.doc_bytes(a) == sessions.doc_bytes(b)
    assert first.mcp_path == second.mcp_path
    blob = json.dumps(first.mcp_doc) + json.dumps(first.settings_doc)
    for per_launch in ("987651", "987652", "987653", "SURFACE-1", "SURFACE-OTHER", "cm-session-"):
        assert per_launch not in blob
    assert second.env_overrides["CMUX_CLAUDE_PID"] == "987652"


# --- fork and session name -----------------------------------------------------


def test_fork_and_name_go_right_before_resume(catalog):
    p = plan_for(catalog, session="resume", resume_id="abc-1", fork=True, name="Kafka retry",
                 extra_args=["--verbose"])
    assert p.argv[-6:] == ["--verbose", "--fork-session", "--name", "Kafka retry", "--resume", "abc-1"]
    p = plan_for(catalog, session="continue", fork=True)
    assert p.argv[-2:] == ["--continue", "--fork-session"]
    p = plan_for(catalog, name="  New thing ")
    assert p.argv[-2:] == ["--name", "New thing"]  # trimmed, one argv element, no shell
    p = plan_for(catalog, name="-dash first")
    assert p.argv[-2:] == ["--name", "-dash first"]  # a required option value: claude takes it as is
    assert "--name" not in plan_for(catalog, name="   ").argv


@pytest.mark.parametrize("kw", [
    {"fork": True},  # new
    {"fork": True, "session": "resume"},  # claude's picker: nothing chosen to fork
])
def test_fork_needs_something_to_fork(catalog, kw):
    with pytest.raises(PlanError, match="fork needs a session"):
        plan_for(catalog, **kw)


def test_bad_or_duplicate_names_are_rejected(catalog):
    with pytest.raises(PlanError, match="control characters"):
        plan_for(catalog, name="two\nlines")
    for extra in (["--name", "x"], ["--name=x"], ["-n", "x"]):
        with pytest.raises(PlanError, match="named twice"):
            plan_for(catalog, name="mine", extra_args=extra)
    assert plan_for(catalog, extra_args=["--name", "x"]).argv[-2:] == ["--name", "x"]  # only one name: fine


def test_session_line(catalog):
    from claude_launcher.preview import session_line

    titles = {"abc-12345678": "FORCE Kafka"}
    assert session_line(Selection()) == "new (unnamed)"
    assert session_line(Selection(name="X")) == 'new → "X"'
    assert session_line(Selection(session="continue")) == "continue the last session"
    assert session_line(Selection(session="continue", fork=True, name="Y")) == 'fork of the last session → "Y"'
    assert session_line(Selection(session="resume")) == "resume (claude's picker)"
    assert session_line(Selection(session="resume", resume_id="abc-12345678"), titles) == "resume FORCE Kafka"
    assert session_line(Selection(session="resume", resume_id="abc-12345678", fork=True, name="F2"), titles) == \
        'fork of FORCE Kafka → "F2"'
    unknown = Selection(session="resume", resume_id="zzz-99999999", fork=True)
    assert session_line(unknown) == "fork of zzz-9999 (unnamed)"
    assert session_line(Selection(extra_args=["-n", "x"])) == "new (named by the arguments after --)"


# --- worktree and tmux ----------------------------------------------------------


def test_worktree_argv_for_each_combination(catalog):
    p = plan_for(catalog, worktree="x", name="X")
    assert p.argv[-4:] == ["--name", "X", "-w", "x"]
    p = plan_for(catalog, worktree="x", tmux=True)
    assert p.argv[-3:] == ["-w", "x", "--tmux"] and any("tmux" in w for w in p.warnings)
    p = plan_for(catalog, session="resume", resume_id="abc-1", fork=True, name="X v2", worktree="x-v2",
                 extra_args=["--verbose"])
    assert p.argv[-8:] == ["--verbose", "--fork-session", "--name", "X v2", "-w", "x-v2", "--resume", "abc-1"]
    assert p.argv.count("--name") == 1 and p.argv[-2] == "--resume"
    p = plan_for(catalog, session="resume", resume_id="abc-1", fork=True, worktree="f", tmux=True)
    assert p.argv[-6:] == ["--fork-session", "-w", "f", "--tmux", "--resume", "abc-1"]
    p = plan_for(catalog, session="resume", resume_id="abc-1", worktree="w")  # supported, with a warning
    assert p.argv[-4:] == ["-w", "w", "--resume", "abc-1"]
    assert any("moves the session itself into worktree w" in w for w in p.warnings)
    p = plan_for(catalog, session="resume", worktree="w")  # claude's picker
    assert p.argv[-3:] == ["-w", "w", "--resume"]
    assert "-w" not in plan_for(catalog).argv and "--tmux" not in plan_for(catalog).argv


@pytest.mark.parametrize("kw,match", [
    ({"tmux": True}, "tmux needs a worktree"),
    ({"session": "continue", "worktree": "x"}, "Continue"),
    ({"session": "continue", "fork": True, "worktree": "x"}, "Continue"),
    ({"session": "resume", "resume_id": "a", "worktree": "x", "tmux": True}, "needs Fork"),
    ({"worktree": "bad name"}, "invalid worktree name"),
    ({"worktree": ""}, "empty"),
    ({"worktree": "-x"}, "start with"),
    ({"worktree": "x", "extra_args": ["-w", "y"]}, "given twice"),
    ({"worktree": "x", "extra_args": ["--worktree=y"]}, "given twice"),
    ({"worktree": "x", "extra_args": ["--tmux"]}, "given twice"),
])
def test_worktree_combinations_claude_cannot_do(catalog, kw, match):
    with pytest.raises(PlanError, match=match):
        plan_for(catalog, **kw)


def test_worktree_needs_a_repository_tmux_and_a_usable_name(fake_home, monkeypatch, tmp_path):
    from claude_launcher import worktree
    from claude_launcher.catalog import discover

    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(PlanError, match="git repository"):
        plan_for(discover(plain), worktree="x")
    cat = discover(fake_home.project)
    monkeypatch.setattr(worktree, "tmux_available", lambda: False)
    with pytest.raises(PlanError, match="tmux is not installed"):
        plan_for(cat, worktree="x", tmux=True)
    (fake_home.project / ".claude" / "worktrees" / "junk").mkdir(parents=True)  # claude would refuse it
    with pytest.raises(PlanError, match="not a git worktree"):
        plan_for(cat, worktree="junk")


def test_passthrough_worktree_alone_is_passed_through(catalog):
    assert plan_for(catalog, extra_args=["-w", "mine"]).argv[-2:] == ["-w", "mine"]


def test_session_name_survives_local_servers(catalog):
    # regression: the server loop reused the variable holding the session name, so a session with
    # local MCPs was named after the last one (e.g. "agentmemory")
    for local in (["gdrive"], ["agentmemory"], ["agentmemory", "gdrive"]):
        p = plan_for(catalog, local=local, name="My_session_name")
        i = p.argv.index("--name")
        assert p.argv[i + 1] == "My_session_name"
        assert p.argv.count("--name") == 1
