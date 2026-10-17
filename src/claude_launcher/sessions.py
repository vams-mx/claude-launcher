"""Persistent, content-addressed session files.

Every launch passes Claude two files: `--mcp-config=<dir>/mcp.json` and
`--settings <dir>/settings.json`. They live in

    ~/.claude-launcher/sessions/<id>/        (0700; $CM_CONFIG_DIR respected,
                                              $CM_SESSIONS_DIR overrides the whole location)

and are **kept** after Claude exits, because cmux restores a session by
replaying the original `claude` argv (plus `--resume <id>`): the files must
still be there then.

<id> is a hash of the two documents' canonical JSON (the exact bytes written),
so launching the same selection again reuses the same directory and files, and
files written on another machine (different hook paths, cmux client, ...) can
never be mistaken for this machine's.

"Last used" is the directory's modification time. cm refreshes it on every
launch, and the settings carry a SessionStart hook that touches the directory,
so a session started or resumed without cm (a cmux restore replays the argv)
counts as use too. The hook is not part of the id: the id stays a pure
function of the selection. `cleanup` removes directories unused for longer than
`session_retention_days` (config.json, default 60).

Concurrency: `ensure` holds a shared flock on sessions/.lock while it creates
and writes a directory; `cleanup` needs the exclusive lock (non-blocking) and
skips entirely when it is busy, so it never deletes a directory another cm is
about to launch with.

Only the documents cm builds go into these files: server definitions exactly as
configured (`${VAR}` references unexpanded) and the session settings. Secrets
from cm's environment (e.g. the cmux-cua token) are never written here.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .paths import config_dir, expand, home, tilde

FORMAT = b"cm-session-v1"  # part of the hash: a future change of format never reuses old directories
DEFAULT_RETENTION_DAYS = 60
FILES = ("mcp.json", "settings.json")
ID_LEN = 32  # hex chars (128 bits)

_ID_RE = re.compile(rf"[0-9a-f]{{{ID_LEN}}}")
# what a session directory may contain: the two files and our own atomic-write temp files
_OWN_ENTRY_RE = re.compile(r"(?:mcp|settings)\.json|\.(?:mcp|settings)\.json\.[A-Za-z0-9_]+\.tmp")
_MAX_READ = 16 * 1024 * 1024
LOCK_NAME = ".lock"  # not an id: cleanup never touches it
LOCK_WAIT = 2.0  # seconds ensure() waits for the shared lock before going on without it
# a shell comment ending cm's own hook command: how session_id recognises (and ignores) it
TOUCH_MARKER = "# cm: session last used"
BACKUP_XATTR = b"com.apple.metadata:com_apple_backup_excludeItem"  # what `tmutil addexclusion` sets
SYNCED_FOLDERS = ("Library/Mobile Documents", "Library/CloudStorage", "Dropbox", "Google Drive", "OneDrive")


def sessions_dir() -> Path:
    """$CM_SESSIONS_DIR, else <config home>/sessions. Always absolute (argv, hooks, cmux restore)."""
    if os.environ.get("CM_SESSIONS_DIR"):
        return Path(os.path.abspath(expand(os.environ["CM_SESSIONS_DIR"])))
    return config_dir() / "sessions"


def synced_folder(path: Path | None = None) -> str | None:
    """The cloud-synced folder (iCloud Drive, Dropbox, ...) `path` lies in, if any."""
    path = sessions_dir() if path is None else path
    h = home()
    for p in dict.fromkeys([path, Path(os.path.realpath(path))]):
        for name in SYNCED_FOLDERS:
            base = h / name
            if p == base or base in p.parents:
                return tilde(base)
    return None


def doc_bytes(doc: Any) -> bytes:
    """Canonical serialisation: sorted keys, fixed indentation, ASCII only."""
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


# ---------------------------------------------------------------------------
# the "last used" hook


def touch_hook_group(d: Path) -> dict[str, Any]:
    """SessionStart hook group that marks `d` as used. SessionStart also fires on
    --resume, so a cmux restore counts. Silent (no output reaches Claude's
    context), never fails, plain POSIX `touch` (macOS and Linux); `-c` never
    creates the directory again once it is gone."""
    cmd = f"touch -c -- {shlex.quote(str(d))} >/dev/null 2>&1 || true {TOUCH_MARKER}"
    return {"hooks": [{"type": "command", "command": cmd}]}


def _is_touch_group(g: Any) -> bool:
    if not (isinstance(g, dict) and set(g) == {"hooks"} and isinstance(g["hooks"], list) and len(g["hooks"]) == 1):
        return False
    h = g["hooks"][0]
    return isinstance(h, dict) and h.get("type") == "command" and str(h.get("command", "")).endswith(TOUCH_MARKER)


def without_touch_hook(settings: dict[str, Any]) -> dict[str, Any]:
    """The settings without cm's touch hook, in canonical form (an empty
    SessionStart list or hooks object counts as absent)."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    starts = hooks.get("SessionStart")
    starts_changes = isinstance(starts, list) and (not starts or any(_is_touch_group(g) for g in starts))
    if hooks and not starts_changes:
        return settings
    s = copy.deepcopy(settings)
    if starts_changes:
        kept = [g for g in starts if not _is_touch_group(g)]
        if kept:
            s["hooks"]["SessionStart"] = kept
        else:
            del s["hooks"]["SessionStart"]
    if not s["hooks"]:
        del s["hooks"]
    return s


def with_touch_hook(settings: dict[str, Any], d: Path) -> dict[str, Any]:
    """`settings` plus the touch hook for `d`, appended after any companion SessionStart hooks."""
    s = copy.deepcopy(without_touch_hook(settings))
    hooks = s.setdefault("hooks", {})
    if not isinstance(hooks, dict):  # malformed companion settings: Claude will say so; don't clobber them
        return s
    starts = hooks.setdefault("SessionStart", [])
    if isinstance(starts, list):
        starts.append(touch_hook_group(d))
    return s


def session_id(mcp_doc: dict[str, Any], settings_doc: dict[str, Any]) -> str:
    """Hash of the canonical documents, ignoring cm's own touch hook (which names the directory)."""
    h = hashlib.sha256(FORMAT + b"\0")
    h.update(doc_bytes(mcp_doc))
    h.update(b"\0")  # never inside canonical JSON (ensure_ascii escapes it)
    h.update(doc_bytes(without_touch_hook(settings_doc)))
    return h.hexdigest()[:ID_LEN]


def session_path(mcp_doc: dict[str, Any], settings_doc: dict[str, Any]) -> Path:
    """Where these documents live. Pure: creates nothing (used by the preview too)."""
    return sessions_dir() / session_id(mcp_doc, settings_doc)


def is_session_name(name: str) -> bool:
    return bool(_ID_RE.fullmatch(name))


# ---------------------------------------------------------------------------
# writing


class NotOwnedError(PermissionError):
    """A session path belongs to another user (typically: cm was once run with sudo)."""


def _user() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return str(os.getuid())


def _check_owned(path: Path, st: os.stat_result, what: str) -> None:
    if st.st_uid != os.getuid():
        q = shlex.quote(str(path))
        raise NotOwnedError(
            f"{tilde(path)} belongs to uid {st.st_uid}, not to you ({_user()}); was cm run with sudo? "
            f"Fix it with `sudo chown -R {_user()} {q}`, or remove it with `sudo rm -rf {q}` ({what})")


def _exclude_from_backup(path: Path) -> None:
    """macOS: exclude from Time Machine like `tmutil addexclusion` (a sticky xattr). Best effort, silent."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import plistlib

        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        libc.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.c_uint32, ctypes.c_int]
        value = plistlib.dumps("com.apple.backupd", fmt=plistlib.FMT_BINARY)
        libc.setxattr(os.fsencode(path), BACKUP_XATTR, value, len(value), 0, 0x0001)  # XATTR_NOFOLLOW
    except Exception:  # noqa: BLE001, S110 - purely cosmetic, silent by design
        pass



def _private_root(root: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        parent = root.parent
        if parent.exists():
            _check_owned(parent, os.stat(parent), "cm recreates what it needs")
        raise
    st = os.lstat(root)
    if stat.S_ISLNK(st.st_mode):
        if not root.is_dir():
            raise NotADirectoryError(f"{root} is a symlink to something that is not a directory")
        return  # the user pointed it elsewhere on purpose; leave its mode alone
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(f"{root} is not a directory")
    _check_owned(root, st, "cm recreates it on the next launch")
    if stat.S_IMODE(st.st_mode) & 0o077:  # only ever tighten: a directory you made read-only stays so
        os.chmod(root, stat.S_IMODE(st.st_mode) & 0o700)
    _exclude_from_backup(root)


def _private_dir(d: Path) -> None:
    """d exists as a real directory (never a symlink), owned by us, mode 0700."""
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):  # a symlink or a file squatting on our id: replace it, never follow it
        os.unlink(d)
        os.mkdir(d, 0o700)
        st = os.lstat(d)
    _check_owned(d, st, "cm recreates it on the next launch")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(d, 0o700)


def _open_lock(root: Path) -> int:
    return os.open(root / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


@contextlib.contextmanager
def _shared_lock(root: Path) -> Iterator[None]:
    """Best effort: if the lock can't be taken within LOCK_WAIT (a cleanup normally holds it for
    milliseconds; a cm stopped mid-cleanup could hold it forever), or not at all (read-only dir,
    odd filesystem), work without it rather than hang the launch."""
    fd = None
    try:
        fd = _open_lock(root)
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
    except OSError:
        if fd is not None:
            os.close(fd)
        fd = None
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)  # releases the lock


def _matches(path: Path, data: bytes) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:  # missing, or a symlink (ELOOP)
        return False
    try:
        st = os.fstat(fd)
        if not (stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()
                and stat.S_IMODE(st.st_mode) == 0o600 and st.st_size == len(data)):
            return False
        with os.fdopen(os.dup(fd), "rb") as fh:
            return fh.read(min(len(data) + 1, _MAX_READ)) == data
    finally:
        os.close(fd)


def _write_atomic(path: Path, data: bytes) -> None:
    """Write via a 0600 temp file in the same directory + rename: readers see the old file or the new one."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def ensure(mcp_doc: dict[str, Any], settings_doc: dict[str, Any]) -> Path:
    """Create or reuse the session directory for these documents and return it.

    The settings written always carry the touch hook for this directory (added
    here if missing). Under a shared lock (see the module docstring), the
    directory's mtime is refreshed ("last used"); files whose content already
    matches are left as they are, anything else (missing, different, wrong
    mode, a symlink) is replaced atomically."""
    d = session_path(mcp_doc, settings_doc)
    files = {"mcp.json": doc_bytes(mcp_doc), "settings.json": doc_bytes(with_touch_hook(settings_doc, d))}
    _private_root(d.parent)
    with _shared_lock(d.parent):
        for attempt in (1, 2):
            try:
                _private_dir(d)
                os.utime(d)
                for name, data in files.items():
                    if not _matches(d / name, data):
                        _write_atomic(d / name, data)
                return d
            except FileNotFoundError:
                if attempt == 2:  # removed under us twice: give up
                    raise
    raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# retention


def retention_days(cfg: dict[str, Any], warnings: list[str] | None = None) -> float | None:
    """`session_retention_days` from config.json: a number of days > 0 (default 60);
    0 keeps session directories forever. Anything else warns and uses the default."""
    raw = cfg.get("session_retention_days", DEFAULT_RETENTION_DAYS)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and not math.isnan(raw) and raw >= 0:
        return None if raw == 0 else float(raw)
    if warnings is not None:
        warnings.append(f"config.json: session_retention_days must be a number of days (0 = keep forever), "
                        f"not {raw!r}; using {DEFAULT_RETENTION_DAYS}")
    return float(DEFAULT_RETENTION_DAYS)


class _OutOfTime(Exception):
    """cleanup ran past its time budget (e.g. it was stopped and resumed): stop deleting."""


def _remove_if_ours(d: Path, cutoff: float, in_budget: Callable[[], bool] = lambda: True) -> bool:
    """Delete a stale session directory, but only if it holds nothing except what cm puts there."""
    names = os.listdir(d)
    if not all(_OWN_ENTRY_RE.fullmatch(n) for n in names):
        return False
    for n in names:
        if stat.S_ISDIR(os.lstat(d / n).st_mode):
            return False
    if os.lstat(d).st_mtime >= cutoff:  # used again in the meantime
        return False
    if not in_budget():  # checked right before deleting: see cleanup()
        raise _OutOfTime
    for n in names:
        try:
            os.unlink(d / n)  # unlink never follows a symlink
        except FileNotFoundError:
            pass
    os.rmdir(d)
    return True


def cleanup(days: float | None, *, now: float | None = None, keep: Path | None = None) -> list[str]:
    """Remove session directories not used for more than `days` days. Returns the ids removed.

    Best effort, cheap (one directory scan) and conservative: only entries of
    `sessions/` named like a session id, that are real directories (never a
    symlink, never followed), owned by the current user and containing only
    cm's own files are removed. A `sessions` directory that is itself a
    symlink is not cleaned at all.

    Runs only with the exclusive lock on sessions/.lock; if another cm holds
    it (it is creating or reusing a directory right now), this cleanup is
    skipped. A launch waits at most LOCK_WAIT for its shared lock and then
    proceeds unlocked, so the lock alone can't protect against a cleanup that
    was stopped (SIGSTOP, Ctrl+Z, a suspended laptop) while holding it. Hence
    the time budget: right before deleting each directory (after its mtime
    re-check), cleanup stops if more than LOCK_WAIT / 2 has passed on the
    monotonic clock since it took the lock. That clock keeps running while a
    process is stopped, so a resumed cleanup stops before deleting anything a
    launch may have reused in the meantime."""
    if not days:
        return []
    root = sessions_dir()
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return []
        lock = _open_lock(root)
    except OSError:
        return []
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:  # busy (BlockingIOError) or no locking here: skip this time
            return []
        t0 = time.monotonic()

        def in_budget() -> bool:
            return time.monotonic() - t0 <= LOCK_WAIT / 2

        cutoff = (time.time() if now is None else now) - days * 86400
        uid = os.getuid()
        removed: list[str] = []
        for e in os.scandir(root):
            if not is_session_name(e.name) or (keep is not None and e.name == keep.name):
                continue
            try:
                st = e.stat(follow_symlinks=False)
                if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or st.st_mtime >= cutoff:
                    continue
                if _remove_if_ours(Path(e.path), cutoff, in_budget):
                    removed.append(e.name)
            except _OutOfTime:
                break
            except OSError:
                continue
        return removed
    except OSError:
        return []
    finally:
        os.close(lock)


def count() -> int:
    try:
        return sum(1 for e in os.scandir(sessions_dir()) if is_session_name(e.name))
    except OSError:
        return 0
