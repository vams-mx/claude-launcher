"""Claude Code version check.

cm's isolation relies on Claude Code flags and settings whose behaviour was
verified by real launches (README "Control matrix"). A different Claude Code
version will most likely behave the same, but cm says so and points to the
smoke script that re-checks it on the user's machine.
"""

from __future__ import annotations

import re
import subprocess
from typing import Callable

# Claude Code versions the control matrix was verified on (scripts/smoke_verify.py --yes).
VERIFIED_VERSIONS: tuple[str, ...] = ("2.1.281",)
SMOKE_HINT = "python scripts/smoke_verify.py --yes (from a checkout; costs a few cents)"

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


def parse_version(text: str) -> str | None:
    m = _VERSION_RE.search(text or "")
    return m.group(1) if m else None


def installed_version(claude_bin: str, run: Callable[..., subprocess.CompletedProcess] | None = None,
                      timeout: float = 5) -> str | None:
    """`claude --version` -> '2.1.281', or None when it can't be determined."""
    run = run or subprocess.run
    try:
        p = run([claude_bin, "--version"], capture_output=True, text=True, timeout=timeout,
                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version(p.stdout) if p.returncode == 0 else None


def version_warning(version: str | None) -> str | None:
    if version is None:
        return "could not determine the Claude Code version (`claude --version` failed); is `claude` installed?"
    if version in VERIFIED_VERSIONS:
        return None
    return (f"Claude Code {version} differs from the version cm's control matrix was verified on "
            f"({', '.join(VERIFIED_VERSIONS)}); isolation is expected to hold, but verify with {SMOKE_HINT}")


def check(claude_bin: str) -> str | None:
    return version_warning(installed_version(claude_bin))
