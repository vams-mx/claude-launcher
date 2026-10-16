"""Filesystem locations. Everything is derived from the environment at call
time so tests can point HOME / CM_CONFIG_DIR at a temporary directory."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

# Managed (enterprise policy) settings directories per OS, as documented by Claude Code.
MANAGED_DIRS = {
    "darwin": Path("/Library/Application Support/ClaudeCode"),
    "linux": Path("/etc/claude-code"),
}


def home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def expand(p: str | os.PathLike[str]) -> Path:
    s = str(p)
    if s == "~" or s.startswith("~/"):
        s = str(home()) + s[1:]
    return Path(os.path.expandvars(s))


def tilde(p: Path) -> str:
    """Show a path relative to $HOME as ~/..."""
    h = str(home())
    s = str(p)
    if h and s == h:
        return "~"
    return "~" + s[len(h):] if h and s.startswith(h + "/") else s


def config_dir() -> Path:
    """cm's home: config.json, profiles, mcps/, connector cache, measured costs.

    ~/.claude-launcher, or $CM_CONFIG_DIR when set."""
    if os.environ.get("CM_CONFIG_DIR"):
        return expand(os.environ["CM_CONFIG_DIR"])
    return home() / ".claude-launcher"


def xdg_config_home(env: Mapping[str, str] | None = None) -> Path:
    """$XDG_CONFIG_HOME, else ~/.config."""
    xdg = (os.environ if env is None else env).get("XDG_CONFIG_HOME")
    return expand(xdg) if xdg else home() / ".config"


def legacy_config_dir() -> Path:
    """Where cm kept its state before 0.2 ($XDG_CONFIG_HOME/claude-launcher)."""
    return xdg_config_home() / "claude-launcher"


def config_file() -> Path:
    return config_dir() / "config.json"


def default_mcp_dir() -> Path:
    """The one folder scanned when config.json has no `mcp_folders`."""
    return config_dir() / "mcps"


def claude_dir() -> Path:
    """~/.claude (or $CLAUDE_CONFIG_DIR)."""
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return expand(os.environ["CLAUDE_CONFIG_DIR"])
    return home() / ".claude"


def claude_json() -> Path:
    """~/.claude.json holds user- and local-scope MCP servers."""
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return expand(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
    return home() / ".claude.json"


def managed_dir(platform: str | None = None) -> Path:
    """Directory holding managed-settings.json / managed-mcp.json ($CM_MANAGED_DIR overrides)."""
    if os.environ.get("CM_MANAGED_DIR"):
        return Path(os.environ["CM_MANAGED_DIR"])
    plat = platform or sys.platform
    key = "linux" if plat.startswith("linux") else plat
    return MANAGED_DIRS.get(key, MANAGED_DIRS["linux"])
