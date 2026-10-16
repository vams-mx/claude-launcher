"""cm's own configuration in ~/.claude-launcher/config.json (or $CM_CONFIG_DIR).

Keys (all optional; keys starting with `_` are documentation and ignored):

  mcp_folders           folders scanned for *.json files with a top-level
                        "mcpServers" object. Absent -> ["~/.claude-launcher/mcps"].
  sources               explicit files: [{"mcp_config": "...", "settings": "...",
                        "description": "..."}]
  default_profile       profile that plain `cm` opens with
  theme                 screen colours (a cm-* theme or any Textual theme)
  INSTALL_AGENT_MEMORY  "0" / "1" / absent: the AgentMemory setup gate
                        (the environment variable of the same name wins)

Also: the one-time migration from the pre-0.2 home (~/.config/claude-launcher).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from .paths import config_dir, config_file, default_mcp_dir, expand, legacy_config_dir, tilde

# Before 0.2 these were hard-coded defaults. They are only written into a
# *migrated* config so an existing setup keeps working; new installs never see them.
LEGACY_DEFAULT_MCP_FOLDERS = ["~/.personal/claude-mcps", "~/.personal"]
DOCS_URL = "https://github.com/vams-mx/claude-launcher#configuration"

COMMENT = [
    "cm configuration (plain JSON). Keys starting with '_' are comments and are ignored.",
    "mcp_folders: folders scanned for *.json files that have a top-level \"mcpServers\" object.",
    "  Add as many as you like; the first definition of a server name wins. `cm --where` shows them.",
    "sources: explicit files, e.g. {\"mcp_config\": \"~/x/foo-mcp.json\", \"settings\": \"~/x/foo-hooks.json\", "
    "\"description\": \"...\"}",
    "  A companion settings file (hooks) loads only when one of that file's servers is selected.",
    "default_profile: the profile plain `cm` opens with (cm --set-default NAME).",
    "theme: colours of the interactive screen: cm-graphite (default), cm-slate, cm-moss, cm-ink, cm-paper, cm-sand, cm-mist, or any Textual theme. Ctrl+T cycles.",
    "INSTALL_AGENT_MEMORY: absent = ask once, \"0\" = never ask, \"1\" = run `cm setup agentmemory`.",
    "  The environment variable INSTALL_AGENT_MEMORY overrides this value.",
    f"Docs: {DOCS_URL}",
]


def example_config() -> dict[str, Any]:
    return {"_comment": list(COMMENT), "mcp_folders": [tilde(default_mcp_dir())], "sources": []}


class ConfigError(ValueError):
    pass


def _read(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    if not isinstance(doc, dict):
        raise ConfigError(f"{path} is not a JSON object")
    return doc


def load_config(warnings: list[str] | None = None) -> dict[str, Any]:
    """config.json, plus `sources` / `mcp_folders` from a legacy catalog.json
    in the same directory when config.json doesn't set them."""
    out: dict[str, Any] = {}
    for f in (config_dir() / "catalog.json", config_file()):
        try:
            out.update(_read(f))
        except ConfigError as exc:
            if warnings is not None:
                warnings.append(str(exc))
    return out


def save_config(updates: dict[str, Any]) -> Path:
    """Merge `updates` into config.json (atomic; other keys and comments kept).
    A value of None removes the key."""
    path = config_file()
    doc = _read(path)
    for k, v in updates.items():
        if v is None:
            doc.pop(k, None)
        else:
            doc[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def _folder_list(raw: Any) -> list[str]:
    """`mcp_folders` as written by hand: a list of paths, or a single path string."""
    if isinstance(raw, str):
        raw = [raw]
    return [p for p in raw if isinstance(p, str) and p.strip()] if isinstance(raw, list) else []


def mcp_folders(cfg: dict[str, Any] | None = None) -> list[Path]:
    cfg = load_config() if cfg is None else cfg
    raw = cfg.get("mcp_folders")
    if raw is None:
        return [default_mcp_dir()]
    return [expand(p) for p in _folder_list(raw)]


def sources(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = load_config() if cfg is None else cfg
    return [s for s in (cfg.get("sources") or []) if isinstance(s, dict) and s.get("mcp_config")]


# ---------------------------------------------------------------------------
# one-time migration and home initialisation


def migrate_legacy(notify: Callable[[str], None] | None = None) -> bool:
    """Copy ~/.config/claude-launcher -> ~/.claude-launcher once.

    Runs only when the new home doesn't exist, the old one does and
    $CM_CONFIG_DIR isn't set. With $CM_CONFIG_DIR set (an explicit choice; pre-0.2
    cm ignored the old directory then too) it only says so. The old directory is
    never modified or deleted. The legacy defaults for MCP folders are written
    into the new config.json so discovery keeps finding the same servers.
    Returns True if it migrated."""
    new, old = config_dir(), legacy_config_dir()
    if new.exists() or not old.is_dir():
        return False
    notify = notify or (lambda m: print(m, file=sys.stderr))
    if os.environ.get("CM_CONFIG_DIR"):
        notify(f"cm: CM_CONFIG_DIR is set, so {tilde(old)} was not copied to {tilde(new)}; "
               "copy its config.json / profiles there yourself if you want them")
        return False

    try:
        legacy_catalog = _read(old / "catalog.json")
    except ConfigError:
        legacy_catalog = {}
    try:
        legacy_config = _read(old / "config.json")
    except ConfigError:
        legacy_config = {}

    staging = Path(tempfile.mkdtemp(prefix=".claude-launcher-migrate-", dir=new.parent))
    try:
        target = staging / "home"
        shutil.copytree(old, target, symlinks=True, ignore=shutil.ignore_patterns("catalog.json", "*.tmp"))
        cfg = dict(legacy_config)
        old_folders = _folder_list(legacy_catalog.get("mcp_folders")) or LEGACY_DEFAULT_MCP_FOLDERS
        cfg["mcp_folders"] = list(dict.fromkeys([tilde(default_mcp_dir()), *old_folders]))
        if legacy_catalog.get("sources"):
            cfg["sources"] = legacy_catalog["sources"]
        cfg.setdefault("_comment", list(COMMENT))
        cfg = {"_comment": cfg.pop("_comment"), **cfg}
        (target / "config.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        (target / "mcps").mkdir(exist_ok=True)
        os.rename(target, new)  # atomic; fails if something created `new` meanwhile
    except OSError:
        if new.exists():
            return False
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    notify(f"cm: config home is now {tilde(new)}; copied once from {tilde(old)} "
           f"(left untouched, no longer read). MCP folders kept: {', '.join(cfg['mcp_folders'])}")
    return True


def init_home() -> list[str]:
    """Create the config home with a commented example config.json and an empty
    mcps/ folder. Never overwrites anything. Returns what was created."""
    created: list[str] = []
    d = config_dir()
    if not d.exists():
        d.mkdir(parents=True)
        created.append(tilde(d) + "/")
    mcps = default_mcp_dir()
    if not mcps.exists():
        mcps.mkdir(parents=True)
        created.append(tilde(mcps) + "/")
    cf = config_file()
    if not cf.exists():
        try:
            fd = os.open(cf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return created
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(example_config(), indent=2) + "\n")
        created.append(tilde(cf))
    return created
