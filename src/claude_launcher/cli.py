"""`cm` command line.

  cm                              interactive screen (pre-filled from the default profile)
  cm --profile memory+gdrive      launch a saved profile without the screen
  cm --mcp agentmemory,gdrive     choose local MCP servers
  cm --connectors none|all|Slack,Todoist
  cm --plugins none|all|commit-commands
  cm --cmux / --no-mcp / --list / --continue / --resume [id]
  cm ... -- <args forwarded verbatim to claude>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from . import costs as costs_mod
from . import profiles
from .catalog import Catalog, connectors_stale, discover, refresh_connectors
from .launcher import launch
from .plan import MODELS, PlanError, Selection
from .preview import plain, preview_plan
from .redact import redact_server

_RESUME_PICKER = "\0picker"


def _split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def _csv(v: str) -> list[str]:
    return [x.strip() for x in v.split(",") if x.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cm",
        description="Start Claude Code with exactly the MCP servers, claude.ai connectors, plugins and "
                    "cmux computer use you choose. Without selection flags, opens the interactive screen.",
        epilog="Arguments after `--` are forwarded verbatim to claude.",
    )
    sel = p.add_argument_group("selection (any of these skips the interactive screen)")
    sel.add_argument("--profile", metavar="NAME", help="start from a saved profile")
    sel.add_argument("--mcp", metavar="LIST", help="local MCP servers, comma-separated (or 'none' / 'all')")
    sel.add_argument("--connectors", metavar="LIST", help="claude.ai connectors: none | all | Slack,Todoist")
    sel.add_argument("--plugins", metavar="LIST", help="plugins: none | all | name[,name]")
    sel.add_argument("--cmux", action=argparse.BooleanOptionalAction, default=None,
                     help="cmux computer use on/off")
    sel.add_argument("--no-mcp", action="store_true", help="no local MCPs, no connectors, no cmux")
    sel.add_argument("--model", choices=MODELS, help="model alias (default = your configured model)")
    sel.add_argument("--skip-permissions", action="store_true",
                     help="pass --dangerously-skip-permissions (use with care)")
    g = sel.add_mutually_exclusive_group()
    g.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the last conversation")
    g.add_argument("-r", "--resume", nargs="?", const=_RESUME_PICKER, metavar="ID",
                   help="resume by id, or open claude's native picker")
    sel.add_argument("-i", "--interactive", action="store_true", help="open the screen even with flags given")

    info = p.add_argument_group("catalog, profiles and costs")
    info.add_argument("-l", "--list", action="store_true", help="show everything that can be selected")
    info.add_argument("--json", action="store_true", help="with --list or --dry-run: machine-readable output")
    info.add_argument("-n", "--dry-run", action="store_true", help="print the launch preview, don't start claude")
    info.add_argument("--refresh", action="store_true", help="refresh the claude.ai connector list (claude mcp list)")
    info.add_argument("--measure", nargs="?", const="all", metavar="KEYS",
                      help="opt-in: measure real context cost of each item with cheap headless Haiku launches")
    info.add_argument("--profiles", action="store_true", help="list saved profiles")
    info.add_argument("--save-profile", metavar="NAME", help="save the resulting selection as a profile")
    info.add_argument("--delete-profile", metavar="NAME")
    info.add_argument("--set-default", metavar="NAME", help="make a profile the default ('none' to clear)")
    p.add_argument("--claude-bin", metavar="PATH", default=os.environ.get("CM_CLAUDE_BIN"),
                   help="claude executable (default: `claude` on PATH, i.e. cmux's wrapper inside cmux)")
    p.add_argument("--cwd", metavar="DIR", help=argparse.SUPPRESS)
    p.add_argument("-V", "--version", action="version", version=f"cm {__version__}")
    return p


def _selection_flags_given(a: argparse.Namespace) -> bool:
    return any([a.profile, a.mcp is not None, a.connectors is not None, a.plugins is not None,
                a.cmux is not None, a.no_mcp, a.model, a.skip_permissions, a.cont, a.resume is not None])


def _err(msg: str) -> None:
    print(f"cm: {msg}", file=sys.stderr)


def resolve_selection(a: argparse.Namespace, cat: Catalog, passthrough: list[str]) -> tuple[Selection, list[str]]:
    notes: list[str] = []
    name = a.profile or (None if _selection_flags_given(a) else profiles.get_default())
    if name:
        base, notes = profiles.reconcile(profiles.load(name), cat)
    else:
        base = Selection()
    sel = Selection.from_dict(base.to_dict())

    if a.no_mcp:
        sel.local, sel.connectors, sel.cmux = [], [], False
    if a.mcp is not None:
        if a.mcp.lower() == "none":
            sel.local = []
        elif a.mcp.lower() == "all":
            sel.local = [s.name for s in cat.local]
        else:
            found, missing = cat.resolve_local(_csv(a.mcp))
            if missing:
                raise PlanError(f"unknown MCP server(s): {', '.join(missing)} (see cm --list)")
            sel.local = found
    if a.connectors is not None:
        v = a.connectors.strip().lower()
        if v == "none":
            sel.connectors = []
        elif v == "all":
            sel.connectors = [c.name for c in cat.connectors if c.verified] or [c.name for c in cat.connectors]
        else:
            found, missing = cat.resolve_connectors(_csv(a.connectors))
            if missing:
                raise PlanError(f"unknown connector(s): {', '.join(missing)} (see cm --list, cm --refresh)")
            sel.connectors = found
    if a.plugins is not None:
        v = a.plugins.strip().lower()
        if v == "none":
            sel.plugins = []
        elif v == "all":
            sel.plugins = [p.id for p in cat.plugins]
        else:
            found, missing = cat.resolve_plugins(_csv(a.plugins))
            if missing:
                raise PlanError(f"unknown plugin(s): {', '.join(missing)}")
            sel.plugins = found
    if a.cmux is not None:
        sel.cmux = a.cmux
    if a.model:
        sel.model = a.model
    if a.skip_permissions:
        sel.permissions = "skip"
    if a.cont:
        sel.session, sel.resume_id = "continue", None
    if a.resume is not None:
        sel.session = "resume"
        sel.resume_id = None if a.resume == _RESUME_PICKER else a.resume
    if passthrough:
        sel.extra_args = passthrough
    return sel, notes


def print_list(cat: Catalog, as_json: bool) -> None:
    costs = costs_mod.load()
    if as_json:
        doc = {
            "cwd": str(cat.cwd),
            "local": [{"name": s.name, "source": s.source, "transport": s.transport,
                       "config": redact_server(s.config), "hooks": str(s.settings_path) if s.settings_path else None,
                       "cost": vars(costs[s.key]) if s.key in costs else None} for s in cat.local],
            "connectors": [{"name": c.name, "short": c.short, "status": c.status, "verified": c.verified,
                            "cost": vars(costs[c.key]) if c.key in costs else None} for c in cat.connectors],
            "connectors_refreshed_at": cat.connectors_refreshed_at,
            "plugins": [{"id": p.id, "enabled_in": p.enabled_in, "mcp_servers": p.mcp_servers,
                         "cost": vars(costs[p.key]) if p.key in costs else None} for p in cat.plugins],
            "cmux": {"available": cat.cmux.available, "note": cat.cmux.reason, "in_cmux": cat.cmux.in_cmux,
                     "token": cat.cmux.token_source, "cost": vars(costs["cmux"]) if "cmux" in costs else None},
            "profiles": profiles.list_profiles(), "default_profile": profiles.get_default(),
            "warnings": cat.warnings,
        }
        print(json.dumps(doc, indent=2))
        return

    from rich.console import Console
    from rich.table import Table

    from . import health

    con = Console()
    t = Table(title=f"cm catalog · {cat.cwd}", show_lines=False, header_style="bold", expand=False)
    t.add_column("group", style="cyan", no_wrap=True)
    t.add_column("name", style="bold", no_wrap=True, max_width=28)
    t.add_column("details", overflow="fold", ratio=3)
    t.add_column("context", style="magenta", overflow="fold", ratio=1, min_width=14)
    for s in cat.local:
        extra = f" · hooks: {s.settings_path.name}" if s.settings_path else ""
        h = health.check(s)
        t.add_row("local MCP", s.name, f"{s.summary()} [{s.source}]{extra}{' · ' + h if h else ''}",
                  costs_mod.fmt(costs.get(s.key)))
    for c in cat.connectors:
        t.add_row("claude.ai", c.short, c.status + ("" if c.verified else " · kept on the deny list only"),
                  costs_mod.fmt(costs.get(c.key)))
    for p in cat.plugins:
        t.add_row("plugin", p.id, "enabled in: " + "/".join(p.enabled_in) +
                  (f" · MCP: {', '.join(p.mcp_servers)}" if p.mcp_servers else ""), costs_mod.fmt(costs.get(p.key)))
    st = cat.cmux
    t.add_row("cmux", "cmux-cua", ("available" if st.available else "unavailable") +
              (f" · {st.reason}" if st.reason else ""), costs_mod.fmt(costs.get("cmux")))
    con.print(t)
    refreshed = cat.connectors_refreshed_at or "never (run cm --refresh)"
    con.print(f"[dim]connectors refreshed: {refreshed} · profiles: {', '.join(profiles.list_profiles()) or 'none'}"
              f" · default: {profiles.get_default() or 'none'}[/dim]")
    for w in cat.warnings:
        con.print(f"[yellow]• {w}[/yellow]")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    own, passthrough = _split_passthrough(raw)
    a = build_parser().parse_args(own)
    claude_bin = a.claude_bin or shutil.which("claude") or "claude"
    cwd = Path(a.cwd).resolve() if a.cwd else Path.cwd()

    # --- profile management that needs no catalog -------------------------------
    try:
        if a.delete_profile:
            profiles.delete(a.delete_profile)
            print(f"deleted profile {a.delete_profile}")
            return 0
        if a.set_default:
            profiles.set_default(None if a.set_default.lower() == "none" else a.set_default)
            print(f"default profile: {profiles.get_default() or 'none'}")
            return 0
        if a.profiles:
            d = profiles.get_default()
            for n in profiles.list_profiles():
                print(f"{n}{'  (default)' if n == d else ''}")
            return 0
    except profiles.ProfileError as exc:
        _err(str(exc))
        return 2

    if a.refresh:
        try:
            found = refresh_connectors(claude_bin)
        except Exception as exc:  # noqa: BLE001
            _err(f"refresh failed: {exc}")
            return 1
        print(f"{len(found)} claude.ai connectors: " + ", ".join(n for n, _ in found))
        if not (a.list or a.measure or _selection_flags_given(a)):
            return 0

    cat = discover(cwd)

    if a.list:
        print_list(cat, a.json)
        return 0

    if a.measure:
        keys = costs_mod.item_keys(cat) if a.measure == "all" else _csv(a.measure)
        print(f"measuring {len(keys)} item(s) with headless Haiku probes (each costs ~1-2¢)…", file=sys.stderr)
        try:
            res, base = costs_mod.measure(cat, keys, claude_bin=claude_bin,
                                          progress=lambda m: print(f"  · {m}", file=sys.stderr))
        except Exception as exc:  # noqa: BLE001
            _err(str(exc))
            return 1
        print(f"baseline: {base:,} input tokens")
        for k in keys:
            print(f"  {k:45s} {costs_mod.fmt(res.get(k))}")
        return 0

    try:
        sel, notes = resolve_selection(a, cat, passthrough)
    except (PlanError, profiles.ProfileError) as exc:
        _err(str(exc))
        return 2
    headless = _selection_flags_given(a) and not a.interactive

    if not headless and not a.dry_run:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            _err("no TTY for the interactive screen; pass selection flags (e.g. --mcp, --profile)")
            return 2
        from .tui import run_tui

        chosen = run_tui(cat, sel, claude_bin=claude_bin, profile=a.profile or profiles.get_default(),
                         notes=notes + cat.warnings, auto_refresh=connectors_stale(cat))
        if chosen is None:
            return 0
        sel = chosen
    else:
        for n in notes:
            _err(f"profile: {n}")

    try:
        plan = preview_plan(cat, sel, claude_bin)
    except PlanError as exc:
        _err(str(exc))
        return 2

    if a.save_profile:
        try:
            profiles.save(a.save_profile, sel)
            _err(f"saved profile {a.save_profile}")
        except profiles.ProfileError as exc:
            _err(str(exc))
            return 2

    if a.dry_run:
        if a.json:
            from .preview import env_line
            from .redact import redact_mcp_doc, redact_settings

            print(json.dumps({"argv": plan.argv, "env": env_line(plan), "mode": plan.mode,
                              "mcp": redact_mcp_doc(plan.mcp_doc), "settings": redact_settings(plan.settings_doc),
                              "denied": plan.denied, "warnings": plan.warnings}, indent=2))
        else:
            print(plain(cat, sel, plan, costs_mod.load(), show_files=True))
        return 0

    if headless:
        for w in plan.warnings:
            _err(w)
    try:
        return launch(cat, sel, claude_bin=claude_bin)
    except PlanError as exc:
        _err(str(exc))
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
