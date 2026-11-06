"""The last selection launched from the interactive screen, per project.

    ~/.claude-launcher/last/<key>.json        (0700 / 0600, written atomically;
                                              $CM_CONFIG_DIR respected)

<key> is a hash of the project's absolute path; the path itself is stored in
the file (for `cm --where` and debugging). The project is the git repository
the working directory belongs to, else the working directory itself
(`project_root`, which never runs git).

Only reusable preferences are kept in "selection": local MCPs, connectors,
plugins, skill packs, cmux, the model, the agent, keep awake (macOS; kept but
ignored elsewhere) and the extra args forwarded after `--`. Permissions are always stored (and read
back) as "standard"; the session mode, resume id, fork flag, session name,
worktree and tmux are never part of it.

Next to it, "last_session" (config `remember_last_session`, default true) is
the session that launch ran: {"id", "name", "dir"} (name and dir optional; dir
= the checkout it was resumed in when that is another worktree). It is only a
pointer: the screen pre-selects resuming it if the session still exists
(cli.py checks with recent.find), and never stores Fork, Worktree or tmux.
cm knows it without passing --session-id: a resume is the id chosen, anything
else is the one transcript file that appeared while claude ran (cli.py,
recent.new_session); none or several -> no session is stored.

The file is written only after a launch from the screen actually started
claude, and read only when the screen opens without selection flags; headless
launches never touch it. `remember_last: false` in config.json turns both off.
Nothing here ever fails a launch: problems come back as warnings.

Like sessions/, this is per-machine data (absolute paths of this machine).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import awake as awake_mod
from .paths import config_dir, home, tilde
from .plan import MODELS, Selection
from .worktree import gitdir_of as _gitdir_of
from .worktree import worktree_repo as _worktree_repo

FORMAT = 1
KEY_LEN = 32  # hex chars (128 bits)
_MAX_READ = 1024 * 1024
_LISTS = ("local", "connectors", "plugins", "skill_packs")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")  # recent.ID_RE: a transcript file name


def last_dir() -> Path:
    return config_dir() / "last"


# ---------------------------------------------------------------------------
# the project key


def project_root(cwd: Path) -> Path:
    """The project `cwd` belongs to, found without running git: walk up to the first directory
    holding `.git`.

    * `.git` directory: that directory is the project.
    * `.git` file (gitdir: …): a linked worktree belongs to its main checkout (or its bare
      repository), so all worktrees of a repository share one entry; a submodule or a
      `--separate-git-dir` checkout is its own project.
    * no `.git` up to the root: `cwd` itself.

    A repository at $HOME itself (a dotfiles repository) only counts for $HOME: it would
    otherwise make every directory under ~ one project."""
    start = Path(os.path.realpath(cwd))
    h = Path(os.path.realpath(home()))
    for d in (start, *start.parents):
        if d == h and start != h:
            break
        dotgit = d / ".git"
        try:
            if dotgit.is_dir():
                return d
            if dotgit.is_file():
                gitdir = _gitdir_of(dotgit)
                return (_worktree_repo(gitdir) if gitdir else None) or d
        except OSError:  # an unreadable parent: keep walking
            continue
    return start


def key(root: Path) -> str:
    return hashlib.sha256(str(root).encode("utf-8", "surrogateescape")).hexdigest()[:KEY_LEN]


def entry_path(root: Path) -> Path:
    return last_dir() / f"{key(root)}.json"


# ---------------------------------------------------------------------------
# reading


@dataclass(frozen=True)
class LastSession:
    id: str
    name: str | None = None
    dir: str | None = None  # the checkout it ran in, when that is another worktree of the repository

    def label(self) -> str:
        return self.name or self.id[:8]


@dataclass
class Entry:
    project: Path
    path: Path
    selection: Selection
    saved_at: str | None
    notes: list[str]  # e.g. an unknown model that was reset
    session: LastSession | None = None


class _Corrupt(ValueError):
    pass


def _parse_session(raw: Any) -> LastSession | None:
    """The "last_session" pointer, or None when absent or not understood (never fatal)."""
    if not isinstance(raw, dict):
        return None
    sid = raw.get("id")
    if not (isinstance(sid, str) and _ID_RE.match(sid)):
        return None
    name = raw.get("name") if isinstance(raw.get("name"), str) and raw["name"].strip() else None
    d = raw.get("dir") if isinstance(raw.get("dir"), str) and raw["dir"] else None
    return LastSession(sid, name.strip() if name else None, d)


def _parse(doc: Any, root: Path) -> tuple[Selection, str | None, list[str]]:
    if not isinstance(doc, dict):
        raise _Corrupt("not a JSON object")
    if doc.get("format") != FORMAT:
        raise _Corrupt(f"unknown format {doc.get('format')!r}")
    if doc.get("project") != str(root):
        raise _Corrupt(f"it belongs to another project ({doc.get('project')!r})")
    s = doc.get("selection")
    if not isinstance(s, dict):
        raise _Corrupt("no selection object")
    out: dict[str, Any] = {}
    for k in _LISTS:
        v = s.get(k, [])
        if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            raise _Corrupt(f"{k!r} is not a list of names")
        out[k] = list(dict.fromkeys(v))
    cmux = s.get("cmux", False)
    if not isinstance(cmux, bool):
        raise _Corrupt("'cmux' is not true/false")
    model = s.get("model", "default")
    if not isinstance(model, str):
        raise _Corrupt("'model' is not a string")
    notes: list[str] = []
    if model not in MODELS:
        notes.append(f"model {model!r} is unknown; using Default")
        model = "default"
    awake, note = awake_mod.normalize(s.get("awake", "off"))
    if note:
        notes.append(note)
    extra = s.get("extra_args", [])
    if not (isinstance(extra, list) and all(isinstance(x, str) for x in extra)):
        raise _Corrupt("'extra_args' is not a list of strings")
    agent = s.get("agent")
    if agent is not None and not isinstance(agent, str):
        raise _Corrupt("'agent' is not a string")
    saved_at = doc.get("saved_at") if isinstance(doc.get("saved_at"), str) else None
    # permissions, session mode, resume id, fork and name are never taken from the selection
    return (Selection(**out, cmux=cmux, model=model, awake=awake, extra_args=list(extra),
                      agent=(agent or "").strip() or None), saved_at, notes)


def load(cwd: Path) -> tuple[Entry | None, list[str]]:
    """This project's last selection, or None. Never raises: problems are returned as warnings
    and the file is then ignored (a corrupt one is left in place; the next launch replaces it)."""
    try:
        root = project_root(cwd)
        path = entry_path(root)
    except Exception as exc:  # noqa: BLE001 - must never fail the screen
        return None, [f"last selection unavailable: {exc}"]
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_MAX_READ + 1)
    except FileNotFoundError:
        return None, []
    except OSError as exc:
        return None, [f"cannot read the last selection {tilde(path)} ({exc.strerror or exc}); ignoring it"]
    try:
        if len(raw) > _MAX_READ:
            raise _Corrupt("too large")
        try:
            doc = json.loads(raw.decode("utf-8"))
        except ValueError as exc:  # also UnicodeDecodeError
            raise _Corrupt(f"not valid JSON: {exc}") from None
        sel, saved_at, notes = _parse(doc, root)
    except _Corrupt as exc:
        return None, [f"ignoring the last selection {tilde(path)}: {exc}"]
    return Entry(project=root, path=path, selection=sel, saved_at=saved_at, notes=notes,
                 session=_parse_session(doc.get("last_session"))), []


# ---------------------------------------------------------------------------
# writing


def to_doc(root: Path, sel: Selection, session: LastSession | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "format": FORMAT,
        "project": str(root),
        "saved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "selection": {
            "local": list(sel.local), "connectors": list(sel.connectors), "plugins": list(sel.plugins),
            "skill_packs": list(sel.skill_packs),
            "cmux": bool(sel.cmux), "model": sel.model, "agent": sel.agent, "awake": sel.awake,
            "extra_args": list(sel.extra_args),
            "permissions": "standard",  # never "skip", whatever the launch used
        },
    }
    if session is not None:
        doc["last_session"] = {k: v for k, v in (("id", session.id), ("name", session.name), ("dir", session.dir))
                               if v}
    return doc


def _private_dir(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.path.islink(d) or not os.path.isdir(d):
        raise OSError(f"{tilde(d)} is not a directory")
    st = os.stat(d)
    if st.st_mode & 0o077 and st.st_uid == os.getuid():
        os.chmod(d, 0o700)


def save(cwd: Path, sel: Selection, session: LastSession | None = None) -> str | None:
    """Remember `sel` (and the session it ran, if known) for cwd's project. Returns a warning
    instead of raising."""
    path: Path | None = None
    try:
        root = project_root(cwd)
        path = entry_path(root)
        _private_dir(path.parent)
        data = (json.dumps(to_doc(root, sel, session), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)  # 0600
        try:
            with os.fdopen(fd, "wb") as fh:
                os.fchmod(fh.fileno(), 0o600)
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except Exception as exc:  # noqa: BLE001 - remembering must never fail a launch
        where = tilde(path) if path else tilde(last_dir())
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
        return f"could not remember this selection in {where}: {reason}"
    return None


def forget(cwd: Path) -> tuple[Path, Path, bool]:
    """Delete cwd's project entry: (project, file, whether it existed). Raises OSError."""
    root = project_root(cwd)
    path = entry_path(root)
    try:
        path.unlink()
    except FileNotFoundError:
        return root, path, False
    return root, path, True
