"""Hermetic fixtures: a fake HOME with fake Claude/cmux config.

No test may start a real process or read the real user's config: an autouse
fixture replaces subprocess.Popen/run with tripwires and HOME, config dirs and
cmux variables are redirected into tmp_path.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SECRET = "sk-THIS-MUST-NEVER-LEAK-123"
MEM_SECRET = "agentmemory-secret-value-xyz"
CUA_TOKEN = "cua-token-should-stay-in-env-only"

WRAPPER_SNIPPET = r'''#!/usr/bin/env bash
cmux_computer_use_config() {
    printf '{"mcpServers":{"cmux-cua":{"command":"%s","args":["mcp","--socket","%s"],"env":{"CMUX_CUA_MCP_FORCE_PROXY":"1","CMUX_CUA_DEFAULT_SESSION":"%s","CMUX_CUA_STATE_OWNER_PID":"%s","CMUX_CUA_STATE_DIR":"%s","NODE_OPTIONS":""}}}}' \
        "$(cmux_computer_use_json_escape "$client")" \
        "$(cmux_computer_use_json_escape "$cua_socket")" \
        "$(cmux_computer_use_json_escape "$default_session")" \
        "$(cmux_computer_use_json_escape "${CMUX_CLAUDE_PID:-$$}")" \
        "$(cmux_computer_use_json_escape "$state_dir")"
}
'''


def _w(path: Path, doc) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) if not isinstance(doc, str) else doc)
    return path


@pytest.fixture(autouse=True)
def no_real_processes(monkeypatch):
    def tripwire(*a, **k):
        raise AssertionError(f"tests must not start real processes: {a[:1]}")

    monkeypatch.setattr(subprocess, "Popen", tripwire)
    monkeypatch.setattr(subprocess, "run", tripwire)
    monkeypatch.setattr(subprocess, "call", tripwire)
    monkeypatch.setattr(subprocess, "check_output", tripwire)
    # health checks must not hit the network either
    import claude_launcher.health as health

    monkeypatch.setattr(health, "check", lambda srv, timeout=0.8: None)
    # `claude --version` is a process too: pretend the verified version is installed
    import claude_launcher.compat as compat

    monkeypatch.setattr(compat, "installed_version", lambda claude_bin, run=None, timeout=5: compat.VERIFIED_VERSIONS[0])


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "work" / "proj"
    project.mkdir(parents=True)
    (project / ".git").mkdir()
    for var in list(os.environ):
        if var.startswith(("CMUX_", "CLAUDE", "AGENTMEMORY", "XDG_CONFIG_HOME", "CM_", "INSTALL_AGENT_MEMORY")):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))  # cm's home is therefore ~/.claude-launcher (the default)
    monkeypatch.setenv("CM_MANAGED_DIR", str(tmp_path / "managed"))
    monkeypatch.chdir(project)

    _w(home / ".claude" / "settings.json", {
        "env": {"SUPER_SECRET_PASSWORD": SECRET},
        "model": "opus",
        "enabledPlugins": {"commit-commands@official": True, "prompt-tools@market": True, "old@x": False},
    })
    _w(home / ".claude" / "plugins" / "installed_plugins.json", {"version": 2, "plugins": {
        "commit-commands@official": [{"scope": "user", "installPath": str(home / "plug" / "commit")}],
        "prompt-tools@market": [{"scope": "user", "installPath": str(home / "plug" / "prompt")}],
        "dbtools@x": [{"scope": "local", "installPath": str(home / "plug" / "dbtools"), "projectPath": str(project)}],
    }})
    _w(home / "plug" / "dbtools" / ".mcp.json", {"mcpServers": {"db": {"command": "dbserver"}}})
    _w(project / ".claude" / "settings.local.json", {"enabledPlugins": {"dbtools@x": True}})
    _w(home / ".claude.json", {
        "mcpServers": {"userwide": {"type": "http", "url": "https://user.example.com/mcp?token=abc"}},
        "projects": {
            str(home): {"mcpServers": {"gdrive": {"type": "stdio", "command": "npx",
                                                  "args": ["-y", "@modelcontextprotocol/server-gdrive"],
                                                  "env": {"GDRIVE_CREDENTIALS_PATH": "/secret/path.json"}}}},
            str(project): {"mcpServers": {"projlocal": {"command": "projlocal-bin"}}},
        },
        "claudeAiMcpEverConnected": ["claude.ai Slack", "claude.ai Gmail", "claude.ai Todoist"],
        "oauthAccount": {"emailAddress": "someone@example.com"},
    })
    _w(project / ".mcp.json", {"mcpServers": {"teamsrv": {"type": "http", "url": "https://team.example.com/mcp"}}})
    # cm config: the default folder plus one extra folder holding a hand-made AgentMemory config
    _w(home / ".claude-launcher" / "config.json", {"mcp_folders": ["~/.claude-launcher/mcps", "~/extra-mcps"]})
    (home / ".claude-launcher" / "mcps").mkdir(parents=True)
    _w(home / "extra-mcps" / "claude-memory-mcp.json", {"mcpServers": {"agentmemory": {
        "command": "npx", "args": ["-y", "@agentmemory/mcp"],
        "env": {"AGENTMEMORY_URL": "http://localhost:3111", "AGENTMEMORY_SECRET": MEM_SECRET}}}})
    _w(home / "extra-mcps" / "claude-memory-settings.json", {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "node session-start.mjs"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "node stop.mjs"}]}],
    }})
    _w(home / ".claude-launcher" / "connectors.json", {
        "refreshed_at": "2026-10-15T00:00:00+00:00",
        "connectors": [{"name": "claude.ai Slack", "status": "Connected"},
                       {"name": "claude.ai Todoist", "status": "Connected"},
                       {"name": "claude.ai Acme Analytics (ACME)", "status": "Connected"}],
    })

    # fake cmux install
    bindir = tmp_path / "cmux.app" / "bin"
    wrapper = _w(bindir / "cmux-claude-wrapper", WRAPPER_SNIPPET)
    client = _w(bindir / "cmux-cua", "#!/bin/sh\n")
    client.chmod(client.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("CM_CMUX_WRAPPER", str(wrapper))
    monkeypatch.setenv("CMUX_SURFACE_ID", "SURFACE-1")
    monkeypatch.setenv("CMUX_CUA_SOCKET_PATH", str(tmp_path / "cua.sock"))
    monkeypatch.setenv("CMUX_CUA_STATE_DIR", str(tmp_path / "cua-state"))
    token = tmp_path / "auth-token"
    token.write_text(CUA_TOKEN + "\n")
    token.chmod(0o600)
    monkeypatch.setenv("CMUX_CUA_AUTH_TOKEN_FILE", str(token))

    class Env:
        pass

    e = Env()
    e.home, e.project, e.tmp, e.wrapper, e.client, e.token = home, project, tmp_path, wrapper, client, token
    return e


@pytest.fixture
def catalog(fake_home):
    from claude_launcher.catalog import discover

    return discover(fake_home.project)


class FakeProc:
    def __init__(self, argv, rc=0, on_wait=None, **kw):
        self.argv = argv
        self.kw = kw
        self.rc = rc
        self.on_wait = on_wait
        self.signals = []

    def wait(self):
        if self.on_wait:
            self.on_wait(self)
        return self.rc

    def send_signal(self, s):
        self.signals.append(s)


@pytest.fixture
def fake_popen():
    calls = []

    def make(rc=0, on_wait=None):
        def popen(argv, **kw):
            p = FakeProc(argv, rc, on_wait, **kw)
            calls.append(p)
            return p
        popen.calls = calls
        return popen

    return make


# ---------------------------------------------------------------------------
# fake Claude Code transcripts (~/.claude/projects/<key>/<id>.jsonl)

SID = ["11111111-aaaa-4aaa-8aaa-000000000001", "22222222-bbbb-4bbb-8bbb-000000000002",
       "33333333-cccc-4ccc-8ccc-000000000003", "44444444-dddd-4ddd-8ddd-000000000004"]


def ts(hours_ago: float) -> str:
    from datetime import datetime, timedelta, timezone

    t = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def rec_user(text, hours_ago=1.0, **kw):
    return {"parentUuid": None, "isSidechain": False, "type": "user", "message": {"role": "user", "content": text},
            "uuid": "u", "timestamp": ts(hours_ago), "cwd": "/x", "sessionId": "s", **kw}


def rec_assistant(hours_ago=1.0, **kw):
    return {"parentUuid": "u", "isSidechain": False, "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            "uuid": "a", "timestamp": ts(hours_ago), **kw}


def rec_custom(title):
    return {"type": "custom-title", "customTitle": title, "sessionId": "s"}


def rec_ai(title):
    return {"type": "ai-title", "aiTitle": title, "sessionId": "s"}


def write_session(cwd, sid, records, *, hours_ago=None, raw_lines=()):
    """Write a transcript for project `cwd`; mtime = `hours_ago` (default: the newest record)."""
    import time
    from claude_launcher import recent

    d = recent.projects_root() / recent.project_key(cwd)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    lines = [json.dumps(r) for r in records] + list(raw_lines)
    p.write_text("".join(line + "\n" for line in lines))
    if hours_ago is not None:
        t = time.time() - hours_ago * 3600
        os.utime(p, (t, t))
    return p


def standard_sessions(cwd):
    """Four sessions: custom name (2 h), AI title only (5 h), first prompt only (26 h), oldest (50 h)."""
    write_session(cwd, SID[0], [rec_user("fix the kafka consumer", 3), rec_assistant(2.5), rec_ai("Kafka fix"),
                                rec_custom("FORCE Kafka reconciliation"), rec_user("and the tests", 2.1),
                                rec_assistant(2)], hours_ago=2)
    write_session(cwd, SID[1], [rec_user("why is the build red", 6), rec_assistant(5), rec_ai("Red build triage")],
                  hours_ago=5)
    write_session(cwd, SID[2], [rec_user("<command-name>/mcp</command-name>", 27),
                                rec_user("please summarise the release notes for version 4.2 of the payments "
                                         "gateway and list every breaking change", 26.5),
                                rec_assistant(26)], hours_ago=26)
    write_session(cwd, SID[3], [rec_user("old one", 51), rec_assistant(50), rec_custom("Oldest named")],
                  hours_ago=50)
