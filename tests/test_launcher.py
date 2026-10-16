import json
import os
import signal
import stat
from pathlib import Path

from claude_launcher.launcher import exit_code, launch
from claude_launcher.plan import Selection
from tests.conftest import CUA_TOKEN


def test_launch_writes_private_files_for_runtime_then_cleans_up(catalog, fake_popen):
    seen = {}

    def on_wait(proc):
        mcp = Path(next(a.split("=", 1)[1] for a in proc.argv if a.startswith("--mcp-config=")))
        settings = Path(proc.argv[proc.argv.index("--settings") + 1])
        seen["dir"] = mcp.parent
        seen["dir_mode"] = stat.S_IMODE(mcp.parent.stat().st_mode)
        seen["modes"] = {stat.S_IMODE(p.stat().st_mode) for p in (mcp, settings)}
        seen["mcp"] = json.loads(mcp.read_text())
        seen["settings"] = json.loads(settings.read_text())

    popen = fake_popen(rc=0, on_wait=on_wait)
    rc = launch(catalog, Selection(local=["agentmemory", "gdrive"], cmux=True), claude_bin="claude", popen=popen)
    assert rc == 0
    proc = popen.calls[0]
    assert isinstance(proc.argv, list) and proc.argv[0] == "claude"
    assert "shell" not in proc.kw
    assert seen["modes"] == {0o600} and seen["dir_mode"] == 0o700
    assert set(seen["mcp"]["mcpServers"]) == {"agentmemory", "gdrive", "cmux-cua"}
    assert "hooks" in seen["settings"]
    assert not seen["dir"].exists()  # removed after claude exits
    env = proc.kw["env"]
    assert env["CMUX_COMPUTER_USE_MCP_DISABLED"] == "1"
    assert env["CMUX_CUA_SOCKET_AUTH_TOKEN"] == CUA_TOKEN
    assert env["HOME"] == os.environ["HOME"]  # environment passed through
    assert proc.kw["cwd"] == catalog.cwd  # claude runs where the catalog was discovered
    assert CUA_TOKEN not in json.dumps(seen["mcp"])


def test_exit_codes(catalog, fake_popen):
    assert launch(catalog, Selection(), claude_bin="claude", popen=fake_popen(rc=3)) == 3
    assert launch(catalog, Selection(), claude_bin="claude", popen=fake_popen(rc=-signal.SIGINT)) == 130
    assert exit_code(-15) == 143


def test_cleanup_even_when_wait_raises(catalog, fake_popen):
    holder = {}

    def boom(proc):
        holder["dir"] = Path(proc.argv[proc.argv.index("--settings") + 1]).parent
        raise RuntimeError("x")

    try:
        launch(catalog, Selection(), claude_bin="claude", popen=fake_popen(on_wait=boom))
    except RuntimeError:
        pass
    assert not holder["dir"].exists()


def test_missing_binary(catalog, capsys):
    def popen(argv, **kw):
        raise FileNotFoundError(argv[0])

    assert launch(catalog, Selection(), claude_bin="/nope/claude", popen=popen) == 127
    assert "not found" in capsys.readouterr().err


def test_sigint_ignored_while_child_runs_and_restored(catalog, fake_popen):
    before = signal.getsignal(signal.SIGINT)
    seen = {}

    def on_wait(proc):
        seen["int"] = signal.getsignal(signal.SIGINT)
        seen["term"] = signal.getsignal(signal.SIGTERM)
        seen["term"](signal.SIGTERM, None)  # forwarded to the child

    popen = fake_popen(on_wait=on_wait)
    launch(catalog, Selection(), claude_bin="claude", popen=popen)
    assert seen["int"] == signal.SIG_IGN
    assert popen.calls[0].signals == [signal.SIGTERM]
    assert signal.getsignal(signal.SIGINT) == before
