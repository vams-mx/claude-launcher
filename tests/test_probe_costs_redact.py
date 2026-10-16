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


def test_redaction_of_env_pairs_and_auth_headers():
    args = ["run", "-e", "GITHUB_TOKEN=ghp_secret1", "--env=API_KEY=secret2", "-e", "LOG_LEVEL=debug",
            "--header", "Authorization: Bearer sk-secret3", "--header=X-Api-Key: secret4",
            "-H", "Accept: application/json", "Bearer sk-secret5", "BASE=https://h/p?token=secret6"]
    out = redact_args(args)
    joined = " ".join(out)
    for n in range(1, 7):
        assert f"secret{n}" not in joined
    assert out == ["run", "-e", f"GITHUB_TOKEN={MASK}", f"--env=API_KEY={MASK}", "-e", "LOG_LEVEL=debug",
                   "--header", f"Authorization: {MASK}", f"--header=X-Api-Key: {MASK}",
                   "-H", "Accept: application/json", MASK, f"BASE=https://h/p?{MASK}"]
    # the same args in a server config, as --list --json / dry runs show them
    assert "ghp_secret1" not in str(redact_server({"command": "docker", "args": args}))


def test_measure_rejects_bad_keys_before_any_probe(catalog, fake_home):
    import pytest

    calls = []
    with pytest.raises(ValueError) as exc:
        costs_mod.measure(catalog, ["mcp:gdrive", "gdrive", "mcp:typo", "connector:Nope"], claude_bin="claude",
                          run_probe=lambda *a, **k: calls.append(a))
    msg = str(exc.value)
    assert "'gdrive': expected mcp:<name>" in msg and "'mcp:typo'" in msg and "'connector:Nope'" in msg
    assert "mcp:gdrive'" not in msg and calls == []


def test_measure_all_skips_bad_items_instead_of_blocking_the_run(catalog, fake_home):
    def fake_probe(argv, env, cwd=None):
        mcp = json.load(open(next(a.split("=", 1)[1] for a in argv if a.startswith("--mcp-config="))))
        return probe.ProbeResult(0, tools=["Bash"], input_tokens=1000 + 250 * len(mcp["mcpServers"]), model="haiku")

    msgs = []
    res, _ = costs_mod.measure(catalog, ["mcp:gdrive", "mcp:typo"], claude_bin="claude", run_probe=fake_probe,
                               full=False, strict=False, progress=msgs.append)
    assert res["mcp:gdrive"].tokens == 250 and "mcp:typo" not in res
    assert any("skipping mcp:typo" in m for m in msgs)


def test_measure_keeps_paid_results_when_a_later_probe_fails(catalog, fake_home):
    import pytest

    def fake_probe(argv, env, cwd=None):
        mcp = json.load(open(next(a.split("=", 1)[1] for a in argv if a.startswith("--mcp-config="))))
        if "agentmemory" in mcp["mcpServers"]:
            raise KeyboardInterrupt  # e.g. Ctrl+C half-way through
        return probe.ProbeResult(0, tools=["Bash"], input_tokens=1000 + 250 * len(mcp["mcpServers"]), model="haiku")

    with pytest.raises(KeyboardInterrupt):
        costs_mod.measure(catalog, ["mcp:gdrive", "mcp:agentmemory"], claude_bin="claude", run_probe=fake_probe,
                          full=False)
    assert costs_mod.load()["mcp:gdrive"].tokens == 250
