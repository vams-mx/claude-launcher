"""Turn a Selection into an exact `claude` argv + session files + env.

Two isolation modes, chosen automatically (all verified against Claude Code
2.1.281, see README "Control matrix"):

strict    No claude.ai connector selected (the common case).
          `--strict-mcp-config --mcp-config=<session mcp.json>` -> only the
          servers in that file load. Strict mode also drops claude.ai
          connectors, user/local/project-scope servers and plugin-bundled
          servers. `disableClaudeAiConnectors: true` is added so the connector
          list isn't even fetched.

denylist  At least one claude.ai connector selected (or a selected plugin
          bundles MCP servers). Strict mode cannot be used because it drops
          every connector, so cm omits --strict-mcp-config and blocks every
          other known server by name with `deniedMcpServers` in the session
          settings. This is exact for every server cm can discover; a server
          that appears after discovery (e.g. a brand-new connector before
          `cm --refresh`) could slip through, which the UI says.

In both modes a single `--settings <session settings.json>` carries
`enabledPlugins` (every known plugin explicitly true/false), the hooks of
selected servers' companion settings (e.g. AgentMemory), and the deny list.
Only one --settings flag is ever passed: Claude Code reads only one when
given several, and cmux's wrapper deep-merges exactly one into its own hooks.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import cmux as cmux_mod
from .catalog import Catalog

MODELS = ("default", "sonnet", "opus", "haiku")
PERMISSIONS = ("standard", "skip")
SESSIONS = ("new", "continue", "resume")


@dataclass
class Selection:
    local: list[str] = field(default_factory=list)
    connectors: list[str] = field(default_factory=list)  # full names, "claude.ai Slack"
    plugins: list[str] = field(default_factory=list)  # plugin ids enabled for the session
    cmux: bool = False
    model: str = "default"
    permissions: str = "standard"
    session: str = "new"
    resume_id: str | None = None
    extra_args: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Selection":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: copy.deepcopy(v) for k, v in d.items() if k in known})


@dataclass
class LaunchPlan:
    argv: list[str]
    mcp_doc: dict[str, Any]
    settings_doc: dict[str, Any]
    mcp_path: Path
    settings_path: Path
    env_overrides: dict[str, str]
    secret_env: set[str]
    mode: str
    selected_server_names: list[str]
    denied: list[str]
    warnings: list[str]
    env_unset: list[str] = field(default_factory=list)  # removed from the inherited environment


class PlanError(ValueError):
    pass


def _merge(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    """Deep merge: dicts recurse, lists concatenate (hooks per event), scalars override."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        elif isinstance(v, list) and isinstance(dst.get(k), list):
            dst[k] = dst[k] + [x for x in v if x not in dst[k]]
        else:
            dst[k] = copy.deepcopy(v)
    return dst


def _load_companion(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        raise PlanError(f"{path} is not a JSON object")
    return doc


def build_plan(
    cat: Catalog,
    sel: Selection,
    *,
    claude_bin: str,
    session_dir: Path,
    env: dict[str, str] | None = None,
    owner_pid: int | None = None,
) -> LaunchPlan:
    env = dict(os.environ) if env is None else env
    owner_pid = os.getpid() if owner_pid is None else owner_pid
    warnings: list[str] = []

    if sel.model not in MODELS:
        raise PlanError(f"unknown model {sel.model!r} (choose from {', '.join(MODELS)})")
    if sel.permissions not in PERMISSIONS:
        raise PlanError(f"unknown permissions {sel.permissions!r}")
    if sel.session not in SESSIONS:
        raise PlanError(f"unknown session mode {sel.session!r}")

    # --- MCP servers --------------------------------------------------------
    servers: dict[str, Any] = {}
    settings: dict[str, Any] = {}
    seen_settings: set[Path] = set()
    for name in sel.local:
        srv = cat.local_by_name(name)
        if srv is None:
            raise PlanError(f"unknown local MCP server {name!r} (see `cm --list`)")
        servers[name] = copy.deepcopy(srv.config)
        if srv.settings_path and srv.settings_path not in seen_settings:
            seen_settings.add(srv.settings_path)
            try:
                companion = _load_companion(srv.settings_path)
            except (OSError, ValueError) as exc:
                raise PlanError(f"cannot read companion settings {srv.settings_path}: {exc}") from exc
            _merge(settings, companion)

    env_overrides = {"CMUX_COMPUTER_USE_MCP_DISABLED": "1"}  # wrapper must never add its own copy
    secret_env: set[str] = set()
    env_unset: list[str] = []
    if sel.cmux:
        # The cmux-cua entry's owner PID is "${CMUX_CLAUDE_PID:-<cm pid>}". cmux's wrapper exports a
        # fresh CMUX_CLAUDE_PID (Claude's own PID); an inherited one (e.g. from an outer Claude
        # session) must not leak through when claude is started some other way.
        env_unset.append("CMUX_CLAUDE_PID")
        if not cat.cmux.available:
            raise PlanError(f"cmux computer-use unavailable: {cat.cmux.reason}")
        servers[cmux_mod.SERVER_NAME] = cmux_mod.server_config(cat.cmux, env, owner_pid)
        token, src = cmux_mod.resolve_token(env)
        if token:
            # cmux-cua 0.7.1's proxy reads only this variable (not the token file)
            env_overrides["CMUX_CUA_SOCKET_AUTH_TOKEN"] = token
            secret_env.add("CMUX_CUA_SOCKET_AUTH_TOKEN")
        if src == "env":
            warnings.append("cmux: token taken from the inherited CMUX_CUA_SOCKET_AUTH_TOKEN because "
                            f"CMUX_CUA_AUTH_TOKEN_FILE ({env.get('CMUX_CUA_AUTH_TOKEN_FILE') or 'unset'}) "
                            "can't be read; it may be stale")
        if cat.cmux.reason:
            warnings.append(f"cmux: {cat.cmux.reason}")

    # --- connectors -> mode -------------------------------------------------
    known_conn = {c.name for c in cat.connectors}
    for c in sel.connectors:
        if c not in known_conn:
            raise PlanError(f"unknown claude.ai connector {c!r} (run `cm --refresh`)")
    plugin_mcp = [s for pid in sel.plugins for s in ((cat.plugin(pid).mcp_servers if cat.plugin(pid) else []))]
    mode = "denylist" if (sel.connectors or plugin_mcp) else "strict"

    # --- plugins ------------------------------------------------------------
    known_plugins = {p.id for p in cat.plugins}
    for pid in sel.plugins:
        if pid not in known_plugins:
            raise PlanError(f"unknown plugin {pid!r}")
    settings["enabledPlugins"] = {p.id: (p.id in sel.plugins) for p in cat.plugins}

    selected_names = list(servers) + list(sel.connectors) + plugin_mcp
    denied: list[str] = []
    if mode == "strict":
        settings["disableClaudeAiConnectors"] = True
    else:
        denied = sorted(cat.all_server_names() - set(selected_names))
        existing = settings.get("deniedMcpServers") or []
        settings["deniedMcpServers"] = existing + [{"serverName": n} for n in denied]
        if not sel.connectors:
            settings["disableClaudeAiConnectors"] = True
        unverified = [c for c in cat.connectors if not c.verified and c.name in sel.connectors]
        if unverified:
            warnings.append("some selected connectors were not seen by the last `claude mcp list`; run `cm --refresh`")
        warnings.append("deny-list mode (deniedMcpServers): run `cm --refresh` after adding connectors in claude.ai")

    mcp_doc = {"mcpServers": servers}
    mcp_path = session_dir / "mcp.json"
    settings_path = session_dir / "settings.json"

    # --- argv -----------------------------------------------------------------
    argv = [claude_bin]
    if mode == "strict":
        argv.append("--strict-mcp-config")
    if mode == "strict" or servers:
        # single-token form: --mcp-config is variadic and would swallow a prompt
        argv.append(f"--mcp-config={mcp_path}")
    argv += ["--settings", str(settings_path)]
    if sel.model != "default":
        argv += ["--model", sel.model]
    if sel.permissions == "skip":
        argv.append("--dangerously-skip-permissions")
    if sel.session == "continue":
        argv.append("--continue")
    argv += list(sel.extra_args)
    if sel.session == "resume":
        # --resume takes an optional value; keep it last so it never eats a forwarded arg
        argv += ["--resume", sel.resume_id] if sel.resume_id else ["--resume"]

    return LaunchPlan(
        argv=argv, mcp_doc=mcp_doc, settings_doc=settings, mcp_path=mcp_path, settings_path=settings_path,
        env_overrides=env_overrides, secret_env=secret_env, mode=mode,
        selected_server_names=selected_names, denied=denied, warnings=warnings, env_unset=env_unset,
    )
