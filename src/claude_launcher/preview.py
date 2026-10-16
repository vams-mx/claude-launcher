"""Human-readable launch preview (secrets redacted)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from rich.text import Text

from . import costs as costs_mod
from .catalog import Catalog
from .plan import LaunchPlan, Selection, build_plan
from .redact import MASK, redact_mcp_doc, redact_settings, shell_join

PREVIEW_DIR = Path(tempfile.gettempdir()) / "cm-session-XXXXXXXX"


def preview_plan(cat: Catalog, sel: Selection, claude_bin: str) -> LaunchPlan:
    return build_plan(cat, sel, claude_bin=claude_bin, session_dir=PREVIEW_DIR)


def env_line(plan: LaunchPlan) -> str:
    return " ".join(f"{k}={MASK if k in plan.secret_env else v}" for k, v in plan.env_overrides.items())


def cost_summary(cat: Catalog, sel: Selection, costs: dict[str, costs_mod.Cost]) -> tuple[str, list[str]]:
    keys = costs_mod.selection_keys(cat, sel)
    tok, tools, missing = costs_mod.estimate(costs, keys)
    if not keys:
        return "+0 tokens · 0 tools (nothing selected)", []
    if len(missing) == len(keys):
        return "not measured yet", missing
    s = f"~{tok:,} tokens · {tools} tools upfront (Tool Search)"
    full = sum((costs[k].full_tokens or costs[k].tokens) for k in keys if k in costs)
    if full > tok:
        s += f" · up to ~{full:,} if every schema gets loaded"
    return s, missing


def render(cat: Catalog, sel: Selection, plan: LaunchPlan, costs: dict[str, costs_mod.Cost]) -> Text:
    t = Text()
    t.append("Command\n", style="bold")
    t.append("  " + env_line(plan) + " \\\n  ", style="dim")
    t.append(shell_join(plan.argv) + "\n", style="cyan")
    mode = "strict (--strict-mcp-config: only listed servers)" if plan.mode == "strict" else \
        "deny-list (connectors selected; everything else blocked by name)"
    t.append("Isolation: ", style="bold")
    t.append(mode + "\n", style="green" if plan.mode == "strict" else "yellow")

    servers = list(plan.mcp_doc["mcpServers"]) + [c for c in sel.connectors]
    t.append("Loads: ", style="bold")
    t.append((", ".join(servers) if servers else "no MCP servers") + "\n")
    enabled = [p for p, on in plan.settings_doc.get("enabledPlugins", {}).items() if on]
    t.append("Plugins: ", style="bold")
    t.append((", ".join(enabled) if enabled else "none (all disabled for this session)") + "\n")
    hooks = plan.settings_doc.get("hooks") or {}
    if hooks:
        t.append("Hooks added: ", style="bold")
        names = list(hooks)
        t.append(f"{sum(len(g) for g in hooks.values())} hook group(s) on {len(names)} events ("
                 + ", ".join(names[:4]) + (", …" if len(names) > 4 else "") + ")\n")
    if plan.denied:
        t.append("Denied by name: ", style="bold")
        t.append(", ".join(plan.denied) + "\n", style="dim")

    summary, missing = cost_summary(cat, sel, costs)
    t.append("Est. context: ", style="bold")
    t.append(summary)
    if missing:
        t.append(f"  (+ {len(missing)} not measured: {', '.join(missing)} → cm --measure)", style="dim")
    t.append("\n")
    if sel.permissions == "skip":
        t.append("⚠ --dangerously-skip-permissions: Claude will run tools without asking\n", style="bold red")
    for w in plan.warnings:
        t.append(f"• {w}\n", style="yellow")
    return t


def plain(cat: Catalog, sel: Selection, plan: LaunchPlan, costs: dict[str, costs_mod.Cost], *,
          show_files: bool = False) -> str:
    import json

    out = render(cat, sel, plan, costs).plain
    if show_files:
        out += "\nmcp.json (0600):\n" + json.dumps(redact_mcp_doc(plan.mcp_doc), indent=2, ensure_ascii=False)
        s = redact_settings(plan.settings_doc)
        if "hooks" in s:
            s["hooks"] = {ev: f"{len(g)} hook group(s)" for ev, g in s["hooks"].items()}
        out += "\nsettings.json (0600):\n" + json.dumps(s, indent=2, ensure_ascii=False) + "\n"
    return out
