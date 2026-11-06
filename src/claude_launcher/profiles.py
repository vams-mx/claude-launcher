"""Named profiles in ~/.claude-launcher/profiles/<name>.json; the default one is
`default_profile` in ~/.claude-launcher/config.json."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import awake as awake_mod
from . import config as cfg_mod
from .catalog import Catalog
from .paths import config_dir
from .plan import LAUNCH_ONLY, Selection

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
        doc = json.loads(p.read_text())
    except FileNotFoundError:
        raise ProfileError(f"no profile named {name!r}") from None
    except OSError as exc:
        raise ProfileError(f"cannot read profile {name!r}: {exc}") from None
    except ValueError as exc:
        raise ProfileError(f"profile {name!r} is not valid JSON: {exc}") from None
    if not isinstance(doc, dict):
        raise ProfileError(f"profile {name!r} is not a JSON object ({p})")
    # a hand-edited profile can't pin a conversation, a fork or a session name either
    return Selection.from_dict({k: v for k, v in doc.items() if k not in LAUNCH_ONLY})


def save(name: str, sel: Selection) -> Path:
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = sel.to_dict()
    for k in LAUNCH_ONLY:  # a conversation id, a fork or a session name isn't a reusable preference
        data.pop(k, None)
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


def get_default() -> str | None:
    name = cfg_mod.load_config().get("default_profile")
    return name if isinstance(name, str) and name in list_profiles() else None


def set_default(name: str | None) -> None:
    if name is not None and name not in list_profiles():
        raise ProfileError(f"no profile named {name!r}")
    try:
        cfg_mod.save_config({"default_profile": name})
    except cfg_mod.ConfigError as exc:
        raise ProfileError(str(exc)) from None


def reconcile(sel: Selection, cat: Catalog) -> tuple[Selection, list[str]]:
    """Drop entries that no longer exist (server removed, connector gone...)."""
    notes: list[str] = []
    local, miss = cat.resolve_local(sel.local)
    notes += [f"MCP server {m!r} no longer found" for m in miss]
    conns, miss = cat.resolve_connectors(sel.connectors)
    notes += [f"connector {m!r} no longer found" for m in miss]
    plugins, miss = cat.resolve_plugins(sel.plugins)
    notes += [f"plugin {m!r} not enabled/installed here" for m in miss]
    packs, miss = cat.resolve_skill_packs(sel.skill_packs)
    notes += [f"skill pack {m!r} no longer found" for m in miss]
    agent = sel.agent if isinstance(sel.agent, str) and sel.agent.strip() else None
    if agent and cat.agent(agent) is None:
        notes.append(f"agent {agent!r} no longer found; using the default agent")
        agent = None
    elif agent and (ag := cat.agent(agent)) and ag.plugin_id:
        if why := cat.agent_block(ag):
            notes.append(f"{why}; using the default agent")
            agent = None
        elif ag.plugin_id not in plugins:
            plugins = [p.id for p in cat.plugins if p.id in plugins or p.id == ag.plugin_id]
            notes.append(f"agent {agent} comes with plugin {ag.plugin_id}: ticked it")
    cmux = sel.cmux and cat.cmux.available
    if sel.cmux and not cmux:
        notes.append("cmux is not installed here" if not cat.cmux.installed else f"cmux unavailable: {cat.cmux.reason}")
    # a hand-edited keep-awake value; a valid one is kept even where it's ignored (off macOS)
    awake, note = awake_mod.normalize(sel.awake)
    if note:
        notes.append(note)
    out = Selection.from_dict({**sel.to_dict(), "local": local, "connectors": conns, "plugins": plugins, "cmux": cmux,
                               "awake": awake, "skill_packs": packs, "agent": agent})
    return out, notes
