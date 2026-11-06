"""Plugins available in the configured marketplaces, and installing one through Claude Code.

Read-only discovery:
  * ~/.claude/plugins/known_marketplaces.json: {name: {"installLocation": …}}, plus any folder
    of ~/.claude/plugins/marketplaces/ it doesn't list;
  * each marketplace's catalog, <location>/.claude-plugin/marketplace.json (else
    <location>/marketplace.json): {"plugins": [{"name", "description", "category", "version"}, …]};
  * installed_plugins.json (catalog.installed_plugins) to mark what is installed, and where.
Missing or odd files are skipped with a warning; nothing is ever written.

Installing runs `claude plugin install <plugin>@<marketplace> --scope <scope> --json` (an argv,
no shell; stdin closed; a timeout), from the project directory (project and local scope apply
to it). cm never passes --accept-command or -y: when the marketplace declares a command that has
to be accepted, claude refuses without a terminal and reports it, and cm shows it and tells you
to run the install in a terminal yourself, where claude asks.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import catalog as catalog_mod
from .paths import claude_dir, tilde

SCOPES = ("user", "project", "local")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}@[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
INSTALL_TIMEOUT = 300.0
MAX_OUTPUT = 8000
_MAX_CATALOG = 8 * 1024 * 1024
_CONFIRM_HINTS = ("--accept-command", "shownCommand", "-y", "--yes", "confirm")


@dataclass
class Available:
    name: str
    marketplace: str
    description: str = ""
    category: str = ""
    version: str = ""
    installed: list[str] = field(default_factory=list)  # e.g. ["user"], ["local (this project)"]
    installed_here: bool = False

    @property
    def id(self) -> str:
        return f"{self.name}@{self.marketplace}"

    def matches(self, query: str) -> bool:
        q = query.strip().casefold()
        return not q or all(w in f"{self.id} {self.description} {self.category}".casefold() for w in q.split())

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "marketplace": self.marketplace, "description": self.description,
                "category": self.category, "version": self.version, "installed": self.installed,
                "installed_here": self.installed_here}


def _read_json(path: Path, warnings: list[str] | None) -> Any:
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.read(_MAX_CATALOG))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        if warnings is not None:
            warnings.append(f"could not read {tilde(path)}: {exc.__class__.__name__}")
        return None


def marketplaces(warnings: list[str] | None = None) -> dict[str, Path]:
    """{marketplace name: its folder}."""
    root = claude_dir() / "plugins"
    out: dict[str, Path] = {}
    known = _read_json(root / "known_marketplaces.json", warnings)
    if isinstance(known, dict):
        for name, v in known.items():
            loc = v.get("installLocation") if isinstance(v, dict) else None
            if isinstance(name, str) and isinstance(loc, str) and loc:
                out[name] = Path(loc)
    try:
        entries = sorted(os.scandir(root / "marketplaces"), key=lambda e: e.name)
    except OSError:
        entries = []
    for e in entries:
        if not e.name.startswith(".") and e.name not in out and Path(e.path).is_dir():
            out[e.name] = Path(e.path)
    return out


def catalog_file(location: Path) -> Path | None:
    for f in (location / ".claude-plugin" / "marketplace.json", location / "marketplace.json"):
        if f.is_file():
            return f
    return None


def _scope_label(e: dict[str, Any], cwd: Path) -> str:
    scope = e.get("scope") if isinstance(e.get("scope"), str) else "?"
    if scope == "user":
        return "user"
    here = catalog_mod.entry_here([e], cwd) is not None
    return f"{scope} (this project)" if here else f"{scope} (another project)"


def available(cwd: Path, warnings: list[str] | None = None) -> list[Available]:
    installed = catalog_mod.installed_plugins(warnings)
    out: list[Available] = []
    for mname, loc in marketplaces(warnings).items():
        f = catalog_file(loc)
        if f is None:
            if warnings is not None:
                warnings.append(f"marketplace {mname}: no catalog in {tilde(loc)}")
            continue
        doc = _read_json(f, warnings)
        plugins = doc.get("plugins") if isinstance(doc, dict) else None
        if not isinstance(plugins, list):
            if warnings is not None and doc is not None:
                warnings.append(f"marketplace {mname}: {tilde(f)} has no plugins list")
            continue
        seen: set[str] = set()
        for p in plugins:
            name = p.get("name") if isinstance(p, dict) else None
            if not (isinstance(name, str) and ID_RE.match(f"{name}@{mname}")) or name in seen:
                continue
            seen.add(name)

            def text(k: str) -> str:
                v = p.get(k)
                return " ".join(v.split()) if isinstance(v, str) else ""

            entries = installed.get(f"{name}@{mname}", [])
            out.append(Available(name, mname, text("description"), text("category"), text("version"),
                                 [_scope_label(e, cwd) for e in entries],
                                 catalog_mod.entry_here(entries, cwd) is not None))
    return sorted(out, key=lambda a: (a.marketplace.casefold(), a.name.casefold()))


# ---------------------------------------------------------------------------
# installing


@dataclass
class InstallResult:
    ok: bool
    output: str  # what claude printed (stdout + stderr), trimmed
    needs_confirmation: bool = False  # claude wants a marketplace-declared command accepted
    command: str = ""  # that command, when claude reported it
    argv: list[str] = field(default_factory=list)

    def terminal_command(self) -> str:
        return " ".join(self.argv[:-1]) if self.argv[-1:] == ["--json"] else " ".join(self.argv)


def install_argv(claude_bin: str, plugin_id: str, scope: str) -> list[str]:
    if not ID_RE.match(plugin_id or ""):
        raise ValueError(f"expected <plugin>@<marketplace>, not {plugin_id!r}")
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r} (choose from {', '.join(SCOPES)})")
    return [claude_bin, "plugin", "install", plugin_id, "--scope", scope, "--json"]


def _json_lines(text: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict):
                out.append(doc)
    if not out:
        try:
            doc = json.loads(text)
            if isinstance(doc, dict):
                out.append(doc)
        except ValueError:
            pass
    return out


def _shown_command(doc: dict[str, Any]) -> str | None:
    sc = doc.get("shownCommand")
    if isinstance(sc, dict):
        for k in ("command", "display", "text", "argv"):
            v = sc.get(k)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                return " ".join(v)
        return json.dumps(sc)
    if isinstance(sc, str) and sc:
        return sc
    return None


def install(claude_bin: str, plugin_id: str, scope: str, cwd: Path, *,
            run: Callable[..., subprocess.CompletedProcess] | None = None,
            timeout: float = INSTALL_TIMEOUT) -> InstallResult:
    """Never raises for a failed install: the result says what happened."""
    run = run or subprocess.run
    argv = install_argv(claude_bin, plugin_id, scope)
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    try:
        res = run(argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout,
                  stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return InstallResult(False, f"`claude plugin install` did not finish within {timeout:g} s", argv=argv)
    except OSError as exc:
        return InstallResult(False, f"cannot run {claude_bin!r}: {exc.strerror or exc}", argv=argv)
    out = ((res.stdout or "") + ("\n" if res.stdout and res.stderr else "") + (res.stderr or "")).strip()
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + "\n…"
    docs = _json_lines(res.stdout or "")
    command = next((c for d in docs if (c := _shown_command(d))), None)
    failed = res.returncode != 0 or any(d.get("success") is False or d.get("ok") is False for d in docs)
    confirm = command is not None or (failed and any(h in out for h in _CONFIRM_HINTS))
    return InstallResult(not failed and not confirm, out, confirm, command or "", argv)


def advice(r: InstallResult) -> str:
    """One line for the user after an install that did not go through."""
    if r.needs_confirmation:
        cmd = f" ({r.command})" if r.command else ""
        return (f"the marketplace declares a command that has to be accepted{cmd}. cm never accepts it for you: "
                f"run `{r.terminal_command()}` in a terminal, where claude shows it and asks")
    return "the install failed; see claude's output above"
