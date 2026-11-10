"""Read-only discovery of everything cm can switch on or off.

Sources (never written to):
  * `sources` and `mcp_folders` from ~/.claude-launcher/config.json (default
    folder ~/.claude-launcher/mcps): any *.json with a top-level "mcpServers" object. A companion settings file
    with hooks is picked up as `<stem>.settings.json` or, for
    `<base>-mcp.json`, `<base>-settings.json`; it is loaded only when one of
    that file's servers is selected.
  * ~/.claude.json: user scope (`mcpServers`) and local scope
    (`projects[<path>].mcpServers`, every project; current one first).
  * <project>/.mcp.json (project scope).
  * managed-mcp.json (names only, for the deny list).
  * claude.ai connectors: cm's cache of `claude mcp list`, plus the
    `claudeAiMcpEverConnected` names in ~/.claude.json.
  * plugins: `enabledPlugins` in user/project/local settings, installed per
    ~/.claude/plugins/installed_plugins.json.
  * agents (for --agent): ~/.claude/agents/*.md, <project>/.claude/agents/*.md
    (the cwd's too), and <installPath>/agents/*.md of every installed plugin
    (named "<plugin>:<agent>"); the `agent` setting of user/project/local
    settings is the default. Frontmatter `name` / `description`.
  * skill packs: ~/.claude-launcher/skills/* (skillpacks.py).
  * cmux computer-use: see cmux.py.

Server definitions are copied verbatim; nothing is invented.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import cmux as cmux_mod
from . import config as cfg_mod
from . import frontmatter
from . import skillpacks
from .paths import claude_dir, claude_json, config_dir, expand, managed_dir, tilde

CONNECTOR_PREFIX = "claude.ai "


@dataclass
class LocalServer:
    name: str
    config: dict[str, Any]
    source: str
    settings_path: Path | None = None
    description: str = ""
    also_in: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"mcp:{self.name}"

    @property
    def transport(self) -> str:
        t = self.config.get("type")
        if t:
            return str(t)
        return "stdio" if "command" in self.config else "http"

    def summary(self) -> str:
        from .redact import redact_args, redact_url

        if self.transport == "stdio":
            cmd = [str(self.config.get("command", "?"))] + [str(a) for a in self.config.get("args", [])]
            return "stdio · " + " ".join(redact_args(cmd))
        return f"{self.transport} · {redact_url(str(self.config.get('url', '?')))}"


@dataclass
class Connector:
    name: str  # full name, e.g. "claude.ai Slack"
    status: str = ""
    verified: bool = True  # False = only known from claudeAiMcpEverConnected

    @property
    def short(self) -> str:
        return self.name[len(CONNECTOR_PREFIX):] if self.name.startswith(CONNECTOR_PREFIX) else self.name

    @property
    def key(self) -> str:
        return f"connector:{self.short}"

    @property
    def tool_prefix(self) -> str:
        return "mcp__" + re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_-]", "_", self.name)).strip("_") + "__"


@dataclass
class Plugin:
    id: str  # name@marketplace
    enabled_in: list[str] = field(default_factory=list)  # settings scopes with true
    install_path: Path | None = None
    mcp_servers: list[str] = field(default_factory=list)  # plugin:<name>:<server>
    installed_here: bool = True  # installed for the user, or for this project (project/local scope)
    dependencies: list[str] = field(default_factory=list)  # plugin.json `dependencies`, as name@marketplace

    @property
    def name(self) -> str:
        return self.id.split("@", 1)[0]

    @property
    def key(self) -> str:
        return f"plugin:{self.id}"


@dataclass
class Agent:
    name: str  # what --agent takes: the agent's name, "<plugin>:<name>" for a plugin's agent
    description: str = ""
    source: str = ""  # "user" | "project" | "plugin <id>"
    path: Path | None = None
    plugin_id: str | None = None  # the plugin it comes with (it must be enabled for the session)
    installed_here: bool = True  # (plugin agents) the plugin is installed for this project

    @property
    def key(self) -> str:
        return f"agent:{self.name}"


@dataclass
class Catalog:
    cwd: Path
    local: list[LocalServer] = field(default_factory=list)
    connectors: list[Connector] = field(default_factory=list)
    plugins: list[Plugin] = field(default_factory=list)
    cmux: cmux_mod.CmuxStatus = field(default_factory=lambda: cmux_mod.CmuxStatus(False, "not probed"))
    other_server_names: set[str] = field(default_factory=set)  # e.g. managed, never selectable
    mcp_folders: list[Path] = field(default_factory=list)  # folders scanned, in order
    source_files: list[Path] = field(default_factory=list)  # explicit `sources` files
    connectors_refreshed_at: str | None = None
    warnings: list[str] = field(default_factory=list)
    agents: list[Agent] = field(default_factory=list)
    settings_agent: str | None = None  # the `agent` setting (user/project/local), the Default agent
    skill_packs: list[skillpacks.SkillPack] = field(default_factory=list)

    # -- lookups -------------------------------------------------------------
    def local_by_name(self, name: str) -> LocalServer | None:
        return next((s for s in self.local if s.name == name), None)

    def resolve_local(self, names: list[str]) -> tuple[list[str], list[str]]:
        found, missing = [], []
        for n in names:
            hit = self.local_by_name(n) or next((s for s in self.local if s.name.lower() == n.lower()), None)
            (found.append(hit.name) if hit else missing.append(n))
        return found, missing

    def resolve_connectors(self, names: list[str]) -> tuple[list[str], list[str]]:
        """Accept 'Slack', 'claude.ai Slack', 'slack', or a unique substring ('ACME')."""
        found, missing = [], []
        for raw in names:
            n = raw.strip()
            low = n.lower()
            if low.startswith(CONNECTOR_PREFIX.lower()):
                low = low[len(CONNECTOR_PREFIX):]
            exact = [c for c in self.connectors if c.short.lower() == low]
            subs = [c for c in self.connectors if low and low in c.short.lower()]
            hit = exact or (subs if len(subs) == 1 else [])
            if hit:
                if hit[0].name not in found:
                    found.append(hit[0].name)
            else:
                missing.append(raw)
        return found, missing

    def resolve_plugins(self, names: list[str]) -> tuple[list[str], list[str]]:
        found, missing = [], []
        for n in names:
            hit = next((p for p in self.plugins if n in (p.id, p.name)), None)
            (found.append(hit.id) if hit else missing.append(n))
        return found, missing

    def plugin(self, pid: str) -> Plugin | None:
        return next((p for p in self.plugins if p.id == pid), None)

    def _installed(self, pid: str) -> tuple[Path | None, bool]:
        """(install path, installed here) of a plugin that may not be enabled in any settings file."""
        entries = installed_plugins().get(pid) or []
        here = entry_here(entries, self.cwd)
        e = here or (entries[0] if entries else None)
        path = Path(e["installPath"]) if e and isinstance(e.get("installPath"), str) else None
        return path, here is not None

    def with_dependencies(self, pids: list[str]) -> tuple[list[str], list[tuple[str, str]], list[str]]:
        """`pids` plus every plugin they depend on, transitively.

        Returns (all plugin ids, [(plugin, dependency it brought)], problems): a dependency that
        isn't installed here can't be loaded, and then neither can the plugin that needs it."""
        out = list(dict.fromkeys(pids))
        added: list[tuple[str, str]] = []
        problems: list[str] = []
        i = 0
        while i < len(out) and i < 200:  # a cycle or a huge graph can't loop forever
            pid = out[i]
            i += 1
            p = self.plugin(pid)
            deps = p.dependencies if p is not None else plugin_dependencies(pid, self._installed(pid)[0])
            for dep in deps:
                if dep in out:
                    continue
                out.append(dep)
                added.append((pid, dep))
                known = self.plugin(dep)
                if not (known.installed_here if known is not None else self._installed(dep)[1]):
                    problems.append(f"plugin {pid} needs plugin {dep}, which isn't installed for this project: "
                                    f"claude won't load {pid} (claude plugin install {dep})")
        return out, added, problems

    def plugin_mcp(self, pid: str) -> list[str]:
        p = self.plugin(pid)
        return p.mcp_servers if p is not None else _plugin_mcp_servers(pid, self._installed(pid)[0])

    def agent(self, name: str | None) -> Agent | None:
        return next((a for a in self.agents if a.name == name), None) if name else None

    def agent_block(self, agent: Agent) -> str | None:
        """Why a plugin's agent can't be used here, or None."""
        if not agent.plugin_id:
            return None
        if not agent.installed_here:
            return (f"agent {agent.name}: its plugin {agent.plugin_id} is installed for another project only; "
                    "install it for this one (Ctrl+O, or cm plugins install) or pick another agent")
        if self.plugin(agent.plugin_id) is None:
            return (f"agent {agent.name}: its plugin {agent.plugin_id} is installed but not enabled in your "
                    f"settings, so cm can't load it (claude plugin enable {agent.plugin_id})")
        return None

    def skill_pack(self, name: str) -> skillpacks.SkillPack | None:
        return next((p for p in self.skill_packs if p.name == name), None)

    def resolve_skill_packs(self, names: list[str]) -> tuple[list[str], list[str]]:
        found, missing = [], []
        for n in names:
            hit = self.skill_pack(n) or self.skill_pack(n.lower())
            if hit is None:
                missing.append(n)
            elif hit.name not in found:
                found.append(hit.name)
        return found, missing

    def all_server_names(self) -> set[str]:
        names = {s.name for s in self.local} | {c.name for c in self.connectors}
        for p in self.plugins:
            names.update(p.mcp_servers)
        names.add(cmux_mod.SERVER_NAME)
        return names | self.other_server_names


# ---------------------------------------------------------------------------
# helpers


def _read_json(path: Path, warnings: list[str] | None = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        if warnings is not None:
            warnings.append(f"could not read {path}: {exc.__class__.__name__}")
        return None


def _companion_settings(f: Path) -> Path | None:
    cands = [f.with_name(f.stem + ".settings.json")]
    if f.stem.endswith("-mcp"):
        cands.append(f.with_name(f.stem[: -len("-mcp")] + "-settings.json"))
    return next((c for c in cands if c.is_file()), None)


def _project_root(cwd: Path) -> Path:
    cur = cwd
    for p in [cur, *cur.parents]:
        if (p / ".git").exists():
            return p
    return cwd


# ---------------------------------------------------------------------------
# discovery


def discover_local(cwd: Path, warnings: list[str], scanned: dict[str, list[Path]] | None = None
                   ) -> tuple[list[LocalServer], set[str]]:
    cfg = cfg_mod.load_config(warnings)
    scanned = {} if scanned is None else scanned
    servers: dict[str, LocalServer] = {}
    other: set[str] = set()

    def add(name: str, conf: Any, source: str, settings: Path | None = None, desc: str = "") -> None:
        if not isinstance(conf, dict):
            return
        if name in servers:
            if servers[name].config != conf:
                servers[name].also_in.append(source)
            return
        servers[name] = LocalServer(name, conf, source, settings, desc)

    # 1. explicit sources from ~/.claude-launcher/config.json
    scanned["sources"] = []
    for src in cfg_mod.sources(cfg):
        f = expand(src["mcp_config"])
        scanned["sources"].append(f)
        if not f.is_file():
            warnings.append(f"source {tilde(f)} not found")
            continue
        doc = _read_json(f, warnings) or {}
        st = expand(src["settings"]) if src.get("settings") else _companion_settings(f)
        for n, c in (doc.get("mcpServers") or {}).items():
            add(n, c, f"file:{tilde(f)}", st, src.get("description", ""))

    # 2. user folders
    folders = cfg_mod.mcp_folders(cfg)
    scanned["folders"] = folders
    for folder in folders:
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.json")):
            if f.name.endswith(".settings.json") or f.name.endswith("-settings.json"):
                continue
            doc = _read_json(f, warnings)
            if not isinstance(doc, dict) or not isinstance(doc.get("mcpServers"), dict):
                continue
            meta = doc.get("x-cm") or {}
            st = expand(meta["settings"]) if meta.get("settings") else _companion_settings(f)
            for n, c in doc["mcpServers"].items():
                add(n, c, f"file:{tilde(f)}", st, meta.get("description", ""))

    # 3. ~/.claude.json local scope (current project first), project .mcp.json, user scope
    cj = _read_json(claude_json(), warnings) or {}
    projects = cj.get("projects") or {}
    cwd_s = str(cwd)
    if isinstance(projects.get(cwd_s), dict):
        for n, c in (projects[cwd_s].get("mcpServers") or {}).items():
            add(n, c, "local (this project)")
    root = _project_root(cwd)
    for d in dict.fromkeys([cwd, root]):
        doc = _read_json(d / ".mcp.json", warnings) or {}
        for n, c in (doc.get("mcpServers") or {}).items():
            add(n, c, f"project:{tilde(d / '.mcp.json')}")
    for n, c in (cj.get("mcpServers") or {}).items():
        add(n, c, "user (~/.claude.json)")
    for p, pv in projects.items():
        if p == cwd_s or not isinstance(pv, dict):
            continue
        for n, c in (pv.get("mcpServers") or {}).items():
            add(n, c, f"local:{tilde(Path(p))}")

    # 4. managed servers: names only (deny list), never selectable
    md = managed_dir()
    for f in [md / "managed-mcp.json"]:
        doc = _read_json(f, warnings) or {}
        other.update((doc.get("mcpServers") or {}).keys())
    return list(servers.values()), other


def connectors_cache_path() -> Path:
    return config_dir() / "connectors.json"


def parse_mcp_list(text: str) -> list[tuple[str, str]]:
    """Parse `claude mcp list` output -> [(full connector name, status)]."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(CONNECTOR_PREFIX):
            continue
        m = re.match(r"^(claude\.ai .+?):\s+(?:\S+)(?:\s+-\s+(.*))?$", line)
        if not m:
            continue
        status = (m.group(2) or "").strip()
        status = re.sub(r"^[^\w]+", "", status)  # drop the check-mark glyph
        out.append((m.group(1), status))
    return out


def refresh_connectors(
    claude_bin: str = "claude",
    run: Callable[..., subprocess.CompletedProcess] | None = None,
    timeout: float = 45,
) -> list[tuple[str, str]]:
    """Run `claude mcp list` from an empty temp dir (so no project/local servers
    are health-checked) and cache the claude.ai connector names."""
    run = run or subprocess.run
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    with tempfile.TemporaryDirectory(prefix="cm-mcp-list-") as tmp:
        res = run(
            [claude_bin, "mcp", "list"],
            cwd=tmp, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    found = parse_mcp_list(res.stdout or "")
    if res.returncode != 0 and not found:
        raise RuntimeError(f"`claude mcp list` exited {res.returncode}")
    path = connectors_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps({
        "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "connectors": [{"name": n, "status": s} for n, s in found],
    }, indent=2))
    tmp_path.replace(path)
    return found


def discover_connectors(warnings: list[str]) -> tuple[list[Connector], str | None]:
    cache = _read_json(connectors_cache_path(), warnings) or {}
    conns: dict[str, Connector] = {}
    for c in cache.get("connectors", []):
        if isinstance(c, dict) and str(c.get("name", "")).startswith(CONNECTOR_PREFIX):
            conns[c["name"]] = Connector(c["name"], c.get("status", ""), True)
    ever = (_read_json(claude_json(), None) or {}).get("claudeAiMcpEverConnected") or []
    have_cache = bool(cache.get("connectors"))
    for n in ever:
        if isinstance(n, str) and n.startswith(CONNECTOR_PREFIX) and n not in conns:
            # Without a fresh cache these are our best guess; with one, they're
            # stale names kept only for the deny list.
            conns[n] = Connector(n, "not in last `claude mcp list`" if have_cache else "unverified", False)
    ordered = sorted(conns.values(), key=lambda c: (not c.verified, c.short.lower()))
    return ordered, cache.get("refreshed_at")


def _settings_files(cwd: Path) -> list[tuple[str, Path]]:
    """user, project and local settings, in increasing precedence (the cwd's after the root's)."""
    root = _project_root(cwd)
    sources = [
        ("user", claude_dir() / "settings.json"),
        ("project", root / ".claude" / "settings.json"),
        ("local", root / ".claude" / "settings.local.json"),
    ]
    if root != cwd:
        sources += [("project", cwd / ".claude" / "settings.json"), ("local", cwd / ".claude" / "settings.local.json")]
    return sources


def installed_plugins(warnings: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """~/.claude/plugins/installed_plugins.json: {plugin id: [install entries]} (odd entries dropped)."""
    doc = _read_json(claude_dir() / "plugins" / "installed_plugins.json", warnings)
    raw = doc.get("plugins") if isinstance(doc, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {pid: [e for e in v if isinstance(e, dict)] for pid, v in raw.items()
            if isinstance(pid, str) and isinstance(v, list)}


def entry_here(entries: list[dict[str, Any]], cwd: Path) -> dict[str, Any] | None:
    """The install entry that applies to `cwd`: user scope, or project/local scope of this project."""
    root = _project_root(cwd)
    for e in entries:
        if e.get("scope") == "user" or e.get("projectPath") in (str(cwd), str(root)):
            return e
    return None


def discover_plugins(cwd: Path, warnings: list[str]) -> list[Plugin]:
    state: dict[str, list[str]] = {}
    final: dict[str, bool] = {}
    for scope, f in _settings_files(cwd):
        doc = _read_json(f, warnings) or {}
        ep = doc.get("enabledPlugins") if isinstance(doc, dict) else None
        if not isinstance(ep, dict):
            continue
        for pid, on in ep.items():
            final[pid] = bool(on)
            if on:
                state.setdefault(pid, []).append(scope)
    installed = installed_plugins(warnings)
    plugins: list[Plugin] = []
    for pid, on in final.items():
        if not on:
            continue
        entries = installed.get(pid) or []
        here = entry_here(entries, cwd)
        path = None
        if here is not None:
            path = Path(here["installPath"]) if isinstance(here.get("installPath"), str) else None
        if path is None and entries and isinstance(entries[0].get("installPath"), str):
            path = Path(entries[0]["installPath"])
        plugins.append(Plugin(pid, state.get(pid, []), path, _plugin_mcp_servers(pid, path),
                              installed_here=here is not None or not entries,
                              dependencies=plugin_dependencies(pid, path)))
    return sorted(plugins, key=lambda p: p.id)


MAX_AGENTS = 200  # agent files read per folder


def _agent_files(d: Path) -> list[Path]:
    try:
        entries = sorted(os.scandir(d), key=lambda e: e.name)
    except OSError:
        return []
    out = []
    for e in entries:
        if not e.name.endswith(".md") or e.name.startswith("."):
            continue
        try:
            if Path(e.path).is_file():
                out.append(Path(e.path))
        except OSError:
            continue
        if len(out) >= MAX_AGENTS:
            break
    return out


def _agent(path: Path, source: str, prefix: str = "", plugin_id: str | None = None,
           installed_here: bool = True) -> Agent | None:
    fm = frontmatter.read(path)
    name = (fm.get("name") or "").strip() or path.stem
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", name):
        return None  # not something --agent could name
    desc = " ".join((fm.get("description") or "").split())
    return Agent(prefix + name, desc, source, path, plugin_id, installed_here)


def discover_agents(cwd: Path, warnings: list[str] | None = None) -> tuple[list[Agent], str | None]:
    """Agents --agent can name, highest precedence first (a project's agent shadows a user one of
    the same name; plugin agents are namespaced), and the `agent` setting."""
    root = _project_root(cwd)
    agents: dict[str, Agent] = {}
    folders = [(d / ".claude" / "agents", "project") for d in dict.fromkeys([cwd, root])]
    folders.append((claude_dir() / "agents", "user"))
    for folder, source in folders:
        for f in _agent_files(folder):
            if (a := _agent(f, source)) and a.name not in agents:
                agents[a.name] = a
    for pid, entries in sorted(installed_plugins(warnings).items()):
        here = entry_here(entries, cwd)
        e = here or (entries[0] if entries else None)
        if not e or not isinstance(e.get("installPath"), str):
            continue
        plugin_name = pid.split("@", 1)[0]
        for f in _agent_files(Path(e["installPath"]) / "agents"):
            a = _agent(f, f"plugin {pid}", f"{plugin_name}:", pid, here is not None)
            if a and a.name not in agents:
                agents[a.name] = a
    setting: str | None = None
    for _scope, f in _settings_files(cwd):
        doc = _read_json(f) or {}
        v = doc.get("agent") if isinstance(doc, dict) else None
        if isinstance(v, str) and v.strip():
            setting = v.strip()
    return list(agents.values()), setting


def plugin_dependencies(pid: str, path: Path | None) -> list[str]:
    """The plugins `pid` declares in its plugin.json `dependencies`, as name@marketplace.

    Claude Code accepts "name" (the plugin's own marketplace), "name@marketplace" and
    {"name": ..., "marketplace": ...}. It doesn't load a plugin whose dependency is disabled
    (its agents are then "not found"), so cm keeps the dependencies of what it enables on."""
    if not path:
        return []
    doc = _read_json(path / ".claude-plugin" / "plugin.json")
    deps = doc.get("dependencies") if isinstance(doc, dict) else None
    if not isinstance(deps, list):
        return []
    own = pid.split("@", 1)[1] if "@" in pid else ""
    out: list[str] = []
    for d in deps:
        name, market = (d.get("name"), d.get("marketplace")) if isinstance(d, dict) else (d, None)
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if "@" not in name:
            market = market.strip() if isinstance(market, str) and market.strip() else own
            if not market:
                continue
            name = f"{name}@{market}"
        if name != pid and name not in out:
            out.append(name)
    return out


def _plugin_mcp_servers(pid: str, path: Path | None) -> list[str]:
    if not path or not path.is_dir():
        return []
    name = pid.split("@", 1)[0]
    names: list[str] = []
    for doc in (_read_json(path / ".mcp.json"), _read_json(path / ".claude-plugin" / "plugin.json")):
        if isinstance(doc, dict) and isinstance(doc.get("mcpServers"), dict):
            names += [f"plugin:{name}:{s}" for s in doc["mcpServers"]]
    return sorted(set(names))


def _sideload_disabled() -> bool:
    md = managed_dir()  # /Library/Application Support/ClaudeCode (macOS) or /etc/claude-code (Linux)
    dropins = sorted((md / "managed-settings.d").glob("*.json")) if (md / "managed-settings.d").is_dir() else []
    for f in [md / "managed-settings.json", *dropins]:
        doc = _read_json(f) or {}
        if doc.get("disableSideloadFlags") is True:
            return True
    return False


def discover(cwd: Path | None = None, env: dict[str, str] | None = None) -> Catalog:
    cwd = (cwd or Path.cwd()).resolve()
    env = dict(os.environ) if env is None else env
    cat = Catalog(cwd=cwd)
    scanned: dict[str, list[Path]] = {}
    cat.local, cat.other_server_names = discover_local(cwd, cat.warnings, scanned)
    cat.mcp_folders, cat.source_files = scanned.get("folders", []), scanned.get("sources", [])
    cat.connectors, cat.connectors_refreshed_at = discover_connectors(cat.warnings)
    cat.plugins = discover_plugins(cwd, cat.warnings)
    cat.agents, cat.settings_agent = discover_agents(cwd)
    cat.skill_packs = skillpacks.discover(cat.warnings)
    cat.cmux = cmux_mod.probe(env)
    if _sideload_disabled():
        cat.warnings.append("managed policy sets disableSideloadFlags: Claude will refuse --mcp-config launches")
    return cat


def connectors_stale(cat: Catalog, max_age_h: float = 24) -> bool:
    if not cat.connectors_refreshed_at:
        return True
    try:
        ts = datetime.fromisoformat(cat.connectors_refreshed_at).timestamp()
    except ValueError:
        return True
    return (time.time() - ts) > max_age_h * 3600
