"""Named profiles in ~/.config/claude-launcher/profiles/<name>.json."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .catalog import Catalog
from .paths import config_dir
from .plan import Selection

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


class ProfileError(ValueError):
    pass


def profiles_dir() -> Path:
    return config_dir() / "profiles"


def _path(name: str) -> Path:
    if not NAME_RE.match(name):
        raise ProfileError(f"invalid profile name {name!r} (letters, digits, . _ + -)")
    return profiles_dir() / f"{name}.json"


def list_profiles() -> list[str]:
    d = profiles_dir()
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def load(name: str) -> Selection:
    p = _path(name)
    try:
        return Selection.from_dict(json.loads(p.read_text()))
    except FileNotFoundError:
        raise ProfileError(f"no profile named {name!r}") from None
    except ValueError as exc:
        raise ProfileError(f"profile {name!r} is not valid JSON: {exc}") from None


def save(name: str, sel: Selection) -> Path:
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = sel.to_dict()
    data.pop("resume_id", None)  # a specific conversation id isn't a reusable preference
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, p)
    return p


def delete(name: str) -> None:
    try:
        _path(name).unlink()
    except FileNotFoundError:
        raise ProfileError(f"no profile named {name!r}") from None
    if get_default() == name:
        set_default(None)


def _settings_path() -> Path:
    return config_dir() / "config.json"


def _read_settings() -> dict:
    try:
        return json.loads(_settings_path().read_text())
    except (OSError, ValueError):
        return {}


def get_default() -> str | None:
    name = _read_settings().get("default_profile")
    return name if isinstance(name, str) and name in list_profiles() else None


def set_default(name: str | None) -> None:
    if name is not None and name not in list_profiles():
        raise ProfileError(f"no profile named {name!r}")
    s = _read_settings()
    s["default_profile"] = name
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2) + "\n")


def reconcile(sel: Selection, cat: Catalog) -> tuple[Selection, list[str]]:
    """Drop entries that no longer exist (server removed, connector gone...)."""
    notes: list[str] = []
    local, miss = cat.resolve_local(sel.local)
    notes += [f"MCP server {m!r} no longer found" for m in miss]
    conns, miss = cat.resolve_connectors(sel.connectors)
    notes += [f"connector {m!r} no longer found" for m in miss]
    plugins, miss = cat.resolve_plugins(sel.plugins)
    notes += [f"plugin {m!r} not enabled/installed here" for m in miss]
    cmux = sel.cmux and cat.cmux.available
    if sel.cmux and not cmux:
        notes.append(f"cmux unavailable: {cat.cmux.reason}")
    out = Selection.from_dict({**sel.to_dict(), "local": local, "connectors": conns, "plugins": plugins, "cmux": cmux})
    return out, notes
