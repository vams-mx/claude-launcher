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
    # Claude Code expands this at launch; cmux's wrapper exports it as Claude's own PID.
    assert entry["CMUX_CUA_STATE_OWNER_PID"] == "${CMUX_CLAUDE_PID:-99}"
    assert "--strict-mcp-config" in p.argv  # isolation is kept
    assert p.env_overrides["CMUX_COMPUTER_USE_MCP_DISABLED"] == "1"
    assert p.env_overrides["CMUX_CUA_SOCKET_AUTH_TOKEN"] and "CMUX_CUA_SOCKET_AUTH_TOKEN" not in str(p.mcp_doc)


def test_cmux_token_from_an_unreadable_file_falls_back_with_a_warning(catalog, fake_home, monkeypatch):
    monkeypatch.setenv("CMUX_CUA_AUTH_TOKEN_FILE", str(fake_home.tmp / "missing-token"))
    monkeypatch.setenv("CMUX_CUA_SOCKET_AUTH_TOKEN", "inherited")
    p = plan_for(catalog, cmux=True)
    assert p.env_overrides["CMUX_CUA_SOCKET_AUTH_TOKEN"] == "inherited"
    assert any("may be stale" in w for w in p.warnings)
