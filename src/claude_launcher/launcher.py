"""Launch Claude safely.

* argv list, never a shell.
* Session files live in a private mkdtemp dir (0700), each file 0600, created
  with O_EXCL; they exist for Claude's whole runtime and are removed after.
* cm waits as the parent: Ctrl+C / Ctrl+\\ reach Claude through the terminal's
  foreground process group while cm ignores them, SIGTERM/SIGHUP are
  forwarded, and Claude's exit status is returned (128+N for a signal).
* stdin/stdout/stderr are inherited, so the TTY stays interactive.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator

from .catalog import Catalog
from .plan import LaunchPlan, Selection, build_plan


def write_private(path: Path, doc: Any) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, json.dumps(doc, indent=2).encode())
    finally:
        os.close(fd)


def write_session_files(plan: LaunchPlan) -> None:
    write_private(plan.mcp_path, plan.mcp_doc)
    write_private(plan.settings_path, plan.settings_doc)


@contextlib.contextmanager
def session_dir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="cm-session-"))
    os.chmod(d, 0o700)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


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
             cwd: Path | None = None) -> int:
    popen = popen or subprocess.Popen
    env = dict(os.environ)
    env.update(plan.env_overrides)
    try:
        proc = popen(plan.argv, env=env, cwd=cwd)
    except FileNotFoundError:
        print(f"cm: cannot run {plan.argv[0]!r}: not found (use --claude-bin)", file=sys.stderr)
        return 127
    except PermissionError:
        print(f"cm: cannot run {plan.argv[0]!r}: permission denied", file=sys.stderr)
        return 126
    with _child_signals(proc):
        while True:
            try:
                return exit_code(proc.wait())
            except InterruptedError:  # pragma: no cover - PEP 475 retries, belt and braces
                continue


def launch(
    cat: Catalog,
    sel: Selection,
    *,
    claude_bin: str,
    popen: Callable[..., subprocess.Popen] | None = None,
) -> int:
    with session_dir() as d:
        plan = build_plan(cat, sel, claude_bin=claude_bin, session_dir=d)
        write_session_files(plan)
        return run_plan(plan, popen, cwd=cat.cwd)  # the directory the catalog (and deny list) was built for
