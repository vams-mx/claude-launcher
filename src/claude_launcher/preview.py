"""Human-readable launch preview (secrets redacted)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from rich.text import Text

from . import awake as awake_mod
from . import costs as costs_mod
from . import sessions as sessions_mod
from . import worktree as wt_mod
from .catalog import Catalog
from .paths import tilde
from .plan import LaunchPlan, PlanError, Selection, build_plan, passthrough_agent, passthrough_name, session_name
from .redact import MASK, redact_mcp_doc, redact_settings, shell_join


def preview_plan(cat: Catalog, sel: Selection, claude_bin: str) -> LaunchPlan:
    """The exact plan a launch would use, including its real session file paths
    (content-addressed, so they are known before anything is written)."""
    return build_plan(cat, sel, claude_bin=claude_bin)


def session_note(plan: LaunchPlan) -> str:
    d = plan.mcp_path.parent
    state = "reused" if (d / "mcp.json").is_file() and (d / "settings.json").is_file() else "new"
    if not sessions_mod.is_session_name(d.name):  # a throwaway dir (measurement probes, smoke script)
        return f"{tilde(d)}/ (temporary)"
    return f"{tilde(d)}/ ({state}; kept after exit for cmux restore; each start or resume marks it used)"


def session_line(sel: Selection, titles: Mapping[str, str] | None = None) -> str:
    """new / continue / resume <name> / fork of <name>, then → "<new name>" when one is passed.
    `titles` maps session ids to their current names (unknown ids show as a short id)."""
    titles = titles or {}
    if sel.session == "continue":
        base = "fork of the last session" if sel.fork else "continue the last session"
    elif sel.session == "resume" and sel.resume_id:
        label = titles.get(sel.resume_id) or sel.resume_id[:8]
        base = f"{'fork of' if sel.fork else 'resume'} {label}"
    elif sel.session == "resume":
        base = "resume (claude's picker)"
    else:
        base = "new"
    try:
        name = session_name(sel)
    except PlanError:
        name = None
    existing = (sel.session == "resume" and sel.resume_id and not sel.fork and name
                and (title := titles.get(sel.resume_id)) and same_name(name, title))
    if existing:
        # resuming a session under the name it already has (e.g. `cm --name backoffice`)
        line = f"{base} (existing session)" + (f' → "{name}"' if name != titles[sel.resume_id] else "")
    elif name:
        line = f'{base} → "{name}"'
    elif passthrough_name(sel.extra_args):
        line = f"{base} (named by the arguments after --)"
    else:
        line = f"{base} (unnamed)" if sel.session == "new" or sel.fork else base
    if sel.worktree:
        line += f" in worktree {sel.worktree}" + (" (tmux)" if sel.tmux else "")
    if sel.launch_dir:
        line += f" · runs in {Path(sel.launch_dir).name} ({tilde(Path(sel.launch_dir))})"
    return line


def agent_line(cat: Catalog, sel: Selection) -> str:
    if not sel.agent:
        if passthrough_agent(sel.extra_args):
            return "given by the arguments after --"
        if sel.session_agent and sel.session != "new":
            return f"{sel.session_agent} (restored by the resumed session)"
        return f"default ({cat.settings_agent} from settings)" if cat.settings_agent else "default"
    ag = cat.agent(sel.agent)
    if ag is None:
        return f"{sel.agent} (not found here)"
    line = f"{ag.name} ({ag.source})"
    if ag.plugin_id:
        line += f" · needs plugin {ag.plugin_id}, enabled for this session"
    return line


def same_name(a: str, b: str) -> bool:
    """Session names compared like `cm --name` finds a session: trimmed, case-insensitive."""
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def env_line(plan: LaunchPlan) -> str:
    return " ".join(f"{k}={MASK if k in plan.secret_env else v}" for k, v in plan.env_overrides.items())


def cost_summary(cat: Catalog, sel: Selection, costs: dict[str, costs_mod.Cost]) -> tuple[str, list[str]]:
    keys = costs_mod.selection_keys(cat, sel)
    tok, tools, missing = costs_mod.estimate(costs, keys)
    # an unmeasured skill pack is estimated from its skills' descriptions instead
    est = 0
    for k in [k for k in missing if k.startswith("pack:")]:
        if (pack := cat.skill_pack(k[len("pack:"):])) is not None:
            est += pack.estimated_tokens()
            missing.remove(k)
    tok += est
    if not keys:
        return "+0 tokens · 0 tools (nothing selected)", []
    if len(missing) == len(keys):
        return "not measured yet", missing
    s = f"~{tok:,} tokens · {tools} tools upfront (Tool Search)" + (" (skill packs estimated)" if est else "")
    full = sum((costs[k].full_tokens or costs[k].tokens) for k in keys if k in costs)
    if full > tok:
        s += f" · up to ~{full:,} if every schema gets loaded"
    return s, missing


# Colour roles; the screen passes its theme's colours instead (tui.LauncherApp._palette).
DEFAULT_PALETTE = {"cmd": "cyan", "ok": "green", "warn": "yellow", "bad": "red", "cost": "magenta"}


def render(cat: Catalog, sel: Selection, plan: LaunchPlan, costs: dict[str, costs_mod.Cost],
           palette: dict[str, str] | None = None, titles: Mapping[str, str] | None = None,
           repo: wt_mod.Repo | None = None, power: str | None = None) -> Text:
    """`power`: "battery" / "ac" / None (unknown), from awake.power_source (read once by the caller)."""
    c = palette or DEFAULT_PALETTE
    t = Text()
    t.append("Command\n", style="bold")
    t.append("  " + env_line(plan) + " \\\n  ", style="dim")
    t.append(shell_join(plan.argv) + "\n", style=c["cmd"])
    mode = "strict (--strict-mcp-config: only listed servers)" if plan.mode == "strict" else \
        "deny-list (connectors selected; everything else blocked by name)"
    t.append("Isolation: ", style="bold")
    t.append(mode + "\n", style=c["ok"] if plan.mode == "strict" else c["warn"])
    t.append("Session: ", style="bold")
    t.append(session_line(sel, titles) + "\n")
    if sel.worktree:
        repo = repo or wt_mod.find_repo(cat.cwd)
        st = wt_mod.inspect(repo, sel.worktree) if repo else None
        t.append("Worktree: ", style="bold")
        if st is None:
            t.append(f"{sel.worktree} (not inside a git repository?)\n", style=c["warn"])
        else:
            t.append(f"{tilde(st.path)} · branch {st.branch}", style="dim")
            t.append(f"  {st.message}\n" if st.exists else "  (new)\n",
                     style=c["warn"] if st.exists else "dim")
    if sel.awake != "off" and awake_mod.supported():  # off macOS a stored value is ignored silently
        t.append("Keep awake: ", style="bold")
        t.append(awake_mod.describe(sel.awake) + "\n")
        if power == "battery":
            t.append(f"  ⚠ {awake_mod.BATTERY_NOTE}\n", style=c["warn"])
    t.append("Session files: ", style="bold")
    t.append(session_note(plan) + "\n", style="dim")

    servers = list(plan.mcp_doc["mcpServers"]) + [c for c in sel.connectors]
    t.append("Loads: ", style="bold")
    t.append((", ".join(servers) if servers else "no MCP servers") + "\n")
    enabled = [p for p, on in plan.settings_doc.get("enabledPlugins", {}).items() if on]
    t.append("Plugins: ", style="bold")
    t.append((", ".join(enabled) if enabled else "none (all disabled for this session)") + "\n")
    if sel.skill_packs:
        t.append("Skill packs: ", style="bold")
        t.append(", ".join(f"{n} ({len(pk.skills)} skills)" if (pk := cat.skill_pack(n)) else n
                           for n in sel.skill_packs) + " (this session only, --add-dir)\n")
    t.append("Agent: ", style="bold")
    t.append(agent_line(cat, sel) + "\n", style="" if sel.agent else "dim")
    hooks = sessions_mod.without_touch_hook(plan.settings_doc).get("hooks") or {}  # cm's own hook isn't news
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
        t.append("⚠ --dangerously-skip-permissions: Claude will run tools without asking\n", style=f"bold {c['bad']}")
    for w in plan.warnings:
        t.append(f"• {w}\n", style=c["warn"])
    return t


def plain(cat: Catalog, sel: Selection, plan: LaunchPlan, costs: dict[str, costs_mod.Cost], *,
          show_files: bool = False, titles: Mapping[str, str] | None = None, power: str | None = None) -> str:
    import json

    out = render(cat, sel, plan, costs, titles=titles, power=power).plain
    if show_files:
        out += f"\n{tilde(plan.mcp_path)} (0600):\n" + json.dumps(redact_mcp_doc(plan.mcp_doc), indent=2,
                                                                 ensure_ascii=False)
        s = redact_settings(plan.settings_doc)
        if "hooks" in s:
            s["hooks"] = {ev: f"{len(g)} hook group(s)" for ev, g in s["hooks"].items()}
        out += f"\n{tilde(plan.settings_path)} (0600):\n" + json.dumps(s, indent=2, ensure_ascii=False) + "\n"
    return out
