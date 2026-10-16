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
    assert cfg["env"]["CMUX_CUA_STATE_OWNER_PID"] == "4242"
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


def test_token_file_rules(fake_home, monkeypatch):
    env = dict(os.environ)
    assert cmux.resolve_token(env) == (CUA_TOKEN, "file")
    fake_home.token.chmod(0o644)  # wrapper only trusts 0600 files
    assert cmux.resolve_token(env) == (None, None)
    env["CMUX_CUA_SOCKET_AUTH_TOKEN"] = "from-env"
    assert cmux.resolve_token(env) == ("from-env", "env")
    link = fake_home.tmp / "link"
    fake_home.token.chmod(0o600)
    link.symlink_to(fake_home.token)
    env["CMUX_CUA_AUTH_TOKEN_FILE"] = str(link)
    assert cmux.resolve_token(env) == ("from-env", "env")  # symlinks rejected


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
