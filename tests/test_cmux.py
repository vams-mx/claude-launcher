import os
import subprocess
from pathlib import Path

import pytest

from claude_launcher import cmux
from tests.conftest import CUA_TOKEN, WRAPPER_SNIPPET

REAL_RUN, REAL_POPEN = subprocess.run, subprocess.Popen  # captured before the autouse tripwire replaces them


def test_parse_template():
    tpl, args = cmux.parse_template(WRAPPER_SNIPPET)
    assert tpl.startswith('{"mcpServers":{"cmux-cua"')
    assert args == ["$client", "$cua_socket", "$default_session", "${CMUX_CLAUDE_PID:-$$}", "$state_dir"]


def test_server_config_fills_from_env(fake_home):
    env = dict(os.environ)
    st = cmux.probe(env)
    cfg = cmux.server_config(st, env)
    # /bin/sh -c TOKEN_SHIM <client> <client args...>: the client is "$0", its arguments "$@", in order
    assert cfg["command"] == "/bin/sh"
    uid = os.getuid()
    assert cfg["args"] == ["-c", cmux.TOKEN_SHIM, str(fake_home.client),
                           "mcp", "--socket", f"${{CMUX_CUA_SOCKET_PATH:-/tmp/cmux-cua-{uid}/default/cmux-cua.sock}}"]
    # environment-derived values are references with the wrapper's fallbacks, expanded by Claude at launch
    assert cfg["env"]["CMUX_CUA_DEFAULT_SESSION"] == "cmux-${CMUX_SURFACE_ID:-cm}"
    assert cfg["env"]["CMUX_CUA_STATE_OWNER_PID"] == "${CMUX_CLAUDE_PID:-}"
    assert cfg["env"]["CMUX_CUA_STATE_DIR"] == (
        f"${{CMUX_CUA_STATE_DIR:-{fake_home.home}/Library/Application Support/cmux/cmux-cua/runtime/default/state}}")
    assert CUA_TOKEN not in str(cfg) and "CMUX_CUA_SOCKET_AUTH_TOKEN" not in cfg["env"]


def test_token_shim_shape():
    shim = cmux.TOKEN_SHIM
    assert "${" not in shim  # Claude Code only expands braced references: the script reaches sh verbatim
    assert shim.endswith('exec "$0" "$@"')  # client and its args are argv elements, never spliced into the script
    assert '$(cat "$CMUX_CUA_AUTH_TOKEN_FILE" | tr -d' in shim and "export CMUX_CUA_SOCKET_AUTH_TOKEN" in shim
    wrapped = cmux.wrap_with_token_shim({"command": "/x y/cmux-cua", "args": ["a", "b c"], "env": {"K": "v"}})
    assert wrapped == {"command": "/bin/sh", "args": ["-c", shim, "/x y/cmux-cua", "a", "b c"], "env": {"K": "v"}}


def _run_shim(tmp_path, env):
    """Run the shim for real with /bin/sh around a stub client; return what the client saw."""
    out = tmp_path / "seen"
    out.unlink(missing_ok=True)
    client = tmp_path / "dir with 'q\" space" / "client"
    if not client.exists():
        client.parent.mkdir()
        client.write_text(f'#!/bin/sh\nprintf "%s|%s|%s" "${{CMUX_CUA_SOCKET_AUTH_TOKEN-unset}}" "$1" "$2" > "{out}"\n')
        client.chmod(0o755)
    entry = cmux.wrap_with_token_shim({"command": str(client), "args": ["mcp", "two words"]})
    assert REAL_RUN([entry["command"], *entry["args"]], env={"PATH": "/usr/bin:/bin", **env}, timeout=10).returncode == 0
    return out.read_text()


def test_token_shim_file_wins_like_resolve_token(tmp_path, monkeypatch):
    """Same order as resolve_token and cmux's wrapper: the token file first, the inherited variable second."""
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)
    tok = tmp_path / "tok"
    tok.write_text("new-from-file\n")
    f = {"CMUX_CUA_AUTH_TOKEN_FILE": str(tok)}
    # daemon restarted and rewrote the file; a stale exported value must not win
    assert _run_shim(tmp_path, {**f, "CMUX_CUA_SOCKET_AUTH_TOKEN": "old-exported"}) == "new-from-file|mcp|two words"
    assert _run_shim(tmp_path, f) == "new-from-file|mcp|two words"  # restore: nothing exported
    # no file (unset, or pointing nowhere): the inherited variable is the fallback
    assert _run_shim(tmp_path, {"CMUX_CUA_SOCKET_AUTH_TOKEN": "from-env"}) == "from-env|mcp|two words"
    gone = {"CMUX_CUA_AUTH_TOKEN_FILE": str(tmp_path / "missing"), "CMUX_CUA_SOCKET_AUTH_TOKEN": "from-env"}
    assert _run_shim(tmp_path, gone) == "from-env|mcp|two words"
    tok.write_bytes(b"crlf-token\r\n")  # CRLF file: no trailing \r, like cm's rstrip("\r\n")
    assert _run_shim(tmp_path, f) == "crlf-token|mcp|two words"
    assert cmux.resolve_token(f) == ("crlf-token", "file")
    tok.write_text("")  # empty file: fall back to the variable
    assert _run_shim(tmp_path, {**f, "CMUX_CUA_SOCKET_AUTH_TOKEN": "from-env"}) == "from-env|mcp|two words"
    assert _run_shim(tmp_path, {}) == "unset|mcp|two words"  # neither: still starts the client


def test_cursor_session_with_and_without_surface(fake_home, monkeypatch):
    env = dict(os.environ)
    assert cmux.server_config(cmux.probe(env), env)["env"]["CMUX_CUA_DEFAULT_SESSION"] == "cmux-${CMUX_SURFACE_ID:-cm}"
    assert not cmux.needs_session_var(env)
    monkeypatch.delenv("CMUX_SURFACE_ID")
    monkeypatch.setenv("CMUX_SOCKET_PATH", "/tmp/cmux.sock")  # in_cmux() true without a surface id
    env = dict(os.environ)
    assert cmux.in_cmux(env) and cmux.needs_session_var(env)
    assert cmux.server_config(cmux.probe(env), env)["env"]["CMUX_CUA_DEFAULT_SESSION"] == "cmux-${CM_CUA_SESSION:-cm}"


def test_unbuildable_template_makes_unavailable(fake_home):
    fake_home.wrapper.write_text(WRAPPER_SNIPPET.replace('"command":"%s"', '"command":["%s"]'))
    st = cmux.probe(dict(os.environ))
    assert not st.available and "cannot build the cmux-cua entry" in st.reason


def test_server_config_is_launch_independent(fake_home):
    """Nothing per launch (PIDs, surface ids, socket or state paths from the environment) is baked in."""
    env = dict(os.environ)
    first = cmux.server_config(cmux.probe(env), env)
    other = {**env, "CMUX_SURFACE_ID": "SURFACE-2", "CMUX_CUA_SOCKET_PATH": "/var/x/other.sock",
             "CMUX_CUA_STATE_DIR": "/elsewhere", "CMUX_CLAUDE_PID": "777"}
    assert cmux.server_config(cmux.probe(other), other) == first
    blob = str(first)
    assert str(os.getpid()) not in blob and "SURFACE-1" not in blob and env["CMUX_CUA_SOCKET_PATH"] not in blob


def test_expanded_values_match_the_wrapper(fake_home):
    """What Claude Code expands the references to equals what the wrapper computes itself."""
    from claude_launcher.health import expand_refs

    env = {**os.environ, "CMUX_CLAUDE_PID": "4242"}
    cfg = cmux.server_config(cmux.probe(env), env)
    assert expand_refs(cfg["args"][-1], env) == env["CMUX_CUA_SOCKET_PATH"]
    assert expand_refs(cfg["env"]["CMUX_CUA_DEFAULT_SESSION"], env) == "cmux-SURFACE-1"
    assert expand_refs(cfg["env"]["CMUX_CUA_STATE_OWNER_PID"], env) == "4242"
    assert expand_refs(cfg["env"]["CMUX_CUA_STATE_DIR"], env) == env["CMUX_CUA_STATE_DIR"]
    assert expand_refs(cfg["env"]["CMUX_CUA_STATE_OWNER_PID"], {}) == ""


def test_json_escaping_of_values(fake_home, monkeypatch):
    weird = fake_home.tmp / 'we"ird\\home'
    monkeypatch.setenv("HOME", str(weird))
    env = dict(os.environ)
    cfg = cmux.server_config(cmux.probe(env), env)
    assert cfg["env"]["CMUX_CUA_STATE_DIR"].startswith(f"${{CMUX_CUA_STATE_DIR:-{weird}/Library/")


def test_unknown_template_var_makes_unavailable(fake_home):
    fake_home.wrapper.write_text(WRAPPER_SNIPPET.replace("$state_dir", "$mystery"))
    st = cmux.probe(dict(os.environ))
    assert not st.available and "unknown" in st.reason


def test_missing_wrapper(fake_home, monkeypatch):
    monkeypatch.setenv("CM_CMUX_WRAPPER", str(fake_home.tmp / "nope"))
    monkeypatch.setattr(cmux, "DEFAULT_APP_BIN", fake_home.tmp / "none")
    assert not cmux.probe(dict(os.environ)).available


def test_token_is_read_like_the_workaround(fake_home, monkeypatch):
    # export CMUX_CUA_SOCKET_AUTH_TOKEN="$(cat "$CMUX_CUA_AUTH_TOKEN_FILE")"
    env = dict(os.environ)
    env["CMUX_CUA_SOCKET_AUTH_TOKEN"] = "stale"
    assert cmux.resolve_token(env) == (CUA_TOKEN, "file")  # the file wins over a stale variable
    fake_home.token.chmod(0o644)  # any mode; the wrapper would ignore this file
    assert cmux.resolve_token(env) == (CUA_TOKEN, "file")
    link = fake_home.tmp / "link"
    link.symlink_to(fake_home.token)  # symlinks are followed, like cat
    assert cmux.resolve_token({**env, "CMUX_CUA_AUTH_TOKEN_FILE": str(link)}) == (CUA_TOKEN, "file")
    fake_home.token.write_text(CUA_TOKEN + "\n\n")
    assert cmux.resolve_token(env) == (CUA_TOKEN, "file")  # trailing newlines stripped, like $(…)
    fake_home.token.chmod(0o000)  # unreadable: fall back to the variable
    assert cmux.resolve_token(env) == ("stale", "env")
    assert cmux.resolve_token({k: v for k, v in env.items() if k != "CMUX_CUA_SOCKET_AUTH_TOKEN"}) == (None, None)
    fake_home.token.chmod(0o600)


def test_not_in_cmux_note(fake_home, monkeypatch):
    monkeypatch.delenv("CMUX_SURFACE_ID")
    st = cmux.probe(dict(os.environ))
    assert st.available and "not running inside a cmux terminal" in st.reason


REAL = Path("/Applications/cmux.app/Contents/Resources/bin/cmux-claude-wrapper")


@pytest.mark.skipif(not REAL.is_file(), reason="cmux not installed")
def test_installed_wrapper_template_still_parses():
    """Read-only canary: fails if a cmux update changes the template format."""
    parsed = cmux.parse_template(REAL.read_text(errors="replace"))
    assert parsed, "cmux-claude-wrapper template not found"
    assert {cmux._arg_key(a) for a in parsed[1]} <= {"client", "cua_socket", "default_session",
                                                      "CMUX_CLAUDE_PID", "state_dir"}


def test_token_file_must_be_a_regular_file_given_by_absolute_path(fake_home, tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)  # opening this for reading would block forever
    env = {"CMUX_CUA_AUTH_TOKEN_FILE": str(fifo), "CMUX_CUA_SOCKET_AUTH_TOKEN": "from-env"}
    assert cmux.resolve_token(env) == ("from-env", "env")
    assert cmux.resolve_token({**env, "CMUX_CUA_AUTH_TOKEN_FILE": "/dev/null"}) == ("from-env", "env")
    assert cmux.resolve_token({**env, "CMUX_CUA_AUTH_TOKEN_FILE": "relative/auth-token"}) == ("from-env", "env")
    big = tmp_path / "big"
    big.write_text("x" * 100_000)
    token, src = cmux.resolve_token({**env, "CMUX_CUA_AUTH_TOKEN_FILE": str(big)})
    assert src == "file" and len(token) == 4096  # bounded read
