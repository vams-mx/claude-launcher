"""`cm` command line.

  cm                              interactive screen (pre-filled from your last launch here, else the default profile)
  cm --profile memory+gdrive      launch a saved profile without the screen
  cm --mcp agentmemory,gdrive     choose local MCP servers
  cm --connectors none|all|Slack,Todoist
  cm --plugins none|all|commit-commands
  cm --cmux / --no-mcp / --list / --where / --continue / --resume [id]
  cm --forget-last                forget the screen's last selection for this project
  cm --resume <id> --fork --name "new name"    resume (or fork) a session, naming it
  cm --sessions [N] [--json]      this project's recent Claude Code sessions (id, name, age)
  cm ... -- <args forwarded verbatim to claude>
  cm setup init                   create ~/.claude-launcher (example config.json, mcps/)
  cm setup agentmemory            install and wire up AgentMemory (opt-in, asks first)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__, compat
from . import config as cfg_mod
from . import costs as costs_mod
from . import profiles, recent, remember, sessions
from .catalog import Catalog, connectors_stale, discover, refresh_connectors
from .paths import config_dir, config_file, legacy_config_dir, managed_dir, tilde
from .launcher import launch
from .plan import MODELS, PlanError, Selection, can_fork, passthrough_name
from .preview import plain, preview_plan
from .redact import redact_server

_RESUME_PICKER = "\0picker"
_SESSIONS_DEFAULT = object()  # `--sessions` given without N: use recent_sessions from config.json


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
        epilog="Arguments after `--` are forwarded verbatim to claude. "
               "Setup: `cm setup init`, `cm setup agentmemory` (see `cm setup --help`).",
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
    sel.add_argument("--fork", action="store_true",
                     help="with --resume <id> or --continue: fork into a new session id (--fork-session)")
    sel.add_argument("--name", metavar="NAME",
                     help="session display name, passed as claude's --name (long form only: -n is --dry-run)")
    sel.add_argument("-i", "--interactive", action="store_true", help="open the screen even with flags given")

    info = p.add_argument_group("catalog, profiles and costs")
    info.add_argument("-l", "--list", action="store_true", help="show everything that can be selected")
    info.add_argument("--where", action="store_true",
                      help="show the config home, the MCP folders scanned and the file each server came from")
    info.add_argument("--sessions", nargs="?", type=int, const=_SESSIONS_DEFAULT, metavar="N",
                      help="list this project's recent Claude Code sessions (id, name, age); "
                           "N defaults to recent_sessions in config.json (3)")
    info.add_argument("--json", action="store_true",
                      help="with --list, --where, --sessions or --dry-run: machine-readable output")
    info.add_argument("-n", "--dry-run", action="store_true", help="print the launch preview, don't start claude")
    info.add_argument("--refresh", action="store_true", help="refresh the claude.ai connector list (claude mcp list)")
    info.add_argument("--measure", nargs="?", const="all", metavar="KEYS",
                      help="opt-in: measure real context cost of each item with cheap headless Haiku launches")
    info.add_argument("--profiles", action="store_true", help="list saved profiles")
    info.add_argument("--save-profile", metavar="NAME", help="save the resulting selection as a profile")
    info.add_argument("--delete-profile", metavar="NAME")
    info.add_argument("--set-default", metavar="NAME", help="make a profile the default ('none' to clear)")
    info.add_argument("--forget-last", action="store_true",
                      help="forget the selection last launched from the screen in this project "
                           "(remember_last in config.json turns remembering off)")
    p.add_argument("--claude-bin", metavar="PATH", default=os.environ.get("CM_CLAUDE_BIN"),
                   help="claude executable (default: `claude` on PATH, i.e. cmux's wrapper inside cmux)")
    p.add_argument("--cwd", metavar="DIR", help=argparse.SUPPRESS)
    p.add_argument("-V", "--version", action="version", version=f"cm {__version__}")
    return p


def _selection_flags_given(a: argparse.Namespace) -> bool:
    return any([a.profile, a.mcp is not None, a.connectors is not None, a.plugins is not None,
                a.cmux is not None, a.no_mcp, a.model, a.skip_permissions, a.cont, a.resume is not None])


def _has_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _err(msg: str) -> None:
    print(f"cm: {msg}", file=sys.stderr)


def resolve_selection(a: argparse.Namespace, cat: Catalog, passthrough: list[str],
                      last: Selection | None = None) -> tuple[Selection, list[str]]:
    """The starting selection: explicit selection flags > --profile > `last` (the last selection
    launched from the screen in this project; the caller passes it only when the screen opens)
    > the default profile > nothing. Flags override fields of a --profile; the last selection
    and the default profile are used only when no selection flag is given."""
    notes: list[str] = []
    if _selection_flags_given(a):
        name = a.profile
    elif last is not None:
        name = None
    else:
        name = profiles.get_default()
    if name:
        base, notes = profiles.reconcile(profiles.load(name), cat)
    elif last is not None and not _selection_flags_given(a):
        base, notes = profiles.reconcile(last, cat)
        notes = [f"last launch: {n}" for n in notes]
        base.permissions = "standard"  # never pre-selected from a remembered launch
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
    sel.fork = bool(a.fork)
    sel.name = a.name.strip() if a.name is not None else None
    if passthrough:
        sel.extra_args = passthrough
    return sel, notes


def print_list(cat: Catalog, as_json: bool, notes: list[str] | None = None) -> None:
    costs = costs_mod.load()
    notes = list(notes or [])
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
            "cmux": {"installed": cat.cmux.installed, "available": cat.cmux.available, "note": cat.cmux.reason,
                     "in_cmux": cat.cmux.in_cmux,
                     "token": cat.cmux.token_source, "cost": vars(costs["cmux"]) if "cmux" in costs else None},
            "profiles": profiles.list_profiles(), "default_profile": profiles.get_default(),
            "warnings": cat.warnings + notes,
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
    if st.installed:  # no cmux row at all where cmux isn't installed
        t.add_row("cmux", "cmux-cua", ("available" if st.available else "unavailable") +
                  (f" · {st.reason}" if st.reason else ""), costs_mod.fmt(costs.get("cmux")))
    con.print(t)
    refreshed = cat.connectors_refreshed_at or "never (run cm --refresh)"
    con.print(f"[dim]connectors refreshed: {refreshed} · profiles: {', '.join(profiles.list_profiles()) or 'none'}"
              f" · default: {profiles.get_default() or 'none'}[/dim]")
    for w in cat.warnings + notes:
        con.print(f"• {w}", markup=False, style="yellow")


def print_sessions(cwd: Path, limit: int, as_json: bool) -> int:
    """Names, ids and ages only: never conversation content (the fallback name is a shortened first prompt)."""
    res = recent.list_recent(cwd, limit)
    if as_json:
        print(json.dumps({"cwd": str(cwd), "status": res.status, "reason": res.reason or None,
                          "sessions": [s.to_json() for s in res.sessions]}, indent=2, ensure_ascii=False))
        return 0
    if res.status != "ok":
        msg = f"no recent sessions for {tilde(cwd)}: {res.reason}" if res.status == "empty" else \
            f"recent sessions unavailable: {res.reason}"
        print(msg, file=sys.stderr)
        return 0
    marks = {"ai": "  (AI title)", "prompt": "  (first prompt)", "none": "  (no name)"}
    ages = [s.age() for s in res.sessions]
    w = max(len(x) for x in ages)
    for s, ag in zip(res.sessions, ages, strict=True):
        print(f"{s.id}  {ag:<{w}}  {s.name}{marks.get(s.source, '')}")
    return 0


def _describe_source(source: str) -> str:
    if source.startswith(("file:", "project:")):
        return source.split(":", 1)[1]
    if source == "local (this project)":
        return "~/.claude.json (local scope, this project)"
    if source.startswith("local:"):
        return f"~/.claude.json (local scope of {source[len('local:'):]})"
    if source.startswith("user"):
        return "~/.claude.json (user scope)"
    return source


def _server_files(folder: Path) -> int:
    n = 0
    for f in folder.glob("*.json"):
        if f.name.endswith((".settings.json", "-settings.json")):
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        n += isinstance(doc, dict) and isinstance(doc.get("mcpServers"), dict)
    return n


def _last_info(cwd: Path) -> dict:
    root = remember.project_root(cwd)
    path = remember.entry_path(root)
    warnings: list[str] = []
    return {"project": tilde(root), "file": tilde(path), "exists": path.is_file(),
            "enabled": cfg_mod.remember_last(cfg_mod.load_config(), warnings), "warnings": warnings}


def print_where(cat: Catalog, as_json: bool, claude_bin: str, version: str | None) -> None:
    """Paths only: never server configs or env values."""
    legacy = legacy_config_dir()
    servers = [{"name": s.name, "from": _describe_source(s.source),
                "settings": tilde(s.settings_path) if s.settings_path else None,
                "shadowed": [_describe_source(x) for x in s.also_in]} for s in cat.local]
    folders = [{"path": tilde(f), "exists": f.is_dir(), "server_files": _server_files(f) if f.is_dir() else 0}
               for f in cat.mcp_folders]
    doc = {
        "config_dir": tilde(config_dir()),
        "config_dir_from": "CM_CONFIG_DIR" if os.environ.get("CM_CONFIG_DIR") else "default",
        "config_file": tilde(config_file()), "config_file_exists": config_file().is_file(),
        "legacy_config_dir": tilde(legacy) if legacy.is_dir() else None,
        "sessions_dir": tilde(sessions.sessions_dir()), "sessions": sessions.count(),
        "sessions_dir_from": "CM_SESSIONS_DIR" if os.environ.get("CM_SESSIONS_DIR") else "default",
        "sessions_synced_folder": sessions.synced_folder(),
        "session_retention_days": sessions.retention_days(cfg_mod.load_config()),
        "last_selection": _last_info(cat.cwd),
        "managed_dir": str(managed_dir()),
        "claude": {"bin": claude_bin, "version": version, "verified_on": list(compat.VERIFIED_VERSIONS),
                   "warning": compat.version_warning(version)},
        "mcp_folders": folders, "sources": [tilde(f) for f in cat.source_files], "servers": servers,
    }
    if as_json:
        print(json.dumps(doc, indent=2))
        return
    print(f"config home   {doc['config_dir']}" + ("  (from CM_CONFIG_DIR)" if doc["config_dir_from"] != "default" else ""))
    print(f"config file   {doc['config_file']}" + ("" if doc["config_file_exists"] else "  (not created yet: cm setup init)"))
    if doc["legacy_config_dir"]:
        print(f"old home      {doc['legacy_config_dir']}  (no longer read; safe to delete once you're happy)")
    keep = doc["session_retention_days"]
    print(f"sessions      {doc['sessions_dir']}  ({doc['sessions']} kept; "
          + (f"removed after {keep:g} days unused" if keep else "kept forever (session_retention_days: 0)")
          + "; per machine, don't copy)" + ("  (from CM_SESSIONS_DIR)" if doc["sessions_dir_from"] != "default" else ""))
    if doc["sessions_synced_folder"]:
        print(f"              • inside a synced folder ({doc['sessions_synced_folder']}): session files are per machine, "
              "and one machine's cleanup can delete another's. Set CM_SESSIONS_DIR to a local directory.")
    last = doc["last_selection"]
    state = "" if last["enabled"] else "; remember_last is off"
    if last["exists"]:
        print(f"last launch   {last['file']}  (this project: {last['project']}; per machine; "
              f"cm --forget-last deletes it{state})")
    else:
        print(f"last launch   none for {last['project']} (would be {last['file']}{state})")
    for w in last["warnings"]:
        print(f"              • {w}")
    print(f"managed dir   {doc['managed_dir']}")
    print(f"claude        {claude_bin} ({version or 'version unknown'}; control matrix verified on "
          f"{', '.join(compat.VERIFIED_VERSIONS)})")
    if doc["claude"]["warning"]:
        print(f"              • {doc['claude']['warning']}")
    print("\nMCP folders (scanned in order; the first definition of a server name wins)")
    for i, f in enumerate(folders, 1):
        state = f"{f['server_files']} server file(s)" if f["exists"] else "missing"
        print(f"  {i}. {f['path']}  [{state}]")
    if not folders:
        print("  (none: mcp_folders is empty)")
    print("Explicit sources")
    for f in doc["sources"] or ["(none)"]:
        print(f"  {f}")
    print("Servers and where each came from")
    width = max([len(x["name"]) for x in servers] + [4])
    for x in servers:
        extra = f"  + settings {x['settings']}" if x["settings"] else ""
        print(f"  {x['name']:<{width}}  {x['from']}{extra}")
        for sh in x["shadowed"]:
            print(f"  {'':<{width}}  (also defined, ignored: {sh})")
    if not servers:
        print("  (none)")
    for w in cat.warnings:
        print(f"• {w}")


def build_setup_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cm setup", description="One-time setup steps (safe to re-run).")
    sub = p.add_subparsers(dest="what", required=True)
    sub.add_parser("init", help="create the config home with a commented config.json and an empty mcps/ folder "
                                "(migrates ~/.config/claude-launcher once); never overwrites")
    am = sub.add_parser("agentmemory", help="install AgentMemory and generate its cm MCP + hooks files",
                        description="Gated by INSTALL_AGENT_MEMORY (env var, else config.json): unset = ask once, "
                                    "0 = skip, 1 = run. Every impactful step is confirmed first.")
    am.add_argument("--from-installer", action="store_true", help=argparse.SUPPRESS)
    am.add_argument("--no-service", action="store_true", help="don't offer to register a launchd/systemd service")
    return p


def setup_main(argv: list[str]) -> int:
    a = build_setup_parser().parse_args(argv)
    if a.what == "init":
        try:
            created = cfg_mod.init_home()
        except OSError as exc:
            _err(f"setup init failed: {exc}")
            return 1
        print(f"cm home {tilde(config_dir())}: " + (("created " + ", ".join(created)) if created else "already set up"))
        return 0
    from .agentmemory import run_setup

    if a.from_installer:
        cfg_mod.init_home()
    return run_setup(from_installer=a.from_installer, with_service=not a.no_service)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    own, passthrough = _split_passthrough(raw)
    is_setup = own[:1] == ["setup"]
    a = None if is_setup else build_parser().parse_args(own)
    try:
        cfg_mod.migrate_legacy()
    except OSError as exc:
        # Stop here: carrying on would let the first state write create the new home,
        # after which the migration would never be retried.
        _err(f"could not migrate {tilde(legacy_config_dir())} -> {tilde(config_dir())}: {exc}\n"
             "    Nothing was changed. Fix the cause and run cm again (or set CM_CONFIG_DIR).")
        return 1
    if is_setup:
        return setup_main(own[1:])
    assert a is not None
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

    if a.forget_last:
        try:
            root, path, existed = remember.forget(cwd)
        except OSError as exc:
            _err(f"could not forget the last selection: {exc}")
            return 1
        print(f"forgot the last selection for {tilde(root)}" if existed
              else f"no last selection remembered for {tilde(root)}")
        return 0

    if a.sessions is not None:
        cfg_warnings: list[str] = []
        # recent_sessions: 0 only hides the screen's selector; an explicit `cm --sessions` still lists
        if a.sessions is _SESSIONS_DEFAULT:
            n = cfg_mod.recent_sessions(cfg_mod.load_config(), cfg_warnings) or cfg_mod.DEFAULT_RECENT_SESSIONS
        elif a.sessions < 0:
            _err("--sessions N must be 0 or more")
            return 2
        else:
            n = a.sessions
        for w in cfg_warnings:
            _err(w)
        return print_sessions(cwd, min(n, cfg_mod.MAX_RECENT_SESSIONS), a.json)

    if a.name is not None and not a.name.strip():
        _err("--name needs a non-empty name")
        return 2
    if a.name is not None and passthrough_name(passthrough):
        _err("the session is named twice (--name and a --name/-n after --); keep only one")
        return 2

    if a.refresh:
        try:
            found = refresh_connectors(claude_bin)
        except Exception as exc:  # noqa: BLE001
            _err(f"refresh failed: {exc}")
            return 1
        print(f"{len(found)} claude.ai connectors: " + ", ".join(n for n, _ in found))
        if not (a.list or a.where or a.measure or a.dry_run or a.interactive or a.save_profile or _selection_flags_given(a)):
            return 0

    cat = discover(cwd)

    if a.where:
        print_where(cat, a.json, claude_bin, compat.installed_version(claude_bin))
        return 0

    if a.list:
        vw = compat.check(claude_bin)
        print_list(cat, a.json, [vw] if vw else [])
        return 0

    if a.measure:
        keys = costs_mod.item_keys(cat) if a.measure == "all" else _csv(a.measure)
        print(f"measuring {len(keys)} item(s) with headless Haiku probes (each costs ~1-2¢)…", file=sys.stderr)
        try:
            res, base = costs_mod.measure(cat, keys, claude_bin=claude_bin, strict=a.measure != "all",
                                          progress=lambda m: print(f"  · {m}", file=sys.stderr))
        except Exception as exc:  # noqa: BLE001
            _err(str(exc))
            return 1
        print(f"baseline: {base:,} input tokens")
        for k in keys:
            print(f"  {k:45s} {costs_mod.fmt(res.get(k))}")
        return 0

    headless = _selection_flags_given(a) and not a.interactive
    screen = not headless and not a.dry_run
    # The last selection: read only when the screen opens with no selection flags, written only
    # after the screen launched claude. Headless launches and --dry-run never touch it.
    remember_on = False
    last: remember.Entry | None = None
    last_notes: list[str] = []
    if screen:
        remember_on = cfg_mod.remember_last(cfg_mod.load_config(), last_notes)
        if remember_on and not _selection_flags_given(a):
            last, warns = remember.load(cwd)
            last_notes += warns
        for w in last_notes:
            _err(f"warning: {w}")

    try:
        sel, notes = resolve_selection(a, cat, passthrough, last.selection if last else None)
    except (PlanError, profiles.ProfileError) as exc:
        _err(str(exc))
        return 2
    if last:
        notes = [f"last launch: {n}" for n in last.notes] + notes
    # checked on the resolved selection, so a profile's session ("continue") counts
    if sel.fork and not can_fork(sel):
        _err("--fork needs a session to fork: --resume <id> or --continue")
        return 2

    if screen:
        if not _has_tty():
            _err("no TTY for the interactive screen; pass selection flags (e.g. --mcp, --profile)")
            return 2
        from .tui import run_tui

        prefill = f"Pre-filled from your last launch in {tilde(last.project)} (Ctrl+N clears)" if last else None
        chosen = run_tui(cat, sel, claude_bin=claude_bin,
                         profile=a.profile or (None if last else profiles.get_default()),
                         notes=last_notes + notes + cat.warnings, auto_refresh=connectors_stale(cat),
                         prefill=prefill)
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
        vw = compat.check(claude_bin)
        if vw:
            plan.warnings.append(vw)
        titles = {}
        if sel.session == "resume" and sel.resume_id and (found := recent.find(cat.cwd, sel.resume_id)):
            titles[found.id] = found.name
        if a.json:
            from .preview import env_line, session_line
            from .redact import redact_mcp_doc, redact_settings

            print(json.dumps({"argv": plan.argv, "env": env_line(plan), "mode": plan.mode,
                              "session": session_line(sel, titles),
                              "session_dir": str(plan.mcp_path.parent),
                              "mcp": redact_mcp_doc(plan.mcp_doc), "settings": redact_settings(plan.settings_doc),
                              "denied": plan.denied, "warnings": plan.warnings}, indent=2))
        else:
            print(plain(cat, sel, plan, costs_mod.load(), show_files=True, titles=titles))
        return 0

    if headless:
        for w in plan.warnings:
            _err(w)
    save_warnings: list[str] = []
    launched_sel = sel

    def remember_launch() -> None:  # runs once claude has started; never while it couldn't
        if w := remember.save(cwd, launched_sel):
            save_warnings.append(w)

    hook = {"on_started": remember_launch} if screen and remember_on else {}
    try:
        return launch(cat, sel, claude_bin=claude_bin, **hook)
    except PlanError as exc:
        _err(str(exc))
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        # printed after claude exits: written while it runs, it would land in the middle of its screen
        for w in save_warnings:
            _err(f"warning: {w}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
