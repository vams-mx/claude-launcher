"""`cm setup agentmemory`: the one MCP server with special installation logic.

AgentMemory (https://github.com/rohitg00/agentmemory) is three things:
  * the npm package `@agentmemory/agentmemory`: the backend server (`agentmemory`,
    REST on :3111) plus the Claude Code hook scripts in `plugin/scripts/*.mjs`,
    described by the package's own `plugin/hooks/hooks.json`;
  * the MCP shim `@agentmemory/mcp` (`npx -y @agentmemory/mcp`), which proxies to
    the backend at $AGENTMEMORY_URL;
  * a Claude Code plugin (`/plugin marketplace add rohitg00/agentmemory`). cm does
    NOT use the plugin: a plugin's hooks run in every session, while cm loads the
    hooks only in sessions where `agentmemory` is selected.

What this does, every impactful step confirmed first:
  1. gate on INSTALL_AGENT_MEMORY (env var, else config.json): unset -> ask once
     (a "no" is stored as "0"), "0" -> skip, "1" -> run;
  2. check Node >= 20 and npm; offer `npm install -g @agentmemory/agentmemory`;
  3. optionally register the backend as a user service (launchd / systemd --user),
     asked separately; an already running or registered backend is left alone;
  4. write ~/.claude-launcher/mcps/agentmemory-mcp.json and agentmemory-settings.json.
     Hook paths are resolved from the installed package's hooks.json at setup time;
     the secret is a ${AGENTMEMORY_SECRET:-} reference, never a value.

It never starts, stops or resets an existing backend, never touches AgentMemory's
data (~/.agentmemory), and never overwrites an existing file without asking.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config as cfg_mod
from .health import DEFAULT_AGENTMEMORY_URL
from .paths import config_dir, default_mcp_dir, home, tilde, xdg_config_home

PACKAGE = "@agentmemory/agentmemory"
MCP_PACKAGE = "@agentmemory/mcp"
MIN_NODE = 20
GATE = "INSTALL_AGENT_MEMORY"
SERVER = "agentmemory"
MCP_FILE = "agentmemory-mcp.json"
SETTINGS_FILE = "agentmemory-settings.json"
SERVICE_LABEL = "claude-launcher.agentmemory"  # launchd label (ours, not upstream's)
SYSTEMD_UNIT = "agentmemory.service"
PLUGIN_ROOT_REFS = ("${CLAUDE_PLUGIN_ROOT}", "$CLAUDE_PLUGIN_ROOT")

# Same server entry as upstream's `agentmemory connect claude-code` / plugin .mcp.json,
# with ${VAR} references that Claude Code expands at launch (verified on 2.1.281).
MCP_ENTRY: dict[str, Any] = {
    "command": "npx",
    "args": ["-y", MCP_PACKAGE],
    "env": {
        "AGENTMEMORY_URL": "${AGENTMEMORY_URL:-" + DEFAULT_AGENTMEMORY_URL + "}",
        "AGENTMEMORY_SECRET": "${AGENTMEMORY_SECRET:-}",
        "AGENTMEMORY_TOOLS": "${AGENTMEMORY_TOOLS:-all}",
    },
}

ABOUT = """\
AgentMemory gives Claude Code persistent memory across sessions: a local backend
(npm package @agentmemory/agentmemory, http://localhost:3111), an MCP server and
12 hooks that record what happens in a session. With cm, the hooks run only in
sessions where you select `agentmemory`. Details: https://github.com/rohitg00/agentmemory"""


class SetupError(RuntimeError):
    pass


def _default_ask(question: str, default: bool) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        try:
            ans = input(question + suffix).strip().lower()
        except EOFError:
            return default
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def _default_livez(url: str) -> str:
    from .health import livez

    return livez(url, timeout=1.5)


@dataclass
class Ctx:
    """Everything with side effects, injectable for tests."""
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    which: Callable[[str], str | None] = shutil.which
    ask: Callable[[str, bool], bool] = _default_ask
    say: Callable[[str], None] = print
    interactive: bool = field(default_factory=lambda: sys.stdin.isatty())
    platform: str = sys.platform
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    livez: Callable[[str], str] = _default_livez
    uid: int = field(default_factory=os.getuid)

    def confirm(self, question: str, default: bool) -> bool:
        """Impactful steps are confirmed; without a terminal they are skipped."""
        if not self.interactive:
            self.say(f"  - {question} -> skipped (no terminal to confirm; re-run `cm setup agentmemory`)")
            return False
        return self.ask(question, default)

    def capture(self, argv: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
        return self.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# gate


def gate_value(env: dict[str, str] | None = None) -> str | None:
    """'0' | '1' | None (unset). The environment variable wins over config.json."""
    env = os.environ if env is None else env
    raw = env.get(GATE)
    if raw is None or raw.strip() == "":
        raw = cfg_mod.load_config().get(GATE)
    if raw is None or str(raw).strip() == "":
        return None
    v = str(raw).strip().lower()
    if v in ("1", "true", "yes"):
        return "1"
    if v in ("0", "false", "no"):
        return "0"
    raise SetupError(f"{GATE} must be 0 or 1 (got {raw!r})")


# ---------------------------------------------------------------------------
# install detection


@dataclass
class Install:
    package_dir: Path
    version: str
    plugin_dir: Path
    hooks_json: Path
    cli: Path | None  # the backend's JS entry point (package.json bin.agentmemory)


def node_major(ctx: Ctx) -> int | None:
    if not ctx.which("node"):
        return None
    try:
        p = ctx.capture(["node", "--version"], timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"v?(\d+)\.", (p.stdout or "").strip())
    return int(m.group(1)) if p.returncode == 0 and m else None


def npm_root(ctx: Ctx) -> Path | None:
    try:
        p = ctx.capture(["npm", "root", "-g"], timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (p.stdout or "").strip().splitlines()
    return Path(out[-1]) if p.returncode == 0 and out else None


def find_install(ctx: Ctx) -> Install | None:
    root = npm_root(ctx)
    if root is None:
        return None
    pkg = root / "@agentmemory" / "agentmemory"
    try:
        meta = json.loads((pkg / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    bin_field = meta.get("bin")
    rel = bin_field.get("agentmemory") if isinstance(bin_field, dict) else bin_field
    cli = (pkg / rel) if isinstance(rel, str) and (pkg / rel).is_file() else None
    plugin = pkg / "plugin"
    return Install(pkg, str(meta.get("version", "?")), plugin, plugin / "hooks" / "hooks.json", cli)


# ---------------------------------------------------------------------------
# generated files


def _resolve_root(cmd: str, root: str) -> str:
    """Replace ${CLAUDE_PLUGIN_ROOT} with `root`, shell-quoted: inside "..." or '...'
    the quote is closed around shlex.quote(root), so any path stays part of one word.
    A plain path (the usual case) is inserted as is."""
    out: list[str] = []
    quote = ""
    i = 0
    q = shlex.quote(root)
    while i < len(cmd):
        ref = next((r for r in PLUGIN_ROOT_REFS if cmd.startswith(r, i)), None)
        if ref:
            out.append(root if q == root else f"{quote}{q}{quote}")
            i += len(ref)
            continue
        c = cmd[i]
        if c == "\\" and quote != "'":
            out.append(cmd[i:i + 2])
            i += 2
            continue
        if c in "\"'" and quote in ("", c):
            quote = "" if quote else c
        out.append(c)
        i += 1
    return "".join(out)


def build_settings(inst: Install) -> dict[str, Any]:
    """The package's own hooks.json with ${CLAUDE_PLUGIN_ROOT} resolved to the
    installed plugin dir; every path under it that a command references must exist."""
    try:
        doc = json.loads(inst.hooks_json.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SetupError(f"{inst.hooks_json} not found: unsupported AgentMemory version {inst.version}") from None
    except (OSError, ValueError) as exc:
        raise SetupError(f"cannot read {inst.hooks_json}: {exc}") from None
    hooks = doc.get("hooks") if isinstance(doc, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        raise SetupError(f"{inst.hooks_json} has no hooks")
    root = str(inst.plugin_dir)
    missing: list[str] = []
    for groups in hooks.values():
        for g in groups if isinstance(groups, list) else []:
            for h in g.get("hooks", []) if isinstance(g, dict) else []:
                cmd = h.get("command")
                if not isinstance(cmd, str):
                    continue
                cmd = h["command"] = _resolve_root(cmd, root)
                try:
                    words = shlex.split(cmd)
                except ValueError as exc:
                    raise SetupError(f"cannot parse hook command {cmd!r}: {exc}") from None
                missing += [w for w in words if (w == root or w.startswith(root + "/")) and not Path(w).exists()]
    if missing:
        raise SetupError("hook scripts referenced by hooks.json are missing: " + ", ".join(sorted(set(missing))))
    return {"hooks": hooks}


def build_mcp(inst: Install) -> dict[str, Any]:
    return {
        "mcpServers": {SERVER: json.loads(json.dumps(MCP_ENTRY))},
        "x-cm": {
            "description": f"AgentMemory persistent memory (hooks from {PACKAGE} {inst.version})",
            "generated_by": "cm setup agentmemory",
        },
    }


def _dump(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2) + "\n"


def write_file(path: Path, text: str, mode: int = 0o600) -> None:
    """Atomic write, created with `mode` (the files hold no secrets, but users
    sometimes paste one in later)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _existing_definition(ctx: Ctx, generated: Path) -> str | None:
    """Another `agentmemory` server cm already discovers (e.g. a hand-made file)."""
    from .catalog import discover_local

    try:
        servers, _ = discover_local(Path.cwd(), [])
    except Exception:  # noqa: BLE001 - informational only
        return None
    for s in servers:
        if s.name == SERVER and s.source != f"file:{tilde(generated)}":
            return s.source
    return None


def generate(ctx: Ctx, inst: Install) -> list[Path]:
    """Write the two files; existing different files are replaced only after a yes
    (the old one is kept as .bak-<timestamp>)."""
    folder = default_mcp_dir()
    mcp_path, st_path = folder / MCP_FILE, folder / SETTINGS_FILE
    wanted = {mcp_path: _dump(build_mcp(inst)), st_path: _dump(build_settings(inst))}

    other = _existing_definition(ctx, mcp_path)
    if other and not mcp_path.exists():
        if other.startswith("file:"):  # already set up for cm by hand: don't duplicate it unasked
            ctx.say(f"  An `{SERVER}` server is already defined in {other}.")
            if not ctx.confirm(f"Also generate {tilde(mcp_path)}? (cm uses the definition it finds first: "
                               "`sources`, then mcp_folders in order; see `cm --where`)", False):
                ctx.say("  Left your existing AgentMemory config alone; nothing generated.")
                return []
        else:  # ~/.claude.json or a project .mcp.json: cm's folder files take precedence over those
            ctx.say(f"  An `{SERVER}` server is also defined in {other} (left untouched); cm will use the "
                    "generated file, since files in mcp_folders take precedence over ~/.claude.json and .mcp.json.")

    written: list[Path] = []
    for path, text in wanted.items():
        if path.exists():
            try:
                current = path.read_text(encoding="utf-8")
            except OSError:
                current = None
            if current == text:
                ctx.say(f"  {tilde(path)} is up to date")
                continue
            if not ctx.confirm(f"{tilde(path)} exists and differs. Replace it (a .bak copy is kept)?", False):
                ctx.say(f"  kept {tilde(path)} unchanged")
                continue
            backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(path, backup)
            os.chmod(backup, 0o600)
            ctx.say(f"  backup: {tilde(backup)}")
        write_file(path, text)
        written.append(path)
        ctx.say(f"  wrote {tilde(path)}")

    folders = cfg_mod.mcp_folders()
    if folder not in folders:
        ctx.say(f"  {tilde(folder)} is not in mcp_folders in {tilde(cfg_mod.config_file())}.")
        if ctx.confirm(f"Add {tilde(folder)} to mcp_folders (first, so it wins on duplicates)?", True):
            cfg = cfg_mod.load_config()
            raw = cfg.get("mcp_folders")
            current = [raw] if isinstance(raw, str) else list(raw or [])
            cfg_mod.save_config({"mcp_folders": [tilde(folder), *current]})
        else:
            ctx.say(f"  cm won't see the generated files until {tilde(folder)} is in mcp_folders")
    return written


# ---------------------------------------------------------------------------
# user service


def _service_path(ctx: Ctx) -> Path:
    if ctx.platform == "darwin":
        return home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    return xdg_config_home(ctx.env) / "systemd" / "user" / SYSTEMD_UNIT


def _other_agentmemory_services(ctx: Ctx) -> list[Path]:
    if ctx.platform == "darwin":
        d = home() / "Library" / "LaunchAgents"
        return sorted(p for p in d.glob("*agentmemory*.plist")) if d.is_dir() else []
    d = xdg_config_home(ctx.env) / "systemd" / "user"
    return sorted(p for p in d.glob("*agentmemory*.service")) if d.is_dir() else []


def service_files(ctx: Ctx, inst: Install, node: str) -> tuple[Path, str]:
    """-> (path, content). The service runs `node <package>/dist/cli.mjs`; no
    secrets: the backend reads ~/.agentmemory/.env and manages its own secret."""
    if inst.cli is None:
        raise SetupError(f"cannot find the backend entry point in {inst.package_dir}/package.json")
    logs = config_dir() / "logs"
    path_env = ":".join(dict.fromkeys([str(Path(node).parent), "/usr/local/bin", "/opt/homebrew/bin",
                                       "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    target = _service_path(ctx)
    if ctx.platform == "darwin":
        doc = {
            "Label": SERVICE_LABEL,
            "ProgramArguments": [node, str(inst.cli)],
            "EnvironmentVariables": {"PATH": path_env},
            "WorkingDirectory": str(home()),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "StandardOutPath": str(logs / "agentmemory.log"),
            "StandardErrorPath": str(logs / "agentmemory.log"),
        }
        return target, plistlib.dumps(doc).decode()

    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    unit = (
        "[Unit]\n"
        "Description=AgentMemory backend (registered by cm setup agentmemory)\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={q(node)} {q(str(inst.cli))}\n"
        f"Environment={q('PATH=' + path_env)}\n"
        f"WorkingDirectory={home()}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    return target, unit


def service_step(ctx: Ctx, inst: Install) -> None:
    url = (ctx.env.get("AGENTMEMORY_URL") or DEFAULT_AGENTMEMORY_URL).rstrip("/")
    state = ctx.livez(url)
    if state == "backend up":
        ctx.say(f"  backend already running at {url}; cm leaves it (and any service running it) alone")
        return
    existing = _other_agentmemory_services(ctx)
    if existing:
        ctx.say(f"  a service definition already exists ({', '.join(tilde(p) for p in existing)}); left alone")
        return
    if ctx.platform != "darwin" and not ctx.which("systemctl"):
        ctx.say("  systemd not found: start the backend yourself with `agentmemory` (or a process manager)")
        return
    kind = "launchd agent" if ctx.platform == "darwin" else "systemd --user service"
    if not ctx.confirm(f"Register the AgentMemory backend as a {kind} (starts at login)?", False):
        ctx.say("  no service registered; start the backend yourself with `agentmemory` when you need it")
        return
    node = ctx.which("node")
    if not node:
        raise SetupError("node not found")
    target, content = service_files(ctx, inst, node)
    (config_dir() / "logs").mkdir(parents=True, exist_ok=True)
    write_file(target, content, mode=0o644)
    ctx.say(f"  wrote {tilde(target)}")
    if ctx.platform == "darwin":
        start = ["launchctl", "bootstrap", f"gui/{ctx.uid}", str(target)]
    else:
        for argv in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", SYSTEMD_UNIT]):
            p = ctx.capture(argv)
            if p.returncode != 0:
                ctx.say(f"  `{' '.join(argv)}` failed: {(p.stderr or '').strip()[:200]}")
                return
        start = ["systemctl", "--user", "start", SYSTEMD_UNIT]
    if ctx.confirm("Start the backend now?", True):
        p = ctx.capture(start)
        ctx.say("  started" if p.returncode == 0 else f"  `{' '.join(start)}` failed: {(p.stderr or '').strip()[:200]}")
    else:
        ctx.say(f"  start it later with: {' '.join(start)}")


# ---------------------------------------------------------------------------
# entry point


def _persist_gate(value: str, ctx: Ctx) -> None:
    try:
        cfg_mod.save_config({GATE: value})
    except cfg_mod.ConfigError as exc:
        ctx.say(f"  could not save {GATE}={value}: {exc}")


def run_setup(ctx: Ctx | None = None, *, from_installer: bool = False, with_service: bool = True) -> int:
    ctx = ctx or Ctx()
    try:
        gate = gate_value(ctx.env)
    except SetupError as exc:
        ctx.say(f"cm: {exc}")
        return 2

    if gate == "0":
        if not from_installer:
            ctx.say(f"AgentMemory setup is turned off ({GATE}=0). "
                    f"Run `{GATE}=1 cm setup agentmemory` to run it anyway.")
        return 0
    if gate is None:
        if not ctx.interactive:
            ctx.say("AgentMemory: skipped (no terminal to ask). Run `cm setup agentmemory` later.")
            return 0
        ctx.say(ABOUT)
        if not ctx.ask("Set up AgentMemory now?", False):
            _persist_gate("0", ctx)
            ctx.say(f"OK, not asking again ({GATE}=0 saved in {tilde(cfg_mod.config_file())}). "
                    f"Run `{GATE}=1 cm setup agentmemory` any time.")
            return 0
        _persist_gate("1", ctx)

    ctx.say("AgentMemory setup")
    try:
        major = node_major(ctx)
        if major is None or not ctx.which("npm"):
            ctx.say(f"  Node.js >= {MIN_NODE} with npm is required and was not found on PATH.\n"
                    "  Install it (https://nodejs.org, `brew install node`, your distro's package, or nvm), "
                    "then run `cm setup agentmemory` again.")
            return 1
        if major < MIN_NODE:
            ctx.say(f"  Node.js {major} found; AgentMemory needs >= {MIN_NODE}. Upgrade Node, then re-run.")
            return 1

        inst = find_install(ctx)
        if inst is None:
            if not ctx.confirm(f"Install {PACKAGE} globally (`npm install -g {PACKAGE}`)?", True):
                ctx.say("  not installed; nothing else to do. Re-run `cm setup agentmemory` when ready.")
                return 1
            p = ctx.run(["npm", "install", "-g", PACKAGE], stdin=subprocess.DEVNULL)
            if p.returncode != 0:
                ctx.say("  npm install failed. If it is a permissions error, configure a user-writable npm "
                        "prefix (https://docs.npmjs.com/resolving-eacces-permissions-errors-when-installing-"
                        "packages-globally) or use nvm; cm never uses sudo.")
                return 1
            inst = find_install(ctx)
            if inst is None:
                ctx.say(f"  {PACKAGE} still not found under `npm root -g` after installing")
                return 1
        else:
            ctx.say(f"  {PACKAGE} {inst.version} already installed ({tilde(inst.package_dir)}); "
                    f"upgrade with `npm install -g {PACKAGE}@latest`, then re-run this command")

        generate(ctx, inst)
        if with_service:
            service_step(ctx, inst)
    except SetupError as exc:
        ctx.say(f"  error: {exc}")
        return 1

    ctx.say(
        "Done. Select `agentmemory` in cm (e.g. `cm --mcp agentmemory`); its hooks load only in those sessions.\n"
        "  Secret: AgentMemory >= 0.9.30 creates ~/.agentmemory/secret and its MCP server and hooks read it.\n"
        "  To use your own, export AGENTMEMORY_SECRET in the shell you start cm from; the generated file only\n"
        "  references ${AGENTMEMORY_SECRET:-} and never stores it.\n"
        "  Re-run `cm setup agentmemory` after upgrading AgentMemory or switching Node versions (hook paths\n"
        "  point into the current global npm install)."
    )
    return 0
