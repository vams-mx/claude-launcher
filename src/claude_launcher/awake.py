"""Keep the Mac awake while claude runs (macOS `caffeinate`), chosen per launch.

    off   nothing (the default)
    ims   caffeinate -ims -w <claude pid>   no idle sleep (-i), no disk idle sleep (-m),
                                           no system sleep (-s, AC power only)
    dims  caffeinate -dims -w <claude pid>  the same, and the display stays on (-d)

The assertion lives exactly as long as claude: caffeinate is started right after
claude (argv only, no shell, in its own session with no terminal I/O, so Ctrl+C
in the terminal never reaches it) and `-w` makes it exit by itself when claude
exits. cm also stops it on every exit path of its own (Keeper.stop in
launcher.run_plan's `finally`), so none is left behind. Power settings (pmset)
are never changed; `pmset -g batt` is only read, for the preview's battery note.

macOS only: elsewhere the option isn't offered, and a value stored by a Mac in
a profile or the last selection is kept on disk but ignored.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from typing import Any, Callable

MODES = ("off", "ims", "dims")
LABELS = {"off": "Off", "ims": "System (-ims)", "dims": "System + display (-dims)"}
CAFFEINATE = "/usr/bin/caffeinate"
PMSET = "/usr/bin/pmset"
PLATFORM = sys.platform  # tests patch this
BATTERY_NOTE = "on battery: keeps the Mac awake and drains the battery faster; -s only applies on AC power"
_STOP_TIMEOUT = 2.0


def supported() -> bool:
    """caffeinate exists only on macOS."""
    return PLATFORM == "darwin"


def normalize(value: Any) -> tuple[str, str | None]:
    """A stored value -> (mode, note). Anything unknown (a hand-edited file) becomes "off"."""
    if isinstance(value, str) and value in MODES:
        return value, None
    return "off", f"keep awake {value!r} is unknown; using Off"


def caffeinate_argv(mode: str, pid: int | str, binary: str = CAFFEINATE) -> list[str]:
    if mode not in MODES or mode == "off":
        raise ValueError(f"no caffeinate for keep-awake mode {mode!r}")
    return [binary, f"-{mode}", "-w", str(pid)]


def describe(mode: str) -> str:
    """The preview's line: `caffeinate -ims -w <claude pid> (ends with claude)`."""
    return " ".join(caffeinate_argv(mode, "<claude pid>", binary="caffeinate")) + " (ends with claude)"


# ---------------------------------------------------------------------------
# power source (read-only)


def parse_power_source(text: str) -> str | None:
    """`pmset -g batt` output -> "battery", "ac" or None (unknown). The first line reads
    "Now drawing from 'Battery Power'" or "Now drawing from 'AC Power'"."""
    for line in text.splitlines():
        if "drawing from" in line:
            if "'Battery Power'" in line:
                return "battery"
            if "'AC Power'" in line:
                return "ac"
            return None
    return None


def _read_power_source(run: Callable[..., Any] | None = None, timeout: float = 2.0) -> str | None:
    """Run the read-only `pmset -g batt` (never any argument that changes a setting).
    None when unknown, off macOS, or on any error."""
    if not supported():
        return None
    run = run or subprocess.run
    try:
        res = run([PMSET, "-g", "batt"], capture_output=True, text=True, timeout=timeout,
                  stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if getattr(res, "returncode", 1) != 0:
        return None
    return parse_power_source(res.stdout or "")


power_source = _read_power_source  # callers go through the module attribute (tests replace it)


# ---------------------------------------------------------------------------
# the caffeinate child


class Keeper:
    """Owns the caffeinate process of one launch. start() and stop() never raise; a problem
    becomes `warning` (printed by the launcher after claude exits)."""

    def __init__(self, mode: str, popen: Callable[..., Any] | None = None) -> None:
        self.mode = mode if mode in MODES else "off"
        self.popen = popen
        self.proc: Any = None
        self.warning: str | None = None

    @property
    def active(self) -> bool:
        return self.mode != "off" and supported()

    def start(self, pid: int) -> None:
        if not self.active or self.proc is not None:
            return
        argv = caffeinate_argv(self.mode, pid)
        popen = self.popen or subprocess.Popen
        try:
            self.proc = popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        except FileNotFoundError:
            self.warning = f"keep awake: {CAFFEINATE} not found; the Mac could sleep during this session"
        except Exception as exc:  # noqa: BLE001 - keeping awake must never fail a launch
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
            self.warning = f"keep awake: could not start caffeinate ({reason}); the Mac could sleep during this session"

    def stop(self) -> None:
        """Terminate caffeinate if it is still running and reap it (idempotent)."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    proc.wait(timeout=_STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    proc.wait(timeout=_STOP_TIMEOUT)
        except Exception:  # noqa: BLE001 - cleanup must never mask claude's exit code
            pass
