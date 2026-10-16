#!/usr/bin/env python3
"""Opt-in end-to-end proof of "no more, no less" (NOT part of the test suite).

The scenarios are built from YOUR catalog (what `cm --list` shows): nothing
selected, the first local server, the first one with companion hooks, two
local servers, the first claude.ai connector, all connectors (+ cmux when
installed), the first plugin, and the same "nothing" / "one connector" checks
from your home directory. Scenarios whose ingredients you don't have are
skipped. For each one it builds the exact launch plan `cm` would use, starts a
real headless session through the same `claude` (cmux's wrapper when run inside
cmux), reads the `system/init` event and compares the loaded MCP servers, MCP
tools, plugins and hook count with the selection.

Each probe is one tiny Haiku request (~1-2 cents), uses
--no-session-persistence and --permission-mode dontAsk, and sets
AGENTMEMORY_SDK_CHILD=1 so AgentMemory hooks don't record anything.

    python scripts/smoke_verify.py --list-scenarios        # free: just show what would run
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
from typing import Callable

from claude_launcher import compat, probe
from claude_launcher.catalog import Catalog, discover
from claude_launcher.launcher import write_session_files
from claude_launcher.plan import Selection, build_plan


@dataclass
class Scenario:
    name: str
    cwd: Path
    build: Callable[[Catalog], Selection | None]  # None -> not applicable for this catalog
    notes: str = ""
    baseline: bool = False  # its SessionStart hook count is the baseline for its cwd; always run


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
    expected_hook_delta: int | None
    input_tokens: int
    cost_usd: float
    ok: bool
    problems: list[str] = field(default_factory=list)


def prefix_for(cat: Catalog, server: str) -> str:
    for c in cat.connectors:
        if c.name == server:
            return c.tool_prefix
    return f"mcp__{server}__"


def session_start_hooks(settings_doc: dict) -> int:
    """Hook commands the session settings add on SessionStart (companion settings of selected servers)."""
    groups = (settings_doc.get("hooks") or {}).get("SessionStart") or []
    return sum(len(g.get("hooks") or []) for g in groups if isinstance(g, dict))


def run_one(sc: Scenario, claude_bin: str, baselines: dict[str, int], cat: Catalog, sel: Selection) -> Outcome:
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
    # plugins may bring their own hooks, so the hook count is only checked without plugins
    delta = None if sel.plugins else session_start_hooks(plan.settings_doc)
    if sc.baseline:
        baselines[str(sc.cwd)] = hooks
        base = hooks
    else:
        base = baselines.get(str(sc.cwd))
    if base is not None and delta is not None and hooks != base + delta:
        problems.append(f"SessionStart hooks {hooks}, expected {base + delta}")
    return Outcome(
        name=sc.name, cwd=str(sc.cwd), mode=plan.mode, expected_servers=expected,
        loaded_servers=[list(x) for x in res.servers], tools_by_server=res.tools_by_server(),
        unexpected_tools=unexpected, plugins=user_plugins, expected_plugins=sorted(sel.plugins),
        session_start_hooks=hooks, baseline_hooks=base, expected_hook_delta=delta,
        input_tokens=res.input_tokens, cost_usd=res.cost_usd, ok=not problems, problems=problems,
    )


def scenarios(project: Path, home: Path) -> list[Scenario]:
    """Generic scenarios; each picks its ingredients from the catalog it runs against."""

    def first_local(cat: Catalog) -> Selection | None:
        return Selection(local=[cat.local[0].name]) if cat.local else None

    def first_with_hooks(cat: Catalog) -> Selection | None:
        hooked = [s.name for s in cat.local if s.settings_path]
        if not hooked or (cat.local and hooked[0] == cat.local[0].name):
            return None  # none, or already covered by "first local server"
        return Selection(local=[hooked[0]])

    def two_local(cat: Catalog) -> Selection | None:
        return Selection(local=[s.name for s in cat.local[:2]]) if len(cat.local) >= 2 else None

    def verified(cat: Catalog) -> list[str]:
        return [c.name for c in cat.connectors if c.verified]

    def first_connector(cat: Catalog) -> Selection | None:
        conns = verified(cat)
        return Selection(connectors=conns[:1]) if conns else None

    def all_connectors(cat: Catalog) -> Selection | None:
        conns = verified(cat)
        if not conns and not cat.cmux.available:
            return None
        return Selection(connectors=conns, cmux=cat.cmux.available)

    def first_plugin(cat: Catalog) -> Selection | None:
        return Selection(plugins=[cat.plugins[0].id]) if cat.plugins else None

    return [
        Scenario("none", project, lambda cat: Selection(), baseline=True),
        Scenario("first local server", project, first_local),
        Scenario("first local server with companion hooks", project, first_with_hooks),
        Scenario("two local servers", project, two_local),
        Scenario("first claude.ai connector (deny-list)", project, first_connector),
        Scenario("all claude.ai connectors + cmux when available", project, all_connectors),
        Scenario("first plugin only", project, first_plugin),
        Scenario("~ cwd: none (local-scope servers and local plugins must stay off)", home, lambda cat: Selection(),
                 baseline=True),
        Scenario("~ cwd: first connector (deny-list must block local-scope servers)", home, first_connector),
    ]


def wanted(sc: Scenario, only: str | None) -> bool:
    """--only SUBSTR keeps the matching scenarios plus the baselines their hook check needs."""
    return not only or sc.baseline or only.lower() in sc.name.lower()


def describe(sel: Selection) -> str:
    parts = sel.local + [c.removeprefix("claude.ai ") for c in sel.connectors] + sel.plugins
    if sel.cmux:
        parts.append("cmux-cua")
    return ", ".join(parts) or "nothing"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="really launch headless claude probes")
    ap.add_argument("--json", metavar="FILE")
    ap.add_argument("--only", metavar="SUBSTR")
    ap.add_argument("--claude-bin", default=shutil.which("claude") or "claude")
    ap.add_argument("--project", default=str(Path.cwd()), help="project directory to test from (default: cwd)")
    ap.add_argument("--list-scenarios", action="store_true", help="show the scenarios for your catalog; launch nothing")
    a = ap.parse_args()
    if not (a.yes or a.list_scenarios):
        print("refusing to launch real sessions without --yes (or use --list-scenarios)", file=sys.stderr)
        return 2
    home = Path.home()
    version = compat.installed_version(a.claude_bin)
    print(f"Claude Code {version or '?'} · control matrix verified on {', '.join(compat.VERIFIED_VERSIONS)}",
          file=sys.stderr)
    baselines: dict[str, int] = {}
    outcomes: list[Outcome] = []
    catalogs: dict[Path, Catalog] = {}
    for sc in scenarios(Path(a.project).resolve(), home):
        if not wanted(sc, a.only):
            continue
        if sc.cwd not in catalogs:
            catalogs[sc.cwd] = discover(sc.cwd)
        cat = catalogs[sc.cwd]
        sel = sc.build(cat)
        if sel is None:
            print(f"- skip {sc.name}: not applicable to your catalog", file=sys.stderr)
            continue
        label = f"{sc.name}: {describe(sel)}"
        if a.list_scenarios:
            print(f"- {label}  [cwd {sc.cwd}]")
            continue
        print(f"… {label}", file=sys.stderr, flush=True)
        sc = Scenario(label, sc.cwd, sc.build, sc.notes, sc.baseline)
        outcomes.append(run_one(sc, a.claude_bin, baselines, cat, sel))
    if a.list_scenarios:
        return 0

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
