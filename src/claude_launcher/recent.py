"""Recent Claude Code sessions of a project, read-only.

Claude Code keeps one transcript per session in

    <claude dir>/projects/<project key>/<session id>.jsonl

(<claude dir> is ~/.claude or $CLAUDE_CONFIG_DIR), one JSON record per line.
This format is NOT documented: everything here was read off Claude Code
2.1.281 and is parsed defensively. Unknown records are ignored, and when no
file can be understood at all the result says "unavailable" instead of
raising.

What cm uses, and nothing else:

* the project key: the absolute working directory with every character
  outside [A-Za-z0-9] replaced by "-"; keys longer than 200 characters are cut
  to 200 and get "-" plus a base-36 hash of the path (Claude's own rule);
* top-level *.jsonl files only (subdirectories hold subagents, tool output and
  title sidecars; `agent-*.jsonl` files are subagent transcripts);
* `{"type": "custom-title", "customTitle": ...}` (set by /rename or --name) and
  `{"type": "ai-title", "aiTitle": ...}`: the last of each wins, an empty custom
  title means "cleared". Claude re-appends both near the end of the file, so
  they are read tail-first; `<session id>/custom-title.json` (a sidecar Claude
  writes on rename) is the fallback for a custom title that isn't in the tail;
* `timestamp` of the records for the last activity (else the file's mtime),
  `isSidechain` to skip subagent-only files, and the first real user prompt
  as the name of last resort (shortened to ~60 characters, never more). A
  file without a real prompt or reply (e.g. only a /mcp command) is skipped.

Reads are bounded (a tail window, then at most a larger one, and a head
window), use plain read-only opens (no locks) and never write anything.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import worktree as wt_mod
from .paths import claude_dir

DEFAULT_LIMIT = 3
MAX_LIMIT = 50
KEY_MAX = 200  # Claude cuts project keys longer than this and appends a hash
TAIL_FIRST = 64 * 1024  # first tail window
TAIL_MAX = 1024 * 1024  # second (last) tail window, when the first had no custom title
HEAD = 64 * 1024  # head window: first prompt, a conversation record, the worktree a session entered
SIDECAR_MAX = 64 * 1024
SCAN_SLACK = 40  # extra files looked at beyond `limit` (skipped files, mtime vs. activity order)
PROMPT_CHARS = 60

WT_MARK = "⎇"

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
# a prompt that is really a command, hook output or an interruption marker (Claude skips these too)
_NOT_A_PROMPT = re.compile(r"^(?:\s*<[a-z][\w-]*[\s>]|\[Request interrupted by user)")
_CONVERSATION = ("user", "assistant")
# record types whose presence shows the file is a transcript cm understands
_KNOWN = {"user", "assistant", "system", "summary", "custom-title", "ai-title", "attachment", "last-prompt"}


@dataclass(frozen=True)
class RecentSession:
    id: str
    name: str  # custom title, else AI title, else the shortened first prompt
    source: str  # "custom" | "ai" | "prompt" | "none"
    last_activity: datetime  # aware, UTC
    # Set when the session belongs to another checkout of the repository (a git worktree, or the
    # main checkout seen from a worktree): its label ("⎇ <worktree>") and the directory claude
    # has to be started in to resume it. None for the current directory's own sessions.
    worktree: str | None = None
    cwd: str | None = None
    # The directory the conversation last ran in (`cwd` of its last record). A session started in
    # the main checkout and moved into a worktree (`claude -w … --resume`) keeps its transcript here
    # but now lives in that worktree. Internal: never shown or exported.
    last_cwd: str | None = None
    # The last worktree Claude recorded for it (`worktree-state` -> `worktreeSession.worktreePath`),
    # kept even after the session left the worktree (Claude then records `worktreeSession: null`,
    # and a plain resume starts in the main checkout). Internal, like last_cwd.
    worktree_path: str | None = None

    @property
    def custom(self) -> bool:
        return self.source == "custom"

    @property
    def title(self) -> str | None:
        """The session's real name (custom or AI title); None when `name` is only a fallback."""
        return self.name if self.source in ("custom", "ai") else None

    def age(self, now: datetime | None = None) -> str:
        return age(self.last_activity, now)

    @property
    def mark(self) -> str:
        """"⎇ <worktree>" for a session of another checkout, else ""."""
        return f"{WT_MARK} {self.worktree}" if self.worktree else ""

    def label(self, now: datetime | None = None, width: int = 48) -> str:
        return f"{shorten(self.name, width)} · {self.age(now)}" + (f" · {self.mark}" if self.worktree else "")

    def to_json(self, now: datetime | None = None) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "name_source": self.source, "custom_name": self.custom,
                "last_activity": self.last_activity.isoformat(), "age": self.age(now),
                "worktree": self.worktree, "cwd": self.cwd}


@dataclass
class Recent:
    status: str  # "ok" | "empty" | "unavailable"
    sessions: list[RecentSession] = field(default_factory=list)
    reason: str = ""
    project_dirs: list[Path] = field(default_factory=list)


# ---------------------------------------------------------------------------
# helpers


def shorten(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def age(when: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    secs = (now - when).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    if secs < 30 * 86400:
        return f"{int(secs // 86400)} d ago"
    return when.astimezone().strftime("%Y-%m-%d")


def _java_hash(s: str) -> int:
    """Claude's string hash: h = h*31 + code unit (UTF-16), as a signed 32-bit int."""
    h = 0
    data = s.encode("utf-16-le")
    for i in range(0, len(data), 2):
        h = (h * 31 + int.from_bytes(data[i:i + 2], "little")) & 0xFFFFFFFF
    return h - (1 << 32) if h >= (1 << 31) else h


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out


def project_key(cwd: str | os.PathLike[str]) -> str:
    s = str(cwd)
    # JavaScript's /[^a-zA-Z0-9]/g works on UTF-16 code units: a character outside the BMP is two dashes
    key = "".join(c if c.isascii() and c.isalnum() else "-" * (2 if ord(c) > 0xFFFF else 1) for c in s)
    if len(key) <= KEY_MAX:
        return key
    return f"{key[:KEY_MAX]}-{_base36(abs(_java_hash(s)))}"


def projects_root() -> Path:
    return claude_dir() / "projects"


def project_dirs(cwd: str | os.PathLike[str]) -> list[Path]:
    """The project directories Claude would use for `cwd` (as given and fully resolved)."""
    seen: list[Path] = []
    for c in dict.fromkeys([str(cwd), os.path.realpath(cwd)]):
        d = projects_root() / project_key(c)
        if d not in seen and d.is_dir():
            seen.append(d)
    return seen


def _parse_ts(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _prompt_text(rec: dict[str, Any]) -> str | None:
    """The text of a real user prompt, shortened; None for tool results, meta records and commands."""
    if rec.get("isMeta"):
        return None
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(x["text"] for x in content
                        if isinstance(x, dict) and x.get("type") == "text" and isinstance(x.get("text"), str))
    else:
        return None
    text = text.strip()
    if not text or _NOT_A_PROMPT.match(text):
        return None
    return shorten(text, PROMPT_CHARS)


def _records(chunk: bytes, *, partial_first: bool = False, partial_last: bool = False) -> list[dict[str, Any]]:
    """JSON-object records with a string "type"; anything else (bad JSON, cut lines) is skipped."""
    lines = chunk.split(b"\n")
    if partial_first:
        lines = lines[1:]
    if partial_last and lines:
        lines = lines[:-1]
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(rec, dict) and isinstance(rec.get("type"), str):
            out.append(rec)
    return out


class _Scan:
    """What one transcript's windows tell (every field optional: the format may change)."""

    def __init__(self) -> None:
        self.custom: str | None = None  # last custom-title value ("" = cleared)
        self.ai: str | None = None
        self.last_ts: datetime | None = None
        self.prompt: str | None = None  # first real user prompt seen in the window
        self.main = False  # a real prompt or an assistant reply outside sidechains
        self.side = False
        self.known = False
        self.last_cwd: str | None = None  # `cwd` of the last main-conversation record in the window
        self.worktree_path: str | None = None  # last non-null worktreeSession.worktreePath in the window

    def feed(self, recs: list[dict[str, Any]]) -> None:
        """Records in file order: later titles win, the first prompt is kept."""
        for rec in recs:
            t = rec["type"]
            self.known = self.known or t in _KNOWN
            if t == "worktree-state":
                ws = rec.get("worktreeSession")
                if isinstance(ws, dict) and isinstance(ws.get("worktreePath"), str) and ws["worktreePath"]:
                    self.worktree_path = ws["worktreePath"]
            if t in _CONVERSATION and rec.get("isSidechain") is not True and isinstance(rec.get("cwd"), str):
                self.last_cwd = rec["cwd"]
            if t in _CONVERSATION and rec.get("isSidechain") is True:
                self.side = True
            elif t == "assistant":
                self.main = True
            elif t == "user" and (prompt := _prompt_text(rec)):
                # only slash commands / their output (e.g. a lone /mcp) is not a conversation
                self.main = True
                if self.prompt is None:
                    self.prompt = prompt
            elif t == "custom-title" and isinstance(rec.get("customTitle"), str):
                self.custom = rec["customTitle"].strip()
            elif t == "ai-title" and isinstance(rec.get("aiTitle"), str) and rec["aiTitle"].strip():
                self.ai = rec["aiTitle"].strip()
            ts = _parse_ts(rec.get("timestamp"))
            if ts and (self.last_ts is None or ts > self.last_ts):
                self.last_ts = ts


def _read_sidecar(path: Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            doc = json.loads(fh.read(SIDECAR_MAX))
    except (OSError, ValueError):
        return None
    v = doc.get("customTitle") if isinstance(doc, dict) else None
    return v.strip() if isinstance(v, str) and v.strip() else None


def read_session(path: Path, *, want_worktree: bool = False) -> RecentSession | str:
    """One transcript -> a RecentSession, or why it was skipped:
    "empty", "sidechain", "unreadable", "unknown" (no record cm understands)."""
    sid = path.stem
    try:
        with open(path, "rb") as fh:  # read-only, no lock: Claude may be appending meanwhile
            st = os.fstat(fh.fileno())
            size = st.st_size
            if size == 0:
                return "empty"
            # Tail first: titles are re-appended near the end. A second, larger window only if the
            # first had no custom title (an AI title may still be overridden by an earlier rename).
            for window in (TAIL_FIRST, TAIL_MAX):
                n = min(size, window)
                fh.seek(size - n)
                tail = _Scan()
                tail.feed(_records(fh.read(n), partial_first=n < size))
                if tail.custom is not None or n == size:
                    break
            whole = n == size
            head = _Scan()
            if not whole and (tail.custom is None or not tail.main or not (tail.custom or tail.ai)
                              or (want_worktree and tail.worktree_path is None)):
                fh.seek(0)
                head.feed(_records(fh.read(min(HEAD, size - n)), partial_last=True))
    except OSError:
        return "unreadable"

    if not (tail.known or head.known):
        return "unknown"
    if not (tail.main or head.main):
        return "sidechain" if (tail.side or head.side) else "empty"
    # the tail is later in the file, so its titles win; the head only fills gaps
    custom = tail.custom if tail.custom is not None else head.custom
    if custom is None:
        custom = _read_sidecar(path.parent / sid / "custom-title.json")
    ai = tail.ai or head.ai
    prompt = tail.prompt if whole else head.prompt
    if custom:
        name, source = custom, "custom"
    elif ai:
        name, source = ai, "ai"
    elif prompt:
        name, source = prompt, "prompt"
    else:
        name, source = f"session {sid[:8]}", "none"
    when = tail.last_ts or head.last_ts or datetime.fromtimestamp(st.st_mtime, timezone.utc)
    return RecentSession(id=sid, name=name, source=source, last_activity=when,
                         last_cwd=tail.last_cwd or head.last_cwd,
                         worktree_path=tail.worktree_path or head.worktree_path)


@dataclass(frozen=True)
class _Source:
    """One project directory to read, and which checkout it belongs to."""
    dir: Path
    worktree: str | None = None  # label of another checkout, None for the current directory
    cwd: str | None = None  # that checkout's directory


def sources(cwd: str | os.PathLike[str], worktrees: bool = True) -> list[_Source]:
    """The current directory's project dirs, then (with `worktrees`) those of the repository's
    other checkouts: its git worktrees (`.claude/worktrees/*` included) and, seen from a worktree,
    the main checkout. Bounded (worktree.MAX_CHECKOUTS) and read-only."""
    out = [_Source(d) for d in project_dirs(cwd)]
    if not worktrees:
        return out
    seen = {s.dir for s in out}
    for co in wt_mod.other_checkouts(Path(cwd)):
        for d in project_dirs(co.path):
            if d not in seen:
                seen.add(d)
                out.append(_Source(d, co.label, str(co.path)))
    return out


def _candidates(srcs: Iterable[_Source]) -> list[tuple[float, Path, _Source]]:
    out: list[tuple[float, Path, _Source]] = []
    for src in srcs:
        try:
            entries = list(os.scandir(src.dir))
        except OSError:
            continue
        for e in entries:
            if not e.name.endswith(".jsonl") or e.name.startswith("agent-"):
                continue
            if not ID_RE.match(e.name[: -len(".jsonl")]):
                continue
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
                out.append((e.stat(follow_symlinks=False).st_mtime, Path(e.path), src))
            except OSError:
                continue
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def _tag(s: RecentSession, src: _Source, checkouts: list[wt_mod.Checkout] | None = None) -> RecentSession:
    """Mark a session of another checkout. A session stored with this directory's transcripts but
    last run inside another checkout of the same repository (moved there with `claude -w …
    --resume`) is marked and resumed from that checkout too, so cm takes you back where it was."""
    if src.worktree:
        return replace(s, worktree=src.worktree, cwd=src.cwd)
    if not checkouts:
        return s
    # the worktree Claude last recorded (still there, same repository) wins over the last cwd,
    # which points at the main checkout once the session has left the worktree
    for hint in (s.worktree_path, s.last_cwd):
        if not hint:
            continue
        last = Path(os.path.realpath(hint))
        for co in checkouts:
            root = Path(os.path.realpath(co.path))
            if last == root or root in last.parents:
                return replace(s, worktree=co.label, cwd=str(co.path))
    return s


def _checkouts(cwd: str | os.PathLike[str], worktrees: bool) -> list[wt_mod.Checkout]:
    return wt_mod.other_checkouts(Path(cwd)) if worktrees else []


def list_recent(cwd: str | os.PathLike[str], limit: int = DEFAULT_LIMIT, *, worktrees: bool = True) -> Recent:
    """The project's sessions, newest activity first, including those of the repository's other
    checkouts (marked) unless `worktrees` is False. Never raises."""
    try:
        srcs = sources(cwd, worktrees)
        dirs = [s.dir for s in srcs]
        if not dirs:
            return Recent("empty", reason="no Claude Code sessions for this directory yet")
        if limit <= 0:
            return Recent("empty", reason="recent_sessions is 0", project_dirs=dirs)
        found: dict[str, RecentSession] = {}
        skipped: dict[str, int] = {}
        files = _candidates(srcs)
        checkouts = _checkouts(cwd, worktrees)
        for _, path, src in files[: limit + SCAN_SLACK]:
            # the head (where Claude records entering a worktree) only matters when there are other
            # checkouts to resume into
            r = read_session(path, want_worktree=bool(checkouts))
            if isinstance(r, RecentSession):
                r = _tag(r, src, checkouts)
                old = found.get(r.id)  # the same id in two checkouts: the newer copy wins
                if old is None or r.last_activity > old.last_activity:
                    found[r.id] = r
                if len(found) >= limit + 2:  # a little slack: mtime order ~ activity order
                    break
            else:
                skipped[r] = skipped.get(r, 0) + 1
        if found:
            ordered = sorted(found.values(), key=lambda s: s.last_activity, reverse=True)
            return Recent("ok", ordered[:limit], project_dirs=dirs)
        bad = skipped.get("unknown", 0) + skipped.get("unreadable", 0)
        if bad:
            return Recent("unavailable", reason=f"{bad} session file(s) could not be read or understood "
                                                 "(Claude Code's undocumented format may have changed)",
                          project_dirs=dirs)
        return Recent("empty", reason="no Claude Code sessions for this directory yet", project_dirs=dirs)
    except Exception as exc:  # noqa: BLE001 - a format change must never break cm
        return Recent("unavailable", reason=f"could not list sessions: {exc}")


def find(cwd: str | os.PathLike[str], session_id: str, *, worktrees: bool = True) -> RecentSession | None:
    """One session of this project (or, with `worktrees`, of another checkout of its repository)
    by id. None if absent, unreadable or not a valid id. Never raises."""
    if not ID_RE.match(session_id or ""):
        return None
    try:
        checkouts = _checkouts(cwd, worktrees)
        for src in sources(cwd, worktrees):
            p = src.dir / f"{session_id}.jsonl"
            if p.is_file() and not p.is_symlink():
                r = read_session(p, want_worktree=bool(checkouts))
                return _tag(r, src, checkouts) if isinstance(r, RecentSession) else None
    except Exception:  # noqa: BLE001
        return None
    return None


def _norm_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def find_by_name(cwd: str | os.PathLike[str], name: str, *, limit: int = MAX_LIMIT,
                 worktrees: bool = True) -> RecentSession | None:
    """The most recent session of this project (or, with `worktrees`, of another checkout of its
    repository) whose custom name (/rename or --name) is `name`, compared trimmed and
    case-insensitively. Only custom names count (an AI title or a first prompt is not a name you
    gave), and only among the `limit` most recent sessions. None if there is none. Never raises."""
    want = _norm_name(name or "")
    if not want:
        return None
    res = list_recent(cwd, limit, worktrees=worktrees)
    for s in res.sessions:  # newest activity first
        if s.custom and _norm_name(s.name) == want:
            return s
    return None


# ---------------------------------------------------------------------------
# the session a launch created: a before/after diff of the transcript files

MAX_SNAPSHOT_FILES = 20000  # per project directory; beyond that the diff gives up (no guessing)


@dataclass
class Snapshot:
    """Which transcript files existed, per project directory, before a launch. Directories that
    didn't exist yet are recorded as empty (a new project, or a worktree claude -w creates)."""
    files: dict[Path, set[str]] = field(default_factory=dict)
    places: list[str] = field(default_factory=list)  # the directories it was taken for
    complete: bool = True  # False when a directory was too large or unreadable


def _dirs_for(places: Iterable[str | os.PathLike[str]]) -> dict[Path, _Source]:
    """The project directories of exactly these places (existing or not yet): where this launch's
    session can be written. Not the repository's other checkouts: a session started meanwhile in a
    sibling worktree must never be taken for this launch's (the -w worktree and a resume's checkout
    are passed in as places already)."""
    out: dict[Path, _Source] = {}
    for c in places:
        for v in dict.fromkeys([str(c), os.path.realpath(c)]):
            out.setdefault(projects_root() / project_key(v), _Source(projects_root() / project_key(v)))
    return out


def _transcripts(d: Path) -> set[str] | None:
    """Top-level session transcript names (the same rule as the Recent list), or None when the
    directory is too large or can't be read. A missing directory has none."""
    try:
        it = os.scandir(d)
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    out: set[str] = set()
    with it:
        for n, e in enumerate(it):
            if n >= MAX_SNAPSHOT_FILES:
                return None
            if not e.name.endswith(".jsonl") or e.name.startswith("agent-"):
                continue
            if not ID_RE.match(e.name[: -len(".jsonl")]):
                continue
            try:
                if e.is_file(follow_symlinks=False):
                    out.add(e.name)
            except OSError:
                continue
    return out


def snapshot(places: Iterable[str | os.PathLike[str]]) -> Snapshot:
    """Before a launch. Read-only and bounded (names only, MAX_CHECKOUTS checkouts). Never raises."""
    places = [str(p) for p in places]
    snap = Snapshot(places=places)
    try:
        for d in _dirs_for(places):
            names = _transcripts(d)
            if names is None:
                snap.complete = False
                continue
            snap.files[d] = names
    except Exception:  # noqa: BLE001
        snap.complete = False
    return snap


def new_session(before: Snapshot, extra: Iterable[str | os.PathLike[str]] = ()) -> RecentSession | None:
    """After claude exited: the one session file that appeared since `before` in any of its
    project directories (or those of `extra` places, e.g. the worktree claude -w created), as a
    RecentSession (its name and, for another checkout, its directory). None when no file or
    several appeared, or the snapshot was incomplete: never a guess. Never raises."""
    if not before.complete:
        return None
    try:
        places = list(dict.fromkeys([*before.places, *map(str, extra)]))
        dirs = _dirs_for(places)
        found: list[tuple[Path, _Source]] = []
        for d, src in dirs.items():
            names = _transcripts(d)
            if names is None:
                return None
            for n in sorted(names - before.files.get(d, set())):
                found.append((d / n, src))
            if len(found) > 1:
                return None
        if len(found) != 1:
            return None
        path, src = found[0]
        sid = path.name[: -len(".jsonl")]
        checkouts = _checkouts(before.places[0], True) if before.places else []
        r = read_session(path, want_worktree=bool(checkouts))
        if not isinstance(r, RecentSession):
            # e.g. nothing said yet: still the session claude created, just without a name
            r = RecentSession(id=sid, name=f"session {sid[:8]}", source="none",
                              last_activity=datetime.now(timezone.utc))
        if src.worktree:
            return _tag(r, src, checkouts)
        own = {projects_root() / project_key(v) for p in before.places[:1] for v in (p, os.path.realpath(p))}
        if path.parent not in own:
            # a place other than the launch directory (a worktree claude -w created)
            for p in places:
                if path.parent in {projects_root() / project_key(v) for v in (p, os.path.realpath(p))}:
                    return replace(r, cwd=p if os.path.isdir(p) else None)
        return _tag(r, src, checkouts)
    except Exception:  # noqa: BLE001 - remembering must never fail a launch
        return None
