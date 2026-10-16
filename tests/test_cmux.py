import os
from pathlib import Path

import pytest

from claude_launcher import cmux
from tests.conftest import CUA_TOKEN, WRAPPER_SNIPPET


def test_parse_template():
    tpl, args = cmux.parse_template(WRAPPER_SNIPPET)
    assert tpl.startswith('{"mcpServers":{"cmux-cua"')
    assert args == ["$client", "$cua_socket", "$default_session", "${CMUX_CLAUDE_PID:-$$}", "$state_dir"]


def test_server_config_fills_from_env(fake_home):
    env = dict(os.environ)
    st = cmux.probe(env)
    cfg = cmux.server_config(st, env, owner_pid=4242)
    assert cfg["command"] == str(fake_home.client)
    assert cfg["args"] == ["mcp", "--socket", env["CMUX_CUA_SOCKET_PATH"]]
    assert cfg["env"]["CMUX_CUA_DEFAULT_SESSION"] == "cmux-SURFACE-1"
    assert cfg["env"]["CMUX_CUA_STATE_OWNER_PID"] == "${CMUX_CLAUDE_PID:-4242}"
    assert CUA_TOKEN not in str(cfg)


def test_json_escaping_of_values(fake_home, monkeypatch):
    monkeypatch.setenv("CMUX_CUA_STATE_DIR", 'C:\\weird "dir"')
    env = dict(os.environ)
    cfg = cmux.server_config(cmux.probe(env), env, 1)
    assert cfg["env"]["CMUX_CUA_STATE_DIR"] == 'C:\\weird "dir"'


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
