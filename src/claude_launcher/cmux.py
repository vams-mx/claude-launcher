"""cmux computer-use (cmux-cua) support.

The cmux terminal wraps `claude` with `cmux-claude-wrapper`. When a launch does
NOT pass --strict-mcp-config, the wrapper prepends
`--mcp-config={"mcpServers":{"cmux-cua":{...}}}` (built by its
`cmux_computer_use_config` function) and exports CMUX_CUA_SOCKET_AUTH_TOKEN.
With --strict-mcp-config it deliberately injects nothing.

cm always controls the MCP set itself, so it sets CMUX_COMPUTER_USE_MCP_DISABLED=1
(the wrapper's documented opt-out) and, when the user ticks cmux, adds the same
server entry to its own session MCP file. The entry is *not* hard-coded here:
it is read from the installed wrapper's printf template at runtime so it follows
cmux updates, and cmux is reported unavailable if the template can't be parsed.
When cmux isn't installed at all (e.g. on Linux) the cmux group is hidden.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .paths import home

SERVER_NAME = "cmux-cua"
DEFAULT_APP_BIN = Path("/Applications/cmux.app/Contents/Resources/bin")

_TEMPLATE_RE = re.compile(
    r"printf '(\{\"mcpServers\":\{\"cmux-cua\".*?)'\s*\\\n"  # the JSON template
    r"((?:[ \t]*\"\$\(cmux_computer_use_json_escape \"[^\"]+\"\)\"[ \t]*\\?\n?)+)",
)
_ARG_RE = re.compile(r'cmux_computer_use_json_escape "([^"]+)"')


@dataclass
class CmuxStatus:
    available: bool
    reason: str = ""
    in_cmux: bool = False
    wrapper: Path | None = None
    client: Path | None = None
    token_source: str | None = None  # "file" | "env" | None
    template: str | None = None
    arg_names: list[str] = field(default_factory=list)
    installed: bool = False  # cmux's wrapper exists; False -> cm hides the cmux group entirely


def in_cmux(env: dict[str, str]) -> bool:
    return bool(env.get("CMUX_SURFACE_ID") or env.get("CMUX_SOCKET_PATH"))


def find_wrapper(env: dict[str, str]) -> Path | None:
    candidates: list[Path] = []
    if env.get("CM_CMUX_WRAPPER"):
        candidates.append(Path(env["CM_CMUX_WRAPPER"]))
    if env.get("CMUX_BUNDLED_CLI_PATH"):
        candidates.append(Path(env["CMUX_BUNDLED_CLI_PATH"]).parent / "cmux-claude-wrapper")
    candidates.append(DEFAULT_APP_BIN / "cmux-claude-wrapper")
    for c in candidates:
        if c.is_file():
            return c
    return None


def parse_template(text: str) -> tuple[str, list[str]] | None:
    """Return (printf template, [shell arg expressions]) from the wrapper."""
    m = _TEMPLATE_RE.search(text)
    if not m:
        return None
    template, block = m.group(1), m.group(2)
    args = _ARG_RE.findall(block)
    if template.count("%s") != len(args) or not args:
        return None
    return template, args


def _arg_key(expr: str) -> str:
    """'$client' -> 'client'; '${CMUX_CLAUDE_PID:-$$}' -> 'CMUX_CLAUDE_PID'."""
    e = expr.strip()
    if e.startswith("${") and e.endswith("}"):
        e = e[2:-1].split(":-", 1)[0]
    return e.lstrip("$")


def _scope(env: dict[str, str]) -> str:
    raw = env.get("CMUX_CUA_RUNTIME_SCOPE") or env.get("CMUX_TAG") or "default"
    s = re.sub(r"[^A-Za-z0-9_.-]", "-", raw)
    s = re.sub(r"^[.-]+", "", s)
    s = re.sub(r"[.-]+$", "", s)[:64]
    return s or "default"


def _values(env: dict[str, str], client: Path, owner_pid: int) -> dict[str, str]:
    scope = _scope(env)
    uid = os.getuid()
    # The wrapper uses the PID of the process that *becomes* Claude (it exports
    # CMUX_CLAUDE_PID=$$ and then execs claude); cmux's app ties approvals to it.
    # cm can't know Claude's PID before spawning it, so it writes a reference that
    # Claude Code expands at launch from its own environment (falling back to
    # cm's PID when claude isn't started through cmux's wrapper).
    claude_pid = "${CMUX_CLAUDE_PID:-%d}" % owner_pid
    return {
        "client": str(client),
        "cua_socket": env.get("CMUX_CUA_SOCKET_PATH") or f"/tmp/cmux-cua-{uid}/{scope}/cmux-cua.sock",
        "default_session": f"cmux-{env.get('CMUX_SURFACE_ID') or claude_pid}",
        "CMUX_CLAUDE_PID": claude_pid,
        "state_dir": env.get("CMUX_CUA_STATE_DIR")
        or str(home() / "Library/Application Support/cmux/cmux-cua/runtime" / scope / "state"),
    }


def probe(env: dict[str, str]) -> CmuxStatus:
    """Read-only inspection: is cmux computer-use selectable here?"""
    st = CmuxStatus(available=False, in_cmux=in_cmux(env))
    wrapper = find_wrapper(env)
    if not wrapper:
        st.reason = "cmux-claude-wrapper not found (cmux not installed?)"
        return st
    st.wrapper = wrapper
    st.installed = True
    client = wrapper.parent / "cmux-cua"
    if not os.access(client, os.X_OK):
        st.reason = f"bundled cmux-cua client missing next to {wrapper.name}"
        return st
    st.client = client
    try:
        parsed = parse_template(wrapper.read_text(errors="replace"))
    except OSError as exc:
        st.reason = f"cannot read wrapper: {exc}"
        return st
    if not parsed:
        st.reason = "could not find the cmux-cua MCP template in the wrapper (cmux changed?)"
        return st
    st.template, st.arg_names = parsed
    unknown = [a for a in st.arg_names if _arg_key(a) not in _values(env, client, 0)]
    if unknown:
        st.reason = f"wrapper template uses unknown values {unknown}"
        return st
    _, st.token_source = resolve_token(env)
    st.available = True
    if not st.in_cmux:
        st.reason = "not running inside a cmux terminal; computer-use calls will fail"
    elif not st.token_source:
        st.reason = "no cmux-cua auth token (CMUX_CUA_AUTH_TOKEN_FILE missing); tools load but calls may be refused"
    return st


def server_config(status: CmuxStatus, env: dict[str, str], owner_pid: int) -> dict:
    """Fill the wrapper's template -> the `cmux-cua` server entry (no secrets)."""
    if not (status.available and status.template and status.client):
        raise ValueError(status.reason or "cmux computer-use unavailable")
    vals = _values(env, status.client, owner_pid)
    parts = status.template.split("%s")
    out = [parts[0]]
    for expr, tail in zip(status.arg_names, parts[1:]):
        out.append(json.dumps(vals[_arg_key(expr)])[1:-1])  # JSON-escape like the wrapper
        out.append(tail)
    doc = json.loads("".join(out))
    return doc["mcpServers"][SERVER_NAME]


_TOKEN_MAX = 4096  # far above a real token (64 chars); bounds the read


def resolve_token(env: dict[str, str]) -> tuple[str | None, str | None]:
    """The cmux-cua socket token, exactly as the known workaround gets it:
    `export CMUX_CUA_SOCKET_AUTH_TOKEN="$(cat "$CMUX_CUA_AUTH_TOKEN_FILE")"`.

    cmux hands the token over as a file (CMUX_CUA_AUTH_TOKEN_FILE), but the
    cmux-cua 0.7.1 proxy only reads CMUX_CUA_SOCKET_AUTH_TOKEN; without it its
    requests are unauthenticated, the daemon answers "Unauthorized daemon
    client", and the proxy reports the runtime as "not listening". So cm reads
    the file fresh on every launch (following symlinks, trailing newlines
    stripped like `$(cat …)`) and only falls back to an inherited variable when
    the file can't be read. The token only ever goes into Claude's
    environment, never into argv, files or output."""
    path = env.get("CMUX_CUA_AUTH_TOKEN_FILE")
    if path and path.startswith("/"):
        try:
            # a regular file only (symlinks followed): never block on a FIFO or a device
            if stat.S_ISREG(os.stat(path).st_mode):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    token = fh.read(_TOKEN_MAX).rstrip("\r\n")
                if token:
                    return token, "file"
        except OSError:
            pass
    token = env.get("CMUX_CUA_SOCKET_AUTH_TOKEN")
    if token:
        return token, "env"
    return None, None
