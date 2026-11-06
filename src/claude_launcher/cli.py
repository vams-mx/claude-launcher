"""`cm` command line.

  cm                              interactive screen (pre-filled from your last launch here, resuming its session,
                                  else the default profile)
  cm --name backoffice            the screen, resuming this project's session named "backoffice" if there is one
  cm --profile memory+gdrive      launch a saved profile without the screen
  cm --mcp agentmemory,gdrive     choose local MCP servers
  cm --connectors none|all|Slack,Todoist
  cm --plugins none|all|commit-commands
  cm --cmux / --no-mcp / --list / --where / --continue / --resume [id]
  cm --forget-last                forget the screen's last selection for this project
  cm --resume <id> --fork --name "new name"    resume (or fork) a session, naming it
  cm --resume <id> --fork --name "X v2" --worktree [--tmux]   fork it into its own git worktree (claude -w x-v2)
  cm --sessions [N] [--json]      this project's recent Claude Code sessions (id, name, age)
  cm --awake ims|dims|off         macOS: keep the Mac awake while claude runs (caffeinate -w <claude pid>)
  cm --agent NAME                 the session's agent (claude --agent), e.g. a plugin's "team:coordinator"
  cm --skill-packs playwright     skill packs for this session only (--plugin-dir each)
  cm ... -- <args forwarded verbatim to claude>
  cm setup init                   create ~/.claude-launcher (example config.json, mcps/)
  cm setup agentmemory            install and wire up AgentMemory (opt-in, asks first)
  cm plugins available [--json]   plugins in your configured marketplaces (installed ones marked)
  cm plugins install P@M [--scope user|project|local]   claude plugin install, never auto-accepting
  cm skills list | add NAME --from DIR [--link] | remove NAME   manage skill packs
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__, compat
from . import awake as awake_mod
from . import config as cfg_mod
from . import costs as costs_mod
from . import marketplace, profiles, recent, remember, sessions, skillpacks
from . import worktree as wt_mod
from .catalog import Catalog, connectors_stale, discover, refresh_connectors
from .paths import config_dir, config_file, legacy_config_dir, managed_dir, tilde
from .launcher import launch
from .plan import (
    MODELS, PlanError, Selection, can_fork, passthrough_agent, passthrough_name, passthrough_session,
    passthrough_worktree, session_name,
)
from .preview import plain, preview_plan
from .redact import redact_server

_RESUME_PICKER = "\0picker"
_WORKTREE_DEFAULT = "\0default"
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
               "Setup: `cm setup init`, `cm setup agentmemory` (see `cm setup --help`). "
               "Plugins and skill packs: `cm plugins --help`, `cm skills --help`.",
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
    sel.add_argument("--agent", metavar="NAME",
                     help="the session's agent (claude --agent): one of ~/.claude/agents, the project's "
                          ".claude/agents or a plugin's (<plugin>:<agent>, ticks that plugin); 'default' = none")
    sel.add_argument("--skill-packs", metavar="LIST",
                     help="skill packs for this session only: none | all | name[,name] (see cm skills list)")
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
    sel.add_argument("--worktree", nargs="?", const=_WORKTREE_DEFAULT, metavar="NAME",
                     help="start in a new git worktree (claude -w). NAME defaults to the session name as a "
                          "slug (--name \"X (fork)\" -> x-fork), else cm-<date>-<id>")
    sel.add_argument("--tmux", action="store_true", help="with --worktree: also open a tmux session (claude --tmux)")
    # macOS only: hidden from --help elsewhere (and ignored with a note if given anyway)
    sel.add_argument("--awake", choices=awake_mod.MODES, metavar="{off,ims,dims}",
                     help=("keep the Mac awake while claude runs: ims = no idle/disk/system sleep "
                           "(caffeinate -ims; -s only on AC power), dims = also keep the display on, off = no. "
                           "Ends with claude; doesn't skip the screen by itself (like --name)")
                     if awake_mod.supported() else argparse.SUPPRESS)
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
                a.cmux is not None, a.no_mcp, a.model, a.skip_permissions, a.cont, a.resume is not None,
                a.agent is not None, a.skill_packs is not None])


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
    if a.skill_packs is not None:
        v = a.skill_packs.strip().lower()
        if v == "none":
            sel.skill_packs = []
        elif v == "all":
            sel.skill_packs = [p.name for p in cat.skill_packs]
        else:
            found, missing = cat.resolve_skill_packs(_csv(a.skill_packs))
            if missing:
                raise PlanError(f"unknown skill pack(s): {', '.join(missing)} (see cm skills list)")
            sel.skill_packs = found
    if a.cmux is not None:
        sel.cmux = a.cmux
    if a.model:
        sel.model = a.model
    if a.agent is not None:
        name = a.agent.strip()
        sel.agent = None if name.lower() in ("", "default", "none") else name
    if sel.agent and (ag := cat.agent(sel.agent)) and ag.plugin_id and ag.plugin_id not in sel.plugins:
        if cat.plugin(ag.plugin_id) is not None:
            sel.plugins = [p.id for p in cat.plugins if p.id in sel.plugins or p.id == ag.plugin_id]
            notes.append(f"agent {ag.name} comes with plugin {ag.plugin_id}: enabled it for this session")
    if a.skip_permissions:
        sel.permissions = "skip"
    if a.cont:
        sel.session, sel.resume_id = "continue", None
    if a.resume is not None:
        sel.session = "resume"
        sel.resume_id = None if a.resume == _RESUME_PICKER else a.resume
    sel.fork = bool(a.fork)
    sel.name = a.name.strip() if a.name is not None else None
    # "" = on, name still to be chosen (the screen derives it from Name; headless: _finish_worktree)
    sel.worktree = None if a.worktree is None else ("" if a.worktree == _WORKTREE_DEFAULT else a.worktree.strip())
    sel.tmux = bool(a.tmux)
    if a.awake is not None:  # a launch preference: overrides the profile / last selection's value
        sel.awake = a.awake
    if passthrough:
        sel.extra_args = passthrough
    return sel, notes


def finish_session(cwd: Path, sel: Selection, *, headless: bool = False) -> list[str]:
    """The last per-launch decisions, the same for the screen's result and headless launches:

    * a resumed session of another checkout of the repository (a git worktree) is resumed from
      that checkout;
    * `--worktree` without a name gets the default: the session name as a slug, else
      cm-<date>-<id>.

    Returns warnings about an existing worktree of that name. Headless launches never ask, so
    one where claude would replace something (reset a leftover branch, recreate a broken
    worktree) is refused instead (PlanError); the screen has already asked for a second Enter."""
    notes: list[str] = []
    if sel.session == "resume" and sel.resume_id and not sel.launch_dir:
        found = recent.find(cwd, sel.resume_id)
        if found and found.cwd:
            sel.launch_dir = found.cwd
    if sel.worktree is None:
        return notes
    if sel.worktree == "":
        sel.worktree = wt_mod.default_name(sel.name, wt_mod.generated_name())
    if why := wt_mod.unsupported(sel, wt_mod.find_repo(cwd)):
        raise PlanError(why)
    repo = wt_mod.find_repo(cwd)
    if repo is not None and not wt_mod.valid_name(sel.worktree):
        st = wt_mod.inspect(repo, sel.worktree)
        if st.kind == wt_mod.REPLACE and headless:
            raise PlanError(f"worktree {sel.worktree}: {st.message}. Not launched: choose another --worktree "
                            "name, or confirm it on the screen (cm -i …)")
        if st.kind in (wt_mod.REUSE, wt_mod.REPLACE):
            notes.append(f"worktree {sel.worktree}: {st.message}")
    return notes


# Never remembered, like cm's own Skip permissions and Fork: a permission bypass or a fork must
# be chosen on purpose for each launch, not come back pre-filled.
_NOT_REMEMBERED = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                   "--permission-mode", "--fork-session")


def remembered_extra_args(args: list[str]) -> list[str]:
    """The extra args worth remembering: none when they name the session, ask for a worktree,
    choose the session, fork it or change permissions (those are per launch, like cm's own Name /
    Worktree / Session / Fork / Skip permissions)."""
    if passthrough_name(args) or passthrough_worktree(args) or passthrough_session(args):
        return []
    if any(a in _NOT_REMEMBERED or a.startswith(tuple(f"{f}=" for f in _NOT_REMEMBERED)) for a in args):
        return []
    return list(args)


def known_session(sel: Selection, titles: dict[str, str] | None = None) -> remember.LastSession | None:
    """The session a launch runs when it is known before claude starts: the id being resumed (a
    plain resume, not a fork, with no arguments after `--` choosing another). Everything else is
    found after claude exits (session_from_diff)."""
    if (sel.session != "resume" or not sel.resume_id or sel.fork or passthrough_session(sel.extra_args)
            or "--fork-session" in sel.extra_args):  # a fork after `--` runs a new id: found by the diff
        return None
    try:
        name = session_name(sel)
    except PlanError:
        name = None
    return remember.LastSession(sel.resume_id, name or (titles or {}).get(sel.resume_id), sel.launch_dir)


def session_places(cwd: Path, sel: Selection) -> list[Path]:
    """Where a launch's session file can appear: the directory claude starts in (its other
    checkouts are added by recent.snapshot), and the worktree claude -w creates."""
    where = Path(sel.launch_dir) if sel.launch_dir else cwd
    places = [where]
    if sel.worktree and (repo := wt_mod.find_repo(where)) is not None:
        places.append(repo.root / wt_mod.WORKTREES_DIR / sel.worktree)
    return places


def session_from_diff(before: recent.Snapshot, sel: Selection) -> remember.LastSession | None:
    """New / Continue / fork / claude's picker: the one session file that appeared while claude ran
    (recent.new_session), or None when none or several did. Never raises."""
    try:
        found = recent.new_session(before)
        if found is None:
            return None
        try:
            name = session_name(sel)
        except PlanError:
            name = None
        # a session of the launch directory itself is resumed from there when that is another checkout
        return remember.LastSession(found.id, found.title or name, found.cwd or sel.launch_dir)
    except Exception:  # noqa: BLE001 - remembering must never fail a launch
        return None


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
            "skill_packs": [{"name": p.name, "skills": len(p.skills), "path": str(p.path), "error": p.error,
                             "warnings": p.warnings(), "estimated_tokens": p.estimated_tokens(),
                             "cost": vars(costs[p.key]) if p.key in costs else None} for p in cat.skill_packs],
            "agents": [{"name": a.name, "source": a.source, "description": a.description, "plugin": a.plugin_id,
                        "available": cat.agent_block(a) is None} for a in cat.agents],
            "default_agent": cat.settings_agent,
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
    for pk in cat.skill_packs:
        cost = costs_mod.fmt(costs[pk.key]) if pk.key in costs else f"~{pk.estimated_tokens()} tok (est.)"
        t.add_row("skill pack", pk.name, f"{len(pk.skills)} skill(s)" + (f" · {pk.description}" if pk.description else "")
                  + "".join(f" · {w}" for w in ([pk.error] if pk.error else pk.warnings())), cost)
    for ag in cat.agents:
        block = " · unavailable here" if cat.agent_block(ag) else ""
        t.add_row("agent", ag.name, f"[{ag.source}]{block} " + recent.shorten(ag.description, 100), "")
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
        wt = f"  {s.mark} ({tilde(Path(s.cwd))})" if s.worktree and s.cwd else ""
        print(f"{s.id}  {ag:<{w}}  {s.name}{marks.get(s.source, '')}{wt}")
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


# ---------------------------------------------------------------------------
# cm plugins / cm skills


def build_plugins_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cm plugins", description="Plugins of your configured marketplaces.")
    sub = p.add_subparsers(dest="what", required=True)
    av = sub.add_parser("available", help="list the plugins your marketplaces offer (read-only), installed ones marked")
    av.add_argument("--json", action="store_true")
    av.add_argument("--search", metavar="TEXT", help="only those whose id, description or category match")
    ins = sub.add_parser("install", help="run `claude plugin install <plugin@marketplace> --scope …` "
                                         "(never accepts a marketplace-declared command for you)")
    ins.add_argument("plugin", metavar="PLUGIN@MARKETPLACE")
    ins.add_argument("--scope", choices=marketplace.SCOPES, default="user",
                     help="user (default), project (shared via .claude/settings.json) or local (this project, you only)")
    for x in (p, av, ins):
        x.add_argument("--claude-bin", metavar="PATH", default=os.environ.get("CM_CLAUDE_BIN"), help=argparse.SUPPRESS)
        x.add_argument("--cwd", metavar="DIR", help=argparse.SUPPRESS)
    return p


def plugins_main(argv: list[str]) -> int:
    a = build_plugins_parser().parse_args(argv)
    cwd = Path(a.cwd).resolve() if a.cwd else Path.cwd()
    warnings: list[str] = []
    if a.what == "available":
        items = [x for x in marketplace.available(cwd, warnings) if not a.search or x.matches(a.search)]
        if a.json:
            print(json.dumps({"plugins": [x.to_json() for x in items], "warnings": warnings}, indent=2,
                             ensure_ascii=False))
            return 0
        width = max([len(x.id) for x in items] + [10])
        for x in items:
            mark = f"  [installed: {', '.join(x.installed)}]" if x.installed else ""
            desc = recent.shorten(x.description, 90) if x.description else ""
            print(f"{x.id:<{width}}  {desc}{mark}")
        if not items:
            print("no plugins found" + (f" matching {a.search!r}" if a.search else
                                        " (no marketplaces configured? claude plugin marketplace add …)"),
                  file=sys.stderr)
        for w in warnings:
            _err(f"warning: {w}")
        return 0
    claude_bin = a.claude_bin or shutil.which("claude") or "claude"
    try:
        marketplace.install_argv(claude_bin, a.plugin, a.scope)
    except ValueError as exc:
        _err(str(exc))
        return 2
    known = {x.id: x for x in marketplace.available(cwd, warnings)}
    if a.plugin not in known:
        _err(f"warning: {a.plugin} is not in the catalog of a configured marketplace (cm plugins available); "
             "asking claude anyway")
    print(f"running: {' '.join(marketplace.install_argv(claude_bin, a.plugin, a.scope))}  (in {tilde(cwd)})",
          file=sys.stderr)
    r = marketplace.install(claude_bin, a.plugin, a.scope, cwd)
    if r.output:
        print(r.output)
    if r.ok:
        print(f"installed {a.plugin} ({a.scope} scope); cm lists it under Plugins from the next start", file=sys.stderr)
        return 0
    _err(marketplace.advice(r))
    return 1


def build_skills_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cm skills", description=f"Skill packs in {tilde(skillpacks.packs_dir())}: skills a session loads "
                                      "only when you tick the pack (claude --plugin-dir).")
    sub = p.add_subparsers(dest="what", required=True)
    ls = sub.add_parser("list", help="list the skill packs")
    ls.add_argument("--json", action="store_true")
    add = sub.add_parser("add", help="create a pack from a folder of skills (copied, or symlinked with --link)")
    add.add_argument("name", help="the pack's name: lowercase letters, digits and -")
    add.add_argument("--from", dest="src", required=True, metavar="DIR",
                     help="a skill folder (with SKILL.md), or a folder of them (or one with a skills/ folder)")
    add.add_argument("--link", action="store_true", help="symlink the skill folders instead of copying them")
    add.add_argument("--description", default="", help="shown on the screen")
    add.add_argument("--requires", action="append", default=[], metavar="CMD[=INSTALL_HINT]",
                     help="a command the skills need, e.g. 'playwright-cli=npm i -g @playwright/cli' (repeatable)")
    rm = sub.add_parser("remove", help="delete a pack (asks first)")
    rm.add_argument("name")
    rm.add_argument("-y", "--yes", action="store_true", help="don't ask")
    return p


def _pack_json(pack: skillpacks.SkillPack) -> dict:
    return {"name": pack.name, "path": str(pack.path), "description": pack.description,
            "skills": [{"name": s.name, "description": s.description} for s in pack.skills],
            "requires": [{"command": r.command, "install_hint": r.install_hint, "found": r.found}
                         for r in pack.requires],
            "estimated_tokens": pack.estimated_tokens(), "error": pack.error, "notes": pack.notes}


def skills_main(argv: list[str]) -> int:
    a = build_skills_parser().parse_args(argv)
    if a.what == "list":
        warnings: list[str] = []
        packs = skillpacks.discover(warnings)
        if a.json:
            print(json.dumps({"dir": str(skillpacks.packs_dir()), "packs": [_pack_json(p) for p in packs],
                              "warnings": warnings}, indent=2, ensure_ascii=False))
            return 0
        if not packs:
            print(f"no skill packs in {tilde(skillpacks.packs_dir())} (cm skills add NAME --from DIR)",
                  file=sys.stderr)
        for pk in packs:
            state = f"  ! {pk.error}" if pk.error else "".join(f"  · {w}" for w in pk.warnings())
            desc = f"  {pk.description}" if pk.description else ""
            print(f"{pk.name}  ({len(pk.skills)} skill{'s' if len(pk.skills) != 1 else ''}, "
                  f"~{pk.estimated_tokens()} tok est.){desc}{state}")
            for sk in pk.skills:
                print(f"    {sk.name}" + (f": {recent.shorten(sk.description, 80)}" if sk.description else ""))
        for w in warnings:
            _err(f"warning: {w}")
        return 0
    if a.what == "add":
        try:
            reqs = [skillpacks.parse_requirement(r) for r in a.requires]
            pack = skillpacks.add(a.name, Path(a.src).expanduser(), link=a.link, description=a.description.strip(),
                                  requires=reqs)
        except (skillpacks.PackError, OSError) as exc:
            _err(str(exc))
            return 2
        how = "linked" if a.link else "copied"
        print(f"skill pack {pack.name}: {len(pack.skills)} skill(s) {how} into {tilde(pack.path)}")
        for w in pack.warnings():
            print(f"  · {w}")
        return 0
    if why := skillpacks.valid_name(a.name):
        _err(why)
        return 2
    path = skillpacks.packs_dir() / a.name
    if not (path.is_dir() or path.is_symlink()):
        _err(f"no skill pack named {a.name!r}")
        return 2
    if not a.yes:
        if not _has_tty():
            _err("not removing without confirmation: pass --yes")
            return 2
        try:
            answer = input(f"delete skill pack {a.name} ({tilde(path)})? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("kept")
            return 1
    try:
        skillpacks.remove(a.name)
    except (skillpacks.PackError, OSError) as exc:
        _err(str(exc))
        return 1
    print(f"removed skill pack {a.name}")
    return 0


SUBCOMMANDS = {"plugins": plugins_main, "skills": skills_main}


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    own, passthrough = _split_passthrough(raw)
    is_setup = own[:1] == ["setup"]
    sub = own[0] if own[:1] and own[0] in SUBCOMMANDS else None
    a = None if is_setup or sub else build_parser().parse_args(own)
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
    if sub:
        return SUBCOMMANDS[sub](raw[1:])
    assert a is not None
    claude_bin = a.claude_bin or shutil.which("claude") or "claude"
    cwd = Path(a.cwd).resolve() if a.cwd else Path.cwd()
    if a.awake is not None and not awake_mod.supported():
        _err("--awake is macOS only; ignored")  # a note, not an error: shared scripts keep working
        a.awake = None

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
    if a.agent is not None and a.agent.strip().lower() not in ("", "default", "none") and passthrough_agent(passthrough):
        _err("the agent is given twice (--agent and an --agent after --); keep only one")
        return 2
    if a.tmux and a.worktree is None:
        _err("--tmux needs --worktree (claude's --tmux only works with -w)")
        return 2
    if (a.worktree is not None or a.tmux) and passthrough_worktree(passthrough):
        _err("the worktree is given twice (--worktree/--tmux and a -w/--worktree/--tmux after --); keep only one")
        return 2
    if a.worktree not in (None, _WORKTREE_DEFAULT) and (bad := wt_mod.valid_name(a.worktree.strip())):
        _err(bad)
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
    session_on = False  # remember the last session too (remember_last_session)
    last: remember.Entry | None = None
    last_notes: list[str] = []
    if screen:
        cfg = cfg_mod.load_config()
        remember_on = cfg_mod.remember_last(cfg, last_notes)
        session_on = remember_on and cfg_mod.remember_last_session(cfg, last_notes)
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

    # The session the screen starts on. Only when nothing chose one (no --continue / --resume, a
    # profile's Continue counts) and the screen opens: headless launches keep New.
    by_name: recent.RecentSession | None = None
    last_found: recent.RecentSession | None = None
    if screen and sel.session == "new" and not passthrough_session(sel.extra_args):
        if a.name is not None:
            # --name NAME: resume this project's session of that name, if there is one
            by_name = recent.find_by_name(cwd, a.name)
            if by_name:
                sel.session, sel.resume_id = "resume", by_name.id
        elif last and last.session and session_on:
            last_found = recent.find(cwd, last.session.id)
            if last_found:
                sel.session, sel.resume_id = "resume", last_found.id
            else:
                notes.append(f"last session {last.session.label()!r} no longer exists; starting a new session")

    if screen:
        if not _has_tty():
            _err("no TTY for the interactive screen; pass selection flags (e.g. --mcp, --profile)")
            return 2
        from .tui import run_tui

        prefill = None
        if last:
            session_part = f" · last session: {last_found.name}" if last_found else ""
            prefill = f"Pre-filled from your last launch in {tilde(last.project)}{session_part} (Ctrl+N clears)"
        chosen = run_tui(cat, sel, claude_bin=claude_bin,
                         profile=a.profile or (None if last else profiles.get_default()),
                         notes=last_notes + notes + cat.warnings, auto_refresh=connectors_stale(cat),
                         prefill=prefill, last_session=last_found.id if last_found else None)
        if chosen is None:
            return 0
        sel = chosen
    else:
        for n in notes:
            _err(f"profile: {n}")

    try:
        wt_notes = finish_session(cat.cwd, sel, headless=not screen and not a.dry_run)
    except PlanError as exc:
        _err(str(exc))
        return 2

    try:
        plan = preview_plan(cat, sel, claude_bin)
    except PlanError as exc:
        _err(str(exc))
        return 2
    plan.warnings += wt_notes

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
        keep_awake = sel.awake != "off" and awake_mod.supported()
        power = awake_mod.power_source() if keep_awake else None  # read-only `pmset -g batt`
        if a.json:
            from .preview import env_line, session_line
            from .redact import redact_mcp_doc, redact_settings

            print(json.dumps({"argv": plan.argv, "env": env_line(plan), "mode": plan.mode,
                              "session": session_line(sel, titles),
                              "session_dir": str(plan.mcp_path.parent),
                              "mcp": redact_mcp_doc(plan.mcp_doc), "settings": redact_settings(plan.settings_doc),
                              "denied": plan.denied, "warnings": plan.warnings,
                              "awake": {"mode": sel.awake,
                                        "argv": awake_mod.caffeinate_argv(sel.awake, "<claude pid>"),
                                        "power": power} if keep_awake else None}, indent=2))
        else:
            print(plain(cat, sel, plan, costs_mod.load(), show_files=True, titles=titles, power=power))
        return 0

    if headless:
        for w in plan.warnings:
            _err(w)
    save_warnings: list[str] = []
    stored = Selection.from_dict({**sel.to_dict(), "extra_args": remembered_extra_args(sel.extra_args)})
    titles = {s.id: s.title for s in (by_name, last_found) if s and s.title}
    session = known_session(sel, titles) if session_on else None
    started: list[bool] = []

    def remember_launch() -> None:  # runs once claude has started; never while it couldn't
        started.append(True)
        if w := remember.save(cwd, stored, session):
            save_warnings.append(w)

    hook = {"on_started": remember_launch} if screen and remember_on else {}
    # Not resuming a known id: note the project's session files now, and after claude exits take
    # the one that appeared (cm passes no --session-id, which would break cmux restores).
    before = recent.snapshot(session_places(cat.cwd, sel)) if hook and session_on and session is None else None
    try:
        rc = launch(cat, sel, claude_bin=claude_bin, **hook)
        if started and before is not None and (found := session_from_diff(before, sel)):
            if w := remember.save(cwd, stored, found):
                save_warnings.append(w)
        return rc
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
