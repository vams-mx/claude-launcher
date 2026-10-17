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
SESSION_VAR = "CM_CUA_SESSION"  # cm's per-launch cursor-session fallback when CMUX_SURFACE_ID is unset
# Runs the client with the socket token in its environment, read when the server starts, so a
# cmux restore (which replays argv but not cm's environment, and under --strict-mcp-config gets no
# token from the wrapper either) still authenticates. Same order as resolve_token and cmux's wrapper:
# the token FILE wins, read fresh every time the server starts (so a reconnect via /mcp after the
# daemon rewrote the file picks up the new token, never a stale exported one); an inherited
# CMUX_CUA_SOCKET_AUTH_TOKEN is only the fallback when the file is missing or empty. Like
# `$(cat …)` trailing newlines go; `tr -d '\r'` also drops CR (cm strips \r\n). No secret is ever
# written; "$0" is the client, "$@" its arguments, both separate argv elements (never interpolated
# into the script). The script contains no "${": Claude Code only expands braced references, so it
# reaches /bin/sh verbatim (verified on 2.1.281, see README).
TOKEN_SHIM = ('t=; [ -f "$CMUX_CUA_AUTH_TOKEN_FILE" ] && t=$(cat "$CMUX_CUA_AUTH_TOKEN_FILE" | tr -d \'\\r\'); '
              '[ -n "$t" ] && { CMUX_CUA_SOCKET_AUTH_TOKEN=$t; export CMUX_CUA_SOCKET_AUTH_TOKEN; }; '
              'exec "$0" "$@"')
SHELL = "/bin/sh"
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


def _values(env: dict[str, str], client: Path) -> dict[str, str]:
    """The wrapper's template values, as written into cm's session mcp.json.

    The file is kept and reused across launches (and replayed by a cmux
    restore), so nothing launch-specific may be baked into it. Values the
    wrapper takes from the environment are therefore written as `${VAR:-default}`
    references, which Claude Code expands at launch from its own environment,
    exactly when the wrapper would compute them: the defaults are the wrapper's
    own fallbacks, computed here.

    The owner PID: the wrapper uses the PID of the process that *becomes* Claude
    (it exports CMUX_CLAUDE_PID=$$ and then execs claude); cmux's app ties
    approvals to it. cm can't know Claude's PID before spawning it, so the file
    says `${CMUX_CLAUDE_PID:-}` and cm exports CMUX_CLAUDE_PID=<cm's PID> to
    Claude (plan.build_plan): through the wrapper that is replaced by Claude's
    own PID, and when claude is started directly, cm (Claude's parent, alive
    exactly as long as Claude) is the owner. If nobody sets it (claude replayed
    outside both cm and the wrapper, where computer use can't work anyway) the
    value is empty rather than a stale PID.

    The cursor session: the wrapper uses `cmux-${CMUX_SURFACE_ID:-$$}`. `${}`
    doesn't nest, so this is decided here: with a surface id, the file says
    `cmux-${CMUX_SURFACE_ID:-cm}`; without one, `cmux-${CM_CUA_SESSION:-cm}` and
    cm exports CM_CUA_SESSION=<cm's PID> (see `needs_session_var`), so two such
    sessions never share one cursor session while the file stays stable."""
    scope = _scope(env)
    uid = os.getuid()
    socket_default = f"/tmp/cmux-cua-{uid}/{scope}/cmux-cua.sock"
    state_default = str(home() / "Library/Application Support/cmux/cmux-cua/runtime" / scope / "state")
    return {
        "client": str(client),
        "cua_socket": f"${{CMUX_CUA_SOCKET_PATH:-{socket_default}}}",
        "default_session": "cmux-${%s:-cm}" % ("CMUX_SURFACE_ID" if env.get("CMUX_SURFACE_ID") else SESSION_VAR),
        "CMUX_CLAUDE_PID": "${CMUX_CLAUDE_PID:-}",
        "state_dir": f"${{CMUX_CUA_STATE_DIR:-{state_default}}}",
    }


def needs_session_var(env: dict[str, str]) -> bool:
    """True when the entry refers to CM_CUA_SESSION, which cm must then export."""
    return not env.get("CMUX_SURFACE_ID")


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
    unknown = [a for a in st.arg_names if _arg_key(a) not in _values(env, client)]
    if unknown:
        st.reason = f"wrapper template uses unknown values {unknown}"
        return st
    _, st.token_source = resolve_token(env)
    st.available = True
    try:
        server_config(st, env)  # the entry must be buildable from this template, or cmux is unavailable
    except (ValueError, KeyError, TypeError) as exc:
        st.available = False
        st.reason = f"cannot build the cmux-cua entry from the wrapper template (cmux changed?): {exc}"
        return st
    if not st.in_cmux:
        st.reason = "not running inside a cmux terminal; computer-use calls will fail"
    elif not st.token_source:
        st.reason = "no cmux-cua auth token (CMUX_CUA_AUTH_TOKEN_FILE missing); tools load but calls may be refused"
    return st


def server_config(status: CmuxStatus, env: dict[str, str]) -> dict:
    """Fill the wrapper's template -> the `cmux-cua` server entry (no secrets, nothing launch-specific)."""
    if not (status.available and status.template and status.client):
        raise ValueError(status.reason or "cmux computer-use unavailable")
    vals = _values(env, status.client)
    parts = status.template.split("%s")
    out = [parts[0]]
    for expr, tail in zip(status.arg_names, parts[1:]):
        out.append(json.dumps(vals[_arg_key(expr)])[1:-1])  # JSON-escape like the wrapper
        out.append(tail)
    doc = json.loads("".join(out))
    entry = doc["mcpServers"][SERVER_NAME]
    return wrap_with_token_shim(entry)


def wrap_with_token_shim(entry: dict) -> dict:
    """{"command": C, "args": A} -> {"command": "/bin/sh", "args": ["-c", TOKEN_SHIM, C, *A]}."""
    assert "${" not in TOKEN_SHIM
    if not isinstance(entry.get("command"), str) or not isinstance(entry.get("args", []), list):
        raise TypeError("unexpected cmux-cua entry shape (cmux changed?)")
    out = dict(entry)
    out["command"] = SHELL
    out["args"] = ["-c", TOKEN_SHIM, entry["command"], *entry.get("args", [])]
    return out


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
