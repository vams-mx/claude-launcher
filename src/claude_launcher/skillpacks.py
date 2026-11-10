"""Skill packs: optional skills loaded for one session only.

    ~/.claude-launcher/skills/<pack>/            ($CM_CONFIG_DIR respected)
        .claude/skills/<skill>/SKILL.md          one folder per skill (frontmatter name / description)
        pack.json                                optional: {"description": "...",
                                                   "requires": [{"command": "playwright-cli",
                                                                 "install_hint": "npm i -g @playwright/cli"}]}

cm passes `--add-dir <pack>` for each ticked pack: Claude Code reads the skills in an added
directory's .claude/skills, so they exist in that session and nowhere else. Nothing is installed
and Claude's own files are never touched. (`--plugin-dir` was used up to cm 0.5.2, but newer
Claude Code versions, e.g. 2.1.322, then stop loading the installed plugins, so plugin agents such
as basic-ops-team:operator went missing.) Skills in ~/.claude/skills and the project's
.claude/skills always load; cm can't turn those off per session.

A pack made by cm 0.5.0-0.5.2 keeps its skills in skills/ (the plugin layout); cm moves that folder
to .claude/skills at the next launch. A leftover .claude-plugin/plugin.json is harmless.

Discovery is read-only. Only `ensure_layout` (at launch) and `cm skills add/remove` write, always
inside the packs directory.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import frontmatter
from .paths import config_dir, tilde

PACK_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
MANIFEST = Path(".claude-plugin") / "plugin.json"  # cm <= 0.5.2 packs only: read for its description
SKILLS = Path(".claude") / "skills"  # what `claude --add-dir <pack>` reads
LEGACY_SKILLS = Path("skills")  # cm 0.5.0-0.5.2 layout, moved to SKILLS at launch
PACK_FILE = "pack.json"
MAX_PACKS = 100
MAX_SKILLS = 200
_MAX_JSON = 256 * 1024


class PackError(ValueError):
    pass


@dataclass
class Requirement:
    command: str
    install_hint: str = ""
    found: bool = True

    def describe(self) -> str:
        hint = f": {self.install_hint}" if self.install_hint else ""
        return f"needs {self.command} (not found){hint}"


@dataclass
class Skill:
    name: str
    description: str = ""


@dataclass
class SkillPack:
    name: str
    path: Path
    description: str = ""
    skills: list[Skill] = field(default_factory=list)
    requires: list[Requirement] = field(default_factory=list)
    error: str | None = None  # can't be loaded as it is: selecting it blocks the launch
    notes: list[str] = field(default_factory=list)  # worth knowing, never blocking
    legacy: bool = False  # skills still in skills/ (cm 0.5.0-0.5.2): moved to .claude/skills at launch

    @property
    def key(self) -> str:
        return f"pack:{self.name}"

    @property
    def missing(self) -> list[Requirement]:
        return [r for r in self.requires if not r.found]

    def warnings(self) -> list[str]:
        return [r.describe() for r in self.missing] + list(self.notes)

    def estimated_tokens(self) -> int:
        """What the pack adds upfront: each skill's name and description in the skill listing
        (a rough 4 characters per token; the bodies load only when a skill is used)."""
        return sum((len(s.name) + len(s.description) + 30) // 4 for s in self.skills)


def packs_dir() -> Path:
    return config_dir() / "skills"


def valid_name(name: str) -> str | None:
    if PACK_RE.match(name or ""):
        return None
    return (f"invalid skill pack name {name!r}: lowercase letters, digits and '-' (kebab-case, "
            "like a plugin name), at most 64 characters")


def _read_json(path: Path) -> object:
    with open(path, "rb") as fh:
        return json.loads(fh.read(_MAX_JSON))


def _skill_dirs(skills: Path) -> list[Path]:
    try:
        entries = sorted(os.scandir(skills), key=lambda e: e.name)
    except OSError:
        return []
    out = []
    for e in entries:
        if e.name.startswith("."):
            continue
        p = Path(e.path)
        try:
            if p.is_dir() and (p / "SKILL.md").is_file():
                out.append(p)
        except OSError:
            continue
        if len(out) >= MAX_SKILLS:
            break
    return out


def load(path: Path) -> SkillPack:
    """One pack directory -> SkillPack. Never raises; problems land in `error` / `notes`."""
    pack = SkillPack(path.name, path)
    manifest_desc = ""
    m = path / MANIFEST
    if m.is_file():
        try:
            doc = _read_json(m)
            if not isinstance(doc, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError):
            pass  # only read for an old pack's description; no longer needed
        else:
            if isinstance(doc.get("description"), str):
                manifest_desc = doc["description"].strip()
    meta = path / PACK_FILE
    if meta.is_file():
        try:
            doc = _read_json(meta)
            if not isinstance(doc, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as exc:
            pack.notes.append(f"{PACK_FILE} ignored ({exc.__class__.__name__})")
            doc = {}
        if isinstance(doc.get("description"), str):
            pack.description = doc["description"].strip()
        reqs = doc.get("requires", [])
        if not isinstance(reqs, list):
            pack.notes.append(f"{PACK_FILE}: 'requires' is not a list; ignored")
            reqs = []
        for r in reqs:
            cmd = r.get("command") if isinstance(r, dict) else None
            if not (isinstance(cmd, str) and _COMMAND_RE.match(cmd)):
                pack.notes.append(f"{PACK_FILE}: a 'requires' entry has no valid command; ignored")
                continue
            hint = r.get("install_hint") if isinstance(r.get("install_hint"), str) else ""
            pack.requires.append(Requirement(cmd, hint.strip(), shutil.which(cmd) is not None))
    pack.description = pack.description or manifest_desc
    root = path / SKILLS
    if not root.is_dir() and (path / LEGACY_SKILLS).is_dir():
        root, pack.legacy = path / LEGACY_SKILLS, True
    if (path / "CLAUDE.md").exists() or (path / ".claude" / "CLAUDE.md").exists():
        pack.notes.append("has a CLAUDE.md: claude may load it with the pack's directory (--add-dir)")
    for d in _skill_dirs(root):
        fm = frontmatter.read(d / "SKILL.md")
        name = fm.get("name", "").strip() or d.name
        desc = fm.get("description", "").strip()
        if not desc:
            pack.notes.append(f"skill {name} has no description in its frontmatter (Claude may not offer it)")
        pack.skills.append(Skill(name, desc))
    if not pack.skills:
        pack.error = "no skills found (expected .claude/skills/<skill>/SKILL.md)"
    return pack


def discover(warnings: list[str] | None = None) -> list[SkillPack]:
    d = packs_dir()
    try:
        entries = sorted(os.scandir(d), key=lambda e: e.name)
    except OSError:
        return []
    packs: list[SkillPack] = []
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            if not Path(e.path).is_dir():
                continue
        except OSError:
            continue
        if valid_name(e.name):
            if warnings is not None:
                warnings.append(f"skill pack folder {tilde(Path(e.path))} ignored: names are lowercase letters, "
                                "digits and '-'")
            continue
        packs.append(load(Path(e.path)))
        if len(packs) >= MAX_PACKS:
            break
    return packs


# ---------------------------------------------------------------------------
# writing (inside packs_dir only)


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def ensure_layout(pack: SkillPack) -> str | None:
    """Move an old pack's skills/ to .claude/skills (what --add-dir reads). Returns a warning instead
    of raising; never writes through a linked pack."""
    if not pack.legacy:
        return None
    if pack.path.is_symlink():
        return (f"skill pack {pack.name}: its skills are in skills/; move them to .claude/skills in "
                f"{tilde(pack.path.resolve())} so claude finds them")
    try:
        (pack.path / ".claude").mkdir(exist_ok=True)
        os.rename(pack.path / LEGACY_SKILLS, pack.path / SKILLS)
    except OSError as exc:
        return f"skill pack {pack.name}: could not move skills/ to .claude/skills: {exc.strerror or exc}"
    pack.legacy = False
    return None


def _source_skills(src: Path) -> list[Path]:
    """The skill folders `src` provides: itself (it holds SKILL.md), else its children that do, else
    those of its skills/ folder."""
    if (src / "SKILL.md").is_file():
        return [src]
    for base in (src, src / "skills"):
        found = _skill_dirs(base)
        if found:
            return found
    return []


def parse_requirement(spec: str) -> dict:
    """`CMD` or `CMD=INSTALL HINT` (cm skills add --requires)."""
    cmd, _, hint = spec.partition("=")
    cmd = cmd.strip()
    if not _COMMAND_RE.match(cmd):
        raise PackError(f"--requires {spec!r}: expected COMMAND or COMMAND=INSTALL_HINT")
    return {"command": cmd, "install_hint": hint.strip()} if hint.strip() else {"command": cmd}


def add(name: str, src: Path, *, link: bool = False, description: str = "",
        requires: list[dict] | None = None) -> SkillPack:
    if why := valid_name(name):
        raise PackError(why)
    src = Path(os.path.abspath(src))
    if not src.is_dir():
        raise PackError(f"{src} is not a directory")
    skills = _source_skills(src)
    if not skills:
        raise PackError(f"no skills in {src}: expected a folder with SKILL.md, or folders that each hold one")
    dest = packs_dir() / name
    if dest.exists() or dest.is_symlink():
        raise PackError(f"skill pack {name!r} already exists ({tilde(dest)}); `cm skills remove {name}` first")
    root = packs_dir()
    if root.resolve() in src.resolve().parents or src.resolve() == root.resolve():
        raise PackError("the source is inside the skill packs folder")
    try:
        (dest / SKILLS).mkdir(parents=True)
        for s in skills:
            target = dest / SKILLS / s.name
            if link:
                os.symlink(s, target, target_is_directory=True)
            else:
                shutil.copytree(s, target, symlinks=True)
        if description or requires:
            meta: dict = {}
            if description:
                meta["description"] = description
            if requires:
                meta["requires"] = requires
            _write_json(dest / PACK_FILE, meta)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)  # only what this call created
        raise
    return load(dest)


def remove(name: str) -> Path:
    if why := valid_name(name):
        raise PackError(why)
    dest = packs_dir() / name
    if dest.is_symlink():
        dest.unlink()  # a linked pack: the link only, never what it points to
        return dest
    if not dest.is_dir():
        raise PackError(f"no skill pack named {name!r}")
    shutil.rmtree(dest)  # symlinked skills inside are unlinked, not followed
    return dest
