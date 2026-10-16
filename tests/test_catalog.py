import json
import subprocess

from claude_launcher.catalog import discover, parse_mcp_list, refresh_connectors


def test_local_servers_all_sources(catalog, fake_home):
    by = {s.name: s for s in catalog.local}
    assert set(by) == {"agentmemory", "gdrive", "projlocal", "teamsrv", "userwide"}
    assert by["agentmemory"].source.startswith("file:~/.personal/claude-memory-mcp.json")
    assert by["agentmemory"].settings_path.name == "claude-memory-settings.json"
    assert by["projlocal"].source == "local (this project)"
    assert by["teamsrv"].source.startswith("project:")
    assert by["userwide"].source.startswith("user")
    assert by["gdrive"].source == "local:~"
    # verbatim config, nothing invented
    assert by["gdrive"].config["args"] == ["-y", "@modelcontextprotocol/server-gdrive"]
    # current-project local scope is listed before other projects' servers
    names = [s.name for s in catalog.local]
    assert names.index("projlocal") < names.index("gdrive")


def test_summary_redacts(catalog):
    s = {x.name: x for x in catalog.local}
    assert "token=abc" not in s["userwide"].summary()
    assert "secret" not in s["gdrive"].summary().lower()


def test_settings_files_without_mcpservers_are_ignored(fake_home):
    (fake_home.home / ".personal" / "notes.json").write_text(json.dumps({"foo": 1}))
    (fake_home.home / ".personal" / "broken.json").write_text("{nope")
    cat = discover(fake_home.project)
    assert "foo" not in {s.name for s in cat.local}
    assert any("broken.json" in w for w in cat.warnings)


def test_user_folder_and_x_cm_metadata(fake_home):
    d = fake_home.home / ".personal" / "claude-mcps"
    d.mkdir(parents=True)
    hooks = d / "hooks-for-search.json"
    hooks.write_text(json.dumps({"hooks": {"Stop": []}}))
    (d / "search.json").write_text(json.dumps({
        "mcpServers": {"search": {"command": "search-bin"}},
        "x-cm": {"description": "web search", "settings": str(hooks)},
    }))
    cat = discover(fake_home.project)
    srv = cat.local_by_name("search")
    assert srv.description == "web search" and srv.settings_path == hooks


def test_duplicate_name_first_wins(fake_home):
    d = fake_home.home / ".personal" / "claude-mcps"
    d.mkdir(parents=True)
    (d / "g.json").write_text(json.dumps({"mcpServers": {"gdrive": {"command": "other"}}}))
    cat = discover(fake_home.project)
    g = cat.local_by_name("gdrive")
    assert g.config == {"command": "other"} and g.also_in == ["local:~"]


def test_connectors_cache_plus_ever_connected(catalog):
    by = {c.short: c for c in catalog.connectors}
    assert by["Slack"].verified and by["Todoist"].verified
    assert not by["Gmail"].verified  # only in claudeAiMcpEverConnected
    assert by["Interactive Brokers (IBKR)"].tool_prefix == "mcp__claude_ai_Interactive_Brokers_IBKR__"


def test_resolve_connectors(catalog):
    found, missing = catalog.resolve_connectors(["slack", "claude.ai Todoist", "IBKR", "nope"])
    assert found == ["claude.ai Slack", "claude.ai Todoist", "claude.ai Interactive Brokers (IBKR)"]
    assert missing == ["nope"]


def test_plugins(catalog):
    by = {p.id: p for p in catalog.plugins}
    assert set(by) == {"commit-commands@official", "prompt-tools@vams", "dbtools@x"}  # old@x is disabled
    assert by["dbtools@x"].enabled_in == ["local"]
    assert by["dbtools@x"].mcp_servers == ["plugin:dbtools:db"]


def test_cmux_probe(catalog):
    assert catalog.cmux.available and catalog.cmux.token_source == "file"


def test_parse_mcp_list():
    out = """Checking MCP server health…

claude.ai Claude Docs: https://x/mcp - ✔ Connected
claude.ai Interactive Brokers (IBKR): https://y/mcp - ✘ Failed to connect
gdrive: npx -y @modelcontextprotocol/server-gdrive - ✔ Connected
claude.ai Gmail: https://z - ! Needs authentication
"""
    assert parse_mcp_list(out) == [
        ("claude.ai Claude Docs", "Connected"),
        ("claude.ai Interactive Brokers (IBKR)", "Failed to connect"),
        ("claude.ai Gmail", "Needs authentication"),
    ]


def test_refresh_connectors_uses_temp_cwd_and_writes_cache(fake_home):
    seen = {}

    def run(cmd, **kw):
        seen.update(cmd=cmd, cwd=kw["cwd"], env=kw["env"])
        return subprocess.CompletedProcess(cmd, 0, "claude.ai Slack: https://a - ✔ Connected\n", "")

    found = refresh_connectors("claude", run=run)
    assert found == [("claude.ai Slack", "Connected")]
    assert seen["cmd"] == ["claude", "mcp", "list"]
    assert str(fake_home.project) not in seen["cwd"]
    cache = json.loads((fake_home.home / ".config/claude-launcher/connectors.json").read_text())
    assert cache["connectors"] == [{"name": "claude.ai Slack", "status": "Connected"}]


def test_discovery_is_read_only(fake_home):
    before = {p: p.read_bytes() for p in fake_home.home.rglob("*") if p.is_file()}
    discover(fake_home.project)
    after = {p: p.read_bytes() for p in fake_home.home.rglob("*") if p.is_file()}
    assert before == after
