"""Git worktrees for `claude -w`, inspected read-only (git itself is never run).

cm never creates, deletes or resets a worktree: it only passes `-w <name>`
(and `--tmux`) to claude, and reads the repository's files to

* turn a session name into a worktree name claude accepts (`slug`, `valid_name`),
* tell which combinations claude can't do (`combination_error`, `worktree_block`,
  `tmux_block`),
* say what claude will do with a name that already exists (`inspect`),
* find the repository's other checkouts, whose sessions the Recent list shows
  (`other_checkouts`).

The rules below were read off Claude Code 2.1.281's bundled code (see README,
"Worktrees"): worktrees live in `<main checkout>/.claude/worktrees/<name>` ("/"
in a name becomes "+"), on branch `worktree-<name>`, created with
`git worktree add --no-track -B <branch> <path> <base>` where the base is
origin's default branch (setting `worktree.baseRef`, default "fresh").
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .paths import tilde


MAX_CHECKOUTS = 32  # worktrees looked at for the Recent list (bounded reads)
WORKTREES_DIR = Path(".claude") / "worktrees"  # where claude -w puts its worktrees, under the main checkout
_GITFILE_MAX = 4096
NAME_MAX = 64  # cm's own cap for a derived name


# ---------------------------------------------------------------------------
# the repository


def gitdir_of(dotgit: Path) -> Path | None:
    """The directory a `.git` *file* points to ("gitdir: <path>", relative to the file's directory)."""
    try:
        with open(dotgit, encoding="utf-8") as fh:
            first = fh.readline(4096)
    except (OSError, UnicodeDecodeError):
        return None
    if not first.startswith("gitdir:"):
        return None
    target = first[len("gitdir:"):].strip()
    if not target:
        return None
    p = Path(target)
    return Path(os.path.realpath(p if p.is_absolute() else dotgit.parent / p))


def worktree_repo(gitdir: Path) -> Path | None:
    """For a linked worktree (`git worktree add`), whose gitdir has a `commondir` file: the main
    checkout, or the repository itself when it is bare. None for anything else (a submodule,
    `--separate-git-dir`), which is then its own project."""
    try:
        raw = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw:
        return None
    c = Path(raw)
    common = Path(os.path.realpath(c if c.is_absolute() else gitdir / c))
    if not common.is_dir():
        return None
    return common.parent if common.name == ".git" else common


@dataclass(frozen=True)
class Repo:
    root: Path  # the main checkout (or the bare repository)
    common: Path  # the common git directory (refs, packed-refs, worktrees/)
    toplevel: Path  # the checkout cwd is in (== root unless cwd is in a linked worktree)


def _read_first_line(p: Path) -> str | None:
    try:
        with open(p, encoding="utf-8") as fh:
            return fh.readline(_GITFILE_MAX).strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def find_repo(cwd: Path) -> Repo | None:
    """The git repository `cwd` belongs to, or None (not a repository, a submodule we can't follow…).
    Walks up to the nearest `.git`, like git; never runs git."""
    start = Path(os.path.realpath(cwd))
    for d in (start, *start.parents):
        dotgit = d / ".git"
        try:
            if dotgit.is_dir():
                return Repo(root=d, common=dotgit, toplevel=d)
            if dotgit.is_file():
                gitdir = gitdir_of(dotgit)
                if gitdir is None:
                    return None
                main = worktree_repo(gitdir)
                if main is None:  # a submodule or --separate-git-dir: its own repository
                    return Repo(root=d, common=gitdir, toplevel=d)
                common = main / ".git" if (main / ".git").is_dir() else main
                return Repo(root=main, common=common, toplevel=d)
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# the repository's checkouts


@dataclass(frozen=True)
class Checkout:
    path: Path
    label: str  # the worktree's name (`.claude/worktrees/<name>`), else the directory's name


def _label(repo: Repo, path: Path) -> str:
    wt_dir = repo.root / WORKTREES_DIR
    if path.parent == wt_dir:
        return path.name
    return path.name or str(path)


def linked_worktrees(repo: Repo) -> list[Path]:
    """The linked worktrees git knows (`<common>/worktrees/*/gitdir` -> `<worktree>/.git`) plus any
    `.claude/worktrees/*` checkout, existing directories only, at most MAX_CHECKOUTS."""
    found: dict[Path, Path] = {}  # realpath -> the path as recorded (Claude keys sessions by it)
    try:
        entries = sorted(os.scandir(repo.common / "worktrees"), key=lambda e: e.name)
    except OSError:
        entries = []
    for e in entries[:MAX_CHECKOUTS * 2]:
        line = _read_first_line(Path(e.path) / "gitdir")
        if not line:
            continue
        p = Path(line)
        if not p.is_absolute():
            p = Path(e.path) / p
        wt = p.parent
        if wt.is_dir():
            found.setdefault(Path(os.path.realpath(wt)), wt)
    try:
        claude_wts = sorted(os.scandir(repo.root / WORKTREES_DIR), key=lambda e: e.name)
    except OSError:
        claude_wts = []
    for e in claude_wts[:MAX_CHECKOUTS * 2]:
        try:
            if e.is_dir(follow_symlinks=False) and os.path.exists(os.path.join(e.path, ".git")):
                found.setdefault(Path(os.path.realpath(e.path)), Path(e.path))
        except OSError:
            continue
    return list(found.values())[:MAX_CHECKOUTS]


def other_checkouts(cwd: Path) -> list[Checkout]:
    """The repository's checkouts other than the one `cwd` is in: the main checkout (when `cwd` is
    in a linked worktree) and every linked worktree. Never raises; [] outside a repository."""
    try:
        repo = find_repo(cwd)
        if repo is None:
            return []
        out: list[Checkout] = []
        here = repo.toplevel
        main = [] if repo.common == repo.root else [repo.root]  # a bare repository has no checkout
        for p in [*main, *linked_worktrees(repo)]:
            if Path(os.path.realpath(p)) != here and p.is_dir():
                out.append(Checkout(p, _label(repo, Path(os.path.realpath(p)))))
        return out
    except Exception:  # noqa: BLE001 - only ever an extra; never break the Recent list
        return []


# ---------------------------------------------------------------------------
# names


def slug(text: str | None, max_len: int = NAME_MAX) -> str:
    """A session name -> a worktree name: ASCII lower case, every run of other characters one "-",
    no "-" at either end, at most `max_len` characters. "" when nothing is left.
    "FORCE Kafka reconciliation" -> "force-kafka-reconciliation", "X (fork)" -> "x-fork"."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    if len(t) > max_len:
        t = t[:max_len].rstrip("-")
    return t


def generated_name(now: datetime | None = None, token: str | None = None) -> str:
    """The default for an unnamed session: cm-<yyyymmdd>-<4 hex>, e.g. cm-20261017-3fa2."""
    now = now or datetime.now()
    return f"cm-{now:%Y%m%d}-{token or secrets.token_hex(2)}"


def default_name(session_name: str | None, fallback: str) -> str:
    """The worktree name cm proposes: the session name as a slug, else `fallback` (generated_name)."""
    return slug(session_name) or fallback


# ---------------------------------------------------------------------------
# validity and combinations (Claude Code 2.1.281's own rules)

BRANCH_PREFIX = "worktree-"
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def valid_name(name: str) -> str | None:
    """None if claude accepts `name` for -w, else why not. Claude's rule: at most 64 characters;
    "/"-separated segments of letters, digits, ".", "_" and "-"; no "." / ".." segment and no
    ".git" (any case, trailing dots too)."""
    if not name:
        return "the worktree name is empty"
    if name.startswith("-"):  # cm's own rule: -w's value is optional, so claude would read it as an option
        return f"invalid worktree name {name!r}: it can't start with '-'"
    if len(name) > NAME_MAX:
        return f"invalid worktree name: must be {NAME_MAX} characters or fewer (got {len(name)})"
    for seg in name.split("/"):
        if not _SEGMENT_RE.match(seg):
            return (f"invalid worktree name {name!r}: only letters, digits, '.', '_', '-' "
                    "(and '/' between parts) are allowed")
        if seg in (".", ".."):
            return f"invalid worktree name {name!r}: '.' and '..' are not allowed"
        if seg.rstrip(".").lower() == ".git":
            return f"invalid worktree name {name!r}: '.git' is reserved"
    return None


def dir_name(name: str) -> str:
    return name.replace("/", "+")


def branch_name(name: str) -> str:
    return BRANCH_PREFIX + dir_name(name)


def combination_error(sel) -> str | None:
    """Why claude can't do this worktree combination (from Claude Code's code), else None.
    Needs no filesystem: used by build_plan."""
    if sel.worktree is None:
        return None
    if sel.session == "continue":
        return ("Worktree can't be combined with Continue: with -w, claude --continue looks only at the "
                "new worktree's own sessions (none yet: \"No conversation found to continue\"). "
                "Pick the session in Recent and tick Fork instead")
    if sel.tmux and sel.session == "resume" and not sel.fork:
        return ("tmux with Resume needs Fork: inside tmux claude runs without -w and would resume the "
                "session in its own directory, not in this worktree")
    return None


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def worktree_block(sel, repo: Repo | None) -> str | None:
    """Why the Worktree option can't be used here at all (shown under it), else None."""
    if repo is None:
        return "Worktree: not inside a git repository (claude -w needs one)"
    if sel.session == "continue":
        return "Worktree: not with Continue (claude would look for the last session in the new worktree)"
    return None


def tmux_block(sel) -> str | None:
    if not tmux_available():
        return "tmux: not installed (claude --tmux needs it)"
    if sel.session == "resume" and not sel.fork:
        return "tmux: only for a new session or a Fork (inside tmux, a resume would not use this worktree)"
    return None


def unsupported(sel, repo: Repo | None) -> str | None:
    """Everything that stops this worktree selection from launching (CLI)."""
    if sel.worktree is None:
        return "--tmux needs --worktree" if sel.tmux else None
    if repo is None:
        return "--worktree needs a git repository (claude -w can only create worktrees in one)"
    if sel.tmux and not tmux_available():
        return "--tmux needs tmux, which is not installed"
    return combination_error(sel)


# ---------------------------------------------------------------------------
# an existing worktree of that name

NEW, REUSE, REPLACE, REFUSE = "new", "reuse", "replace", "refuse"


@dataclass(frozen=True)
class State:
    path: Path  # <main checkout>/.claude/worktrees/<name>
    branch: str  # worktree-<name>
    kind: str  # NEW | REUSE (may be reset) | REPLACE (claude recreates something) | REFUSE (claude fails)
    message: str = ""

    @property
    def exists(self) -> bool:
        return self.kind != NEW

    @property
    def needs_confirm(self) -> bool:
        """The screen asks for a second Enter: claude may reset or replace something."""
        return self.kind in (REUSE, REPLACE)


def _ref_exists(common: Path, ref: str) -> bool:
    if (common / ref).is_file():
        return True
    try:
        with open(common / "packed-refs", encoding="utf-8", errors="replace") as fh:
            return any(line.rstrip("\n").endswith(" " + ref) for line in fh)
    except OSError:
        return False


def _checked_out_at(repo: Repo, ref: str) -> Path | None:
    """The checkout whose HEAD is `ref`, if any (main checkout, then linked worktrees)."""
    want = f"ref: {ref}"
    if repo.common != repo.root and _read_first_line(repo.common / "HEAD") == want:
        return repo.root
    try:
        entries = sorted(os.scandir(repo.common / "worktrees"), key=lambda e: e.name)
    except OSError:
        return None
    for e in entries[:MAX_CHECKOUTS * 8]:
        if _read_first_line(Path(e.path) / "HEAD") == want:
            line = _read_first_line(Path(e.path) / "gitdir")
            return Path(line).parent if line else Path(e.path)
    return None


def base_ref(repo: Repo) -> str:
    """What claude bases a new worktree on by default: origin's default branch."""
    line = _read_first_line(repo.common / "refs" / "remotes" / "origin" / "HEAD") or ""
    prefix = "ref: refs/remotes/"
    return line[len(prefix):] if line.startswith(prefix) else "origin's default branch"


def inspect(repo: Repo, name: str) -> State:
    """What claude -w <name> will find, read-only (a few stats and small reads, no git process)."""
    path = repo.root / WORKTREES_DIR / dir_name(name)
    branch = branch_name(name)
    base = base_ref(repo)
    for p in (repo.root / ".claude", repo.root / WORKTREES_DIR, path):
        if p.is_symlink():
            return State(path, branch, REFUSE, f"{tilde(p)} is a symlink: claude refuses to use it")
    if path.exists():
        dotgit = path / ".git"
        if not path.is_dir() or not dotgit.is_file():
            return State(path, branch, REFUSE, f"{tilde(path)} exists but is not a git worktree: "
                                               "claude can't reuse it; choose another name")
        gitdir = gitdir_of(dotgit)
        if gitdir is None or not gitdir.is_dir():
            return State(path, branch, REPLACE,
                         "exists, but its git metadata is gone: claude deletes the directory and creates "
                         "it again (it refuses if the branch has unpushed commits)")
        if Path(os.path.realpath(gitdir.parent)) != Path(os.path.realpath(repo.common / "worktrees")):
            return State(path, branch, REFUSE, f"{tilde(path)} belongs to another repository: claude refuses")
        return State(path, branch, REUSE,
                     f"exists: claude reuses it (git reset --hard to {base} first if it is clean "
                     f"and all its commits are already upstream)")
    ref = f"refs/heads/{branch}"
    if _ref_exists(repo.common, ref):
        where = _checked_out_at(repo, ref)
        if where is not None:
            return State(path, branch, REFUSE, f"branch {branch} is checked out in {tilde(where)}: "
                                               "claude refuses; choose another name")
        return State(path, branch, REPLACE,
                     f"branch {branch} exists without a worktree: claude recreates it from {base} "
                     f"(git worktree add -B), which resets that branch; commits only on it stay only "
                     f"in the reflog")
    return State(path, branch, NEW)
