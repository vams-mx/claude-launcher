import json
import subprocess

from claude_launcher import costs as costs_mod
from claude_launcher import probe
from claude_launcher.redact import MASK, redact_args, redact_server, redact_url

STREAM = "\n".join(json.dumps(e) for e in [
    {"type": "system", "subtype": "hook_started", "hook_event": "SessionStart"},
    {"type": "system", "subtype": "init", "model": "claude-haiku",
     "mcp_servers": [{"name": "agentmemory", "status": "connected"}],
     "tools": ["Bash", "mcp__agentmemory__a", "mcp__agentmemory__b"],
     "plugins": [{"name": "x", "source": "x@y"}]},
    {"type": "result", "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100,
                                 "cache_read_input_tokens": 1000}, "total_cost_usd": 0.01},
]) + "\nnot json\n"


def test_parse_stream():
    r = probe.parse_stream(STREAM)
    assert r.servers == [("agentmemory", "connected")]
    assert r.tools_by_server() == {"agentmemory": 2}
    assert r.plugins == ["x@y"] and r.input_tokens == 1110 and r.hooks_started == {"SessionStart": 1}


def test_probe_run_flags_and_env(monkeypatch):
    seen = {}
    monkeypatch.setenv("CLAUDECODE", "1")

    def runner(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"], stdin=kw["stdin"])
        return subprocess.CompletedProcess(cmd, 0, STREAM, "")

    r = probe.run(["claude", "--settings", "s.json", "--model", "opus", "--dangerously-skip-permissions",
                   "--continue", "--resume", "abc"], {"X": "1"}, runner=runner)
    assert r.returncode == 0
    cmd = seen["cmd"]
    assert cmd[:3] == ["claude", "--settings", "s.json"]
    assert "opus" not in cmd and "--dangerously-skip-permissions" not in cmd and "abc" not in cmd
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert "--no-session-persistence" in cmd and "dontAsk" in cmd
    env = seen["env"]
    assert env["MCP_CONNECTION_NONBLOCKING"] == "false" and env["AGENTMEMORY_SDK_CHILD"] == "1"
    assert env["X"] == "1" and "CLAUDECODE" not in env
    assert seen["stdin"] == subprocess.DEVNULL


def test_measure_and_estimate(catalog, fake_home):
    def fake_probe(argv, env, cwd=None):
        n = 1000
        tools = ["Bash", "ListMcpResourcesTool"]
        if any("mcp-config" in a for a in argv):
            mcp = json.load(open(next(a.split("=", 1)[1] for a in argv if a.startswith("--mcp-config="))))
            n += 250 * len(mcp["mcpServers"])
            tools += [f"mcp__{s}__t" for s in mcp["mcpServers"]]
        r = probe.ProbeResult(0, tools=tools, input_tokens=n, model="haiku")
        return r

    res, base = costs_mod.measure(catalog, ["mcp:gdrive", "cmux"], claude_bin="claude", run_probe=fake_probe)
    assert base == 1000 and res["mcp:gdrive"].tokens == 250 and res["mcp:gdrive"].tools == 1
    loaded = costs_mod.load()
    assert loaded["cmux"].tokens == 250
    tok, tools, missing = costs_mod.estimate(loaded, ["mcp:gdrive", "cmux", "mcp:agentmemory"])
    assert (tok, tools, missing) == (500, 2, ["mcp:agentmemory"])
    assert costs_mod.fmt(loaded["cmux"]) == "~250 tok · 1 tools"
    assert costs_mod.fmt(None) == "not measured"


def test_redaction():
    assert redact_url("https://h.example.com:8443/mcp?token=abc#x") == f"https://h.example.com:8443/mcp?{MASK}"
    assert redact_args(["--api-key", "k", "--token=t", "--x", "y", "https://u/p?q=1"]) == [
        "--api-key", MASK, f"--token={MASK}", "--x", "y", f"https://u/p?{MASK}"]
    r = redact_server({"command": "c", "env": {"A": "1"}, "headers": {"Authorization": "Bearer z"},
                       "url": "https://x/y?k=v"})
    assert r["env"] == {"A": MASK} and r["headers"] == {"Authorization": MASK} and "k=v" not in r["url"]
