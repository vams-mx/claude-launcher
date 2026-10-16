"""Headless probe of a real launch: run the planned argv with
`claude -p --output-format stream-json --verbose` on Haiku and read the
`system/init` event (loaded MCP servers, tools, plugins) and the final usage.

Used only by the opt-in `cm --measure` and scripts/smoke_verify.py; the test
suite mocks it. Probes are cheap and side-effect free:
  * MCP_CONNECTION_NONBLOCKING=false - otherwise -p mode emits init before MCP
    servers connect and reports none (verified).
  * --no-session-persistence - nothing written to your session history.
  * --permission-mode dontAsk - any tool call that would prompt is denied.
  * AGENTMEMORY_SDK_CHILD=1 - AgentMemory's hook scripts exit early, so a
    probe never records observations in your memory store (hooks still fire
    and are counted).
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PROMPT = "reply with just: ok"
PROBE_FLAGS = [
    "-p", "--output-format", "stream-json", "--verbose", "--model", "haiku",
    "--no-session-persistence", "--permission-mode", "dontAsk", "--include-hook-events",
]
STRIP_ENV = (
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
)


@dataclass
class ProbeResult:
    returncode: int
    servers: list[tuple[str, str]] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    hooks_started: dict[str, int] = field(default_factory=dict)
    input_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    error: str = ""

    @property
    def mcp_tools(self) -> list[str]:
        return [t for t in self.tools if t.startswith("mcp__")]

    def tools_by_server(self) -> dict[str, int]:
        c: collections.Counter[str] = collections.Counter(t.split("__")[1] for t in self.mcp_tools)
        return dict(c)


def parse_stream(stdout: str) -> ProbeResult:
    res = ProbeResult(returncode=0)
    hooks: collections.Counter[str] = collections.Counter()
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            res.servers = [(s.get("name", ""), s.get("status", "")) for s in ev.get("mcp_servers") or []]
            res.tools = list(ev.get("tools") or [])
            res.plugins = [p.get("source") or p.get("name", "") for p in ev.get("plugins") or []]
            res.model = ev.get("model", "")
        elif ev.get("type") == "system" and ev.get("subtype") == "hook_started":
            hooks[ev.get("hook_event", "?")] += 1
        elif ev.get("type") == "result":
            u: dict[str, Any] = ev.get("usage") or {}
            res.input_tokens = sum(int(u.get(k) or 0) for k in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            res.cost_usd = float(ev.get("total_cost_usd") or 0)
    res.hooks_started = dict(hooks)
    return res


def run(
    argv: list[str],
    env_overrides: dict[str, str],
    *,
    cwd: Path | None = None,
    timeout: float = 240,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> ProbeResult:
    runner = runner or subprocess.run
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    env.update(env_overrides)
    env["MCP_CONNECTION_NONBLOCKING"] = "false"
    env["AGENTMEMORY_SDK_CHILD"] = "1"
    # strip interactive-only flags the plan may carry; probes are always new sessions on Haiku
    clean: list[str] = []
    skip = False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a == "--model":
            skip = True
            continue
        if a in ("--continue", "--dangerously-skip-permissions"):
            continue
        if a == "--resume":
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            skip = bool(nxt and not nxt.startswith("-"))
            continue
        clean.append(a)
    cmd = clean + PROBE_FLAGS + [PROMPT]
    try:
        p = runner(cmd, cwd=str(cwd) if cwd else None, env=env, capture_output=True, text=True,
                   timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return ProbeResult(returncode=124, error="timeout")
    res = parse_stream(p.stdout or "")
    res.returncode = p.returncode
    if p.returncode != 0 or not res.model:
        res.error = (p.stderr or "")[-400:]
    return res
