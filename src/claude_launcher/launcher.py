"""Launch Claude safely.

* argv list, never a shell.
* Session files live in ~/.claude-launcher/sessions/<content hash>/ (0700,
  files 0600, written atomically) and are kept after Claude exits, so a cmux
  restore (which replays the original argv) still finds them. The same
  selection reuses the same files; directories unused for
  `session_retention_days` (default 60) are removed on a later launch. If they
  can't be written there, cm falls back to a private temp dir that is deleted
  when Claude exits (with a warning: such a session can't be restored by cmux).
* cm waits as the parent: Ctrl+C / Ctrl+\\ reach Claude through the terminal's
  foreground process group while cm ignores them, SIGTERM/SIGHUP are
  forwarded, and Claude's exit status is returned (128+N for a signal).
* stdin/stdout/stderr are inherited, so the TTY stays interactive.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator

from . import config as config_mod
from . import sessions
from .catalog import Catalog
from .paths import tilde
from .plan import LaunchPlan, PlanError, Selection, build_plan


def write_private(path: Path, doc: Any) -> None:
    """Create a new 0600 file (O_EXCL). For throwaway temp dirs (measurement probes, smoke script)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(sessions.doc_bytes(doc))


def write_session_files(plan: LaunchPlan) -> None:
    """Write a plan's files into the (temporary) directory it was built for."""
    write_private(plan.mcp_path, plan.mcp_doc)
    write_private(plan.settings_path, plan.settings_doc)


def persist_session_files(plan: LaunchPlan) -> Path:
    """Create or reuse the plan's persistent session directory (see sessions.py)."""
    d = sessions.ensure(plan.mcp_doc, plan.settings_doc)
    if d != plan.mcp_path.parent or d != plan.settings_path.parent:  # build_plan computed the same path
        raise PlanError(f"session directory mismatch: {d} vs {plan.mcp_path.parent}")
    return d


@contextlib.contextmanager
def temp_session_dir() -> Iterator[Path]:
    """The fallback: a private mkdtemp directory (0700), removed when Claude exits."""
    d = Path(tempfile.mkdtemp(prefix="cm-session-"))
    os.chmod(d, 0o700)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def cleanup_sessions(keep: Path | None = None) -> list[str]:
    """Best effort: remove session directories unused for longer than the retention. Never raises."""
    try:
        warnings: list[str] = []
        days = sessions.retention_days(config_mod.load_config(warnings), warnings)
        for w in warnings:
            print(f"cm: {w}", file=sys.stderr)
        return sessions.cleanup(days, keep=keep)
    except Exception:  # noqa: BLE001 - housekeeping must never fail a launch
        return []


@contextlib.contextmanager
def _child_signals(proc: subprocess.Popen) -> Iterator[None]:
    old: dict[int, Any] = {}

    def forward(signum: int, _frame: Any) -> None:
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signum)

    for sig in (signal.SIGINT, signal.SIGQUIT):
        old[sig] = signal.signal(sig, signal.SIG_IGN)  # the TTY delivers these to claude directly
    for sig in (signal.SIGTERM, signal.SIGHUP):
        old[sig] = signal.signal(sig, forward)
    try:
        yield
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)


def exit_code(rc: int) -> int:
    return 128 + (-rc) if rc < 0 else rc


def run_plan(plan: LaunchPlan, popen: Callable[..., subprocess.Popen] | None = None,
             cwd: Path | None = None, on_started: Callable[[], None] | None = None) -> int:
    """Start claude and wait for it. `on_started` runs once claude has actually been started
    (never when it couldn't be); an exception from it is ignored."""
    popen = popen or subprocess.Popen
    env = {k: v for k, v in os.environ.items() if k not in plan.env_unset}
    env.update(plan.env_overrides)
    try:
        proc = popen(plan.argv, env=env, cwd=cwd)
    except FileNotFoundError:
        print(f"cm: cannot run {plan.argv[0]!r}: not found (use --claude-bin)", file=sys.stderr)
        return 127
    except PermissionError:
        print(f"cm: cannot run {plan.argv[0]!r}: permission denied", file=sys.stderr)
        return 126
    if on_started is not None:
        with contextlib.suppress(Exception):  # bookkeeping must never fail a launch
            on_started()
    with _child_signals(proc):
        while True:
            try:
                return exit_code(proc.wait())
            except InterruptedError:  # pragma: no cover - PEP 475 retries, belt and braces
                continue


def launch_dir(cat: Catalog, sel: Selection) -> Path:
    """Where claude starts: the catalog's directory, or the checkout a resumed worktree session
    belongs to (claude looks a session up by its directory)."""
    return Path(sel.launch_dir) if sel.launch_dir else cat.cwd


def launch(
    cat: Catalog,
    sel: Selection,
    *,
    claude_bin: str,
    popen: Callable[..., subprocess.Popen] | None = None,
    on_started: Callable[[], None] | None = None,
) -> int:
    plan = build_plan(cat, sel, claude_bin=claude_bin)
    try:
        d = persist_session_files(plan)
    except OSError as exc:
        reason = str(exc) if isinstance(exc, sessions.NotOwnedError) else \
            f"cannot write {tilde(plan.mcp_path.parent)}: {exc.strerror or exc}"
        print(f"cm: warning: session files won't survive a cmux restore: {reason}", file=sys.stderr)
        with temp_session_dir() as tmp:
            plan = build_plan(cat, sel, claude_bin=claude_bin, session_dir=tmp)
            write_session_files(plan)
            return run_plan(plan, popen, cwd=launch_dir(cat, sel), on_started=on_started)
    cleanup_sessions(keep=d)
    # the directory the catalog (and deny list) was built for, or the worktree of a resumed session
    return run_plan(plan, popen, cwd=launch_dir(cat, sel), on_started=on_started)
