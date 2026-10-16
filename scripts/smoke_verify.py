#!/usr/bin/env python3
"""Opt-in end-to-end proof of "no more, no less" (NOT part of the test suite).

For each sample selection it builds the exact launch plan `cm` would use,
starts a real headless session through the same `claude` (cmux's wrapper when
run inside cmux), reads the `system/init` event and compares the loaded MCP
servers, MCP tools, plugins and hook count with the selection.

Each probe is one tiny Haiku request (~1-2 cents), uses
--no-session-persistence and --permission-mode dontAsk, and sets
AGENTMEMORY_SDK_CHILD=1 so AgentMemory hooks don't record anything.

    python scripts/smoke_verify.py --yes [--json out.json] [--only NAME]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from claude_launcher import probe
from claude_launcher.catalog import Catalog, discover
from claude_launcher.launcher import write_session_files
from claude_launcher.plan import Selection, build_plan


@dataclass
class Scenario:
    name: str
    cwd: Path
    build: callable  # (Catalog) -> Selection
    notes: str = ""


@dataclass
class Outcome:
    name: str
    cwd: str
    mode: str
    expected_servers: list[str]
    loaded_servers: list[list[str]]
    tools_by_server: dict[str, int]
    unexpected_tools: list[str]
    plugins: list[str]
    expected_plugins: list[str]
    session_start_hooks: int
    baseline_hooks: int | None
    expect_memory_hooks: bool
    input_tokens: int
    cost_usd: float
    ok: bool
    problems: list[str] = field(default_factory=list)


def prefix_for(cat: Catalog, server: str) -> str:
    for c in cat.connectors:
        if c.name == server:
            return c.tool_prefix
    return f"mcp__{server}__"


def run_one(sc: Scenario, claude_bin: str, baselines: dict[str, int]) -> Outcome:
    cat = discover(sc.cwd)
    sel = sc.build(cat)
    with tempfile.TemporaryDirectory(prefix="cm-smoke-") as tmp:
        plan = build_plan(cat, sel, claude_bin=claude_bin, session_dir=Path(tmp))
        write_session_files(plan)
        res = probe.run(plan.argv, plan.env_overrides, cwd=sc.cwd)
    problems: list[str] = []
    if res.returncode != 0 or not res.model:
        problems.append(f"probe failed rc={res.returncode}: {res.error.strip()[:200]}")
    expected = sorted(plan.selected_server_names)
    loaded = sorted(n for n, _ in res.servers)
    if loaded != expected:
        extra, missing = sorted(set(loaded) - set(expected)), sorted(set(expected) - set(loaded))
        problems.append(f"servers differ: extra={extra} missing={missing}")
    not_connected = [f"{n}={s}" for n, s in res.servers if s != "connected"]
    if not_connected:
        problems.append(f"not connected: {not_connected}")
    prefixes = [prefix_for(cat, s) for s in expected]
    unexpected = [t for t in res.mcp_tools if not any(t.startswith(p) for p in prefixes)]
    if unexpected:
        problems.append(f"{len(unexpected)} MCP tools from unselected servers, e.g. {unexpected[:3]}")
    for s, p in zip(expected, prefixes):
        if not any(t.startswith(p) for t in res.mcp_tools):
            problems.append(f"no tools from selected server {s}")
    user_plugins = sorted(p for p in res.plugins if not p.endswith("@builtin"))
    if user_plugins != sorted(sel.plugins):
        problems.append(f"plugins differ: loaded={user_plugins} selected={sorted(sel.plugins)}")
    hooks = res.hooks_started.get("SessionStart", 0)
    mem = "agentmemory" in sel.local
    base = baselines.get(str(sc.cwd))
    if base is None and not sel.local and not sel.plugins:
        baselines[str(sc.cwd)] = base = hooks
    if base is not None and hooks != base + (1 if mem else 0):
        problems.append(f"SessionStart hooks {hooks}, expected {base + (1 if mem else 0)}")
    return Outcome(
        name=sc.name, cwd=str(sc.cwd), mode=plan.mode, expected_servers=expected,
        loaded_servers=[list(x) for x in res.servers], tools_by_server=res.tools_by_server(),
        unexpected_tools=unexpected, plugins=user_plugins, expected_plugins=sorted(sel.plugins),
        session_start_hooks=hooks, baseline_hooks=base, expect_memory_hooks=mem,
        input_tokens=res.input_tokens, cost_usd=res.cost_usd, ok=not problems, problems=problems,
    )


def scenarios(project: Path, home: Path) -> list[Scenario]:
    def conns(cat: Catalog) -> list[str]:
        return [c.name for c in cat.connectors if c.verified]

    def one(name: str):
        return lambda cat: Selection(connectors=cat.resolve_connectors([name])[0])

    return [
        Scenario("none", project, lambda cat: Selection()),
        Scenario("agentmemory only", project, lambda cat: Selection(local=["agentmemory"])),
        Scenario("gdrive + agentmemory", project, lambda cat: Selection(local=["agentmemory", "gdrive"])),
        Scenario("cmux + all claude.ai connectors", project, lambda cat: Selection(cmux=True, connectors=conns(cat))),
        Scenario("single connector (Todoist)", project, one("Todoist")),
        Scenario("plugins: prompt-tools only", project,
                 lambda cat: Selection(plugins=cat.resolve_plugins(["prompt-tools"])[0])),
        Scenario("~ cwd: none (local-scope gdrive + aws-write-guard must stay off)", home, lambda cat: Selection()),
        Scenario("~ cwd: single connector (Slack) - deny-list must block local-scope gdrive", home, one("Slack")),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="really launch headless claude probes")
    ap.add_argument("--json", metavar="FILE")
    ap.add_argument("--only", metavar="SUBSTR")
    ap.add_argument("--claude-bin", default=shutil.which("claude") or "claude")
    ap.add_argument("--project", default=str(Path(__file__).resolve().parents[1]))
    a = ap.parse_args()
    if not a.yes:
        print("refusing to launch real sessions without --yes", file=sys.stderr)
        return 2
    home = Path.home()
    baselines: dict[str, int] = {}
    outcomes: list[Outcome] = []
    for sc in scenarios(Path(a.project), home):
        if a.only and a.only.lower() not in sc.name.lower() and not sc.name == "none":
            continue
        print(f"… {sc.name}", file=sys.stderr, flush=True)
        outcomes.append(run_one(sc, a.claude_bin, baselines))

    print("| scenario | mode | selected servers | loaded (status) | MCP tools per server | plugins | SessionStart hooks | input tok | result |")
    print("|---|---|---|---|---|---|---|---|---|")
    for o in outcomes:
        loaded = ", ".join(f"{n} ({s})" for n, s in o.loaded_servers) or "—"
        tools = ", ".join(f"{k}: {v}" for k, v in sorted(o.tools_by_server.items())) or "—"
        hooks = f"{o.session_start_hooks}" + (f" (base {o.baseline_hooks})" if o.baseline_hooks is not None else "")
        print(f"| {o.name} | {o.mode} | {', '.join(o.expected_servers) or '—'} | {loaded} | {tools} | "
              f"{', '.join(o.plugins) or '—'} | {hooks} | {o.input_tokens:,} | "
              f"{'PASS' if o.ok else 'FAIL: ' + '; '.join(o.problems)} |")
    total = sum(o.cost_usd for o in outcomes)
    print(f"\n{sum(o.ok for o in outcomes)}/{len(outcomes)} passed · total probe cost ${total:.3f}")
    if a.json:
        Path(a.json).write_text(json.dumps([o.__dict__ for o in outcomes], indent=2))
    return 0 if all(o.ok for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
