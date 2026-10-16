"""Filesystem locations. Everything is derived from the environment at call
time so tests can point HOME / CM_CONFIG_DIR at a temporary directory."""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def expand(p: str | os.PathLike[str]) -> Path:
    s = str(p)
    if s == "~" or s.startswith("~/"):
        s = str(home()) + s[1:]
    return Path(os.path.expandvars(s))


def config_dir() -> Path:
    """cm's own state: profiles, connector cache, measured costs."""
    if os.environ.get("CM_CONFIG_DIR"):
        return expand(os.environ["CM_CONFIG_DIR"])
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = expand(xdg) if xdg else home() / ".config"
    return base / "claude-launcher"


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


def managed_dir() -> Path:
    return Path(os.environ.get("CM_MANAGED_DIR", "/Library/Application Support/ClaudeCode"))


def default_mcp_folders() -> list[Path]:
    """User folders scanned for MCP config files (read-only)."""
    return [home() / ".personal" / "claude-mcps", home() / ".personal"]
