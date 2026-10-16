"""install.sh in a temporary HOME with every network/package step stubbed.

`uv` is a stub that logs its arguments and, on `uv tool install`, drops a `cm`
shim that runs this checkout's code with the current interpreter. `curl`,
`uname` and `claude` are stubbed where a test needs them. PATH holds only the
stubs plus /usr/bin:/bin, so the real uv, node, npm and claude are never used.
Each run starts a new session (no controlling terminal), like `curl | sh` in CI.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REAL_RUN, REAL_POPEN = subprocess.run, subprocess.Popen  # captured before the autouse tripwire
REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "install.sh"

pytestmark = pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs /bin/sh")


@pytest.fixture(autouse=True)
def allow_processes(no_real_processes, monkeypatch):
    """This module runs /bin/sh on purpose (with stubbed tools only); re-enable processes here."""
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)
    monkeypatch.setattr(subprocess, "run", REAL_RUN)

UV_STUB = """#!/bin/sh
echo "uv $*" >> "$STUB_LOG"
case "$1 $2" in
  "tool install")
    mkdir -p "$HOME/.local/bin"
    cat > "$HOME/.local/bin/cm" <<EOF
#!/bin/sh
PYTHONPATH="{src}" exec "{py}" -m claude_launcher.cli "\\$@"
EOF
    chmod +x "$HOME/.local/bin/cm" ;;
  "tool dir") echo "$HOME/.local/bin" ;;
esac
"""


def _exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    stubs = tmp_path / "stubs"
    uv = UV_STUB.replace("{src}", str(REPO / "src")).replace("{py}", sys.executable)
    _exe(stubs / "uv", uv)
    log = tmp_path / "stub.log"
    log.write_text("")

    def run(*, stdin_script=False, env=None, with_uv=True, cwd=None):
        if not with_uv:
            (stubs / "uv").rename(tmp_path / "uv.stub")
        e = {"HOME": str(home), "PATH": f"{stubs}:{home}/.local/bin:/usr/bin:/bin", "STUB_LOG": str(log),
             "LANG": "C", "TERM": "dumb"}
        e.update(env or {})
        if stdin_script:
            argv, inp = ["/bin/sh"], SCRIPT.read_text()
        else:
            argv, inp = ["/bin/sh", str(SCRIPT)], ""
        p = REAL_RUN(argv, input=inp, capture_output=True, text=True, env=e, cwd=cwd or str(tmp_path),
                     timeout=120, start_new_session=True)
        return p, log.read_text()

    run.home, run.stubs, run.tmp = home, stubs, tmp_path
    return run


def test_from_checkout_creates_home_and_skips_agentmemory_without_tty(sandbox):
    p, log = sandbox()
    assert p.returncode == 0, p.stderr
    assert f"uv tool install --force --reinstall-package claude-launcher {REPO}" in log
    home = sandbox.home / ".claude-launcher"
    cfg = json.loads((home / "config.json").read_text())
    assert cfg["mcp_folders"] == ["~/.claude-launcher/mcps"] and "_comment" in cfg
    assert (home / "mcps").is_dir() and not any((home / "mcps").iterdir())
    assert "AgentMemory: skipped (no terminal to ask)" in p.stdout
    assert "INSTALL_AGENT_MEMORY" not in cfg  # nothing persisted without an answer
    assert "Claude Code (`claude`) is not on your PATH" in p.stderr
    assert "claude.ai/install.sh" in p.stderr


def test_rerun_upgrades_and_keeps_user_config(sandbox):
    assert sandbox()[0].returncode == 0
    cfg_path = sandbox.home / ".claude-launcher" / "config.json"
    cfg_path.write_text(json.dumps({"mcp_folders": ["~/mine"], "INSTALL_AGENT_MEMORY": "0"}))
    (sandbox.home / ".claude-launcher" / "mcps" / "x.json").write_text("{}")
    p, log = sandbox()
    assert p.returncode == 0, p.stderr
    assert log.count("uv tool install") == 2  # upgraded again
    assert json.loads(cfg_path.read_text()) == {"mcp_folders": ["~/mine"], "INSTALL_AGENT_MEMORY": "0"}
    assert (sandbox.home / ".claude-launcher" / "mcps" / "x.json").read_text() == "{}"
    assert "already set up" in p.stdout
    assert "AgentMemory" not in p.stdout  # gate 0: silent


def test_piped_mode_installs_from_github(sandbox):
    p, log = sandbox(stdin_script=True)
    assert p.returncode == 0, p.stderr
    assert "uv tool install --force --reinstall-package claude-launcher " \
           "git+https://github.com/vams-mx/claude-launcher@main" in log
    p, log = sandbox(stdin_script=True, env={"CM_REPO": "someone/claude-launcher", "CM_REF": "v0.2.0"})
    assert "git+https://github.com/someone/claude-launcher@v0.2.0" in log


def test_installs_uv_with_official_installer_when_missing(sandbox):
    uv_body = (sandbox.stubs / "uv").read_text()
    installer = f"""mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/uv" <<'UVEOF'
{uv_body}UVEOF
chmod +x "$HOME/.local/bin/uv"
"""
    (sandbox.tmp / "uv-installer.sh").write_text(installer)
    _exe(sandbox.stubs / "curl", f"""#!/bin/sh
echo "curl $*" >> "$STUB_LOG"
cat "{sandbox.tmp / 'uv-installer.sh'}"
""")
    p, log = sandbox(with_uv=False)
    assert p.returncode == 0, p.stderr
    assert "curl -LsSf https://astral.sh/uv/install.sh" in log
    assert f"uv: {sandbox.home}/.local/bin/uv" in p.stdout
    assert "uv tool install" in log


@pytest.mark.parametrize("system", ["MINGW64_NT-10.0-22631", "CYGWIN_NT-10.0", "FreeBSD"])
def test_refuses_unsupported_os(sandbox, system):
    _exe(sandbox.stubs / "uname", f"#!/bin/sh\necho {system}\n")
    p, log = sandbox()
    assert p.returncode == 1
    assert "macOS and Linux only (Windows is not supported)" in p.stderr
    assert "uv" not in log


@pytest.mark.parametrize("system,label", [("Darwin", "macos"), ("Linux", "linux")])
def test_supported_os(sandbox, system, label):
    _exe(sandbox.stubs / "uname", f"#!/bin/sh\necho {system}\n")
    p, _ = sandbox()
    assert p.returncode == 0, p.stderr
    assert f"cm installer ({label})" in p.stdout


def test_agentmemory_gate_from_environment(sandbox):
    p, _ = sandbox(env={"INSTALL_AGENT_MEMORY": "0"})
    assert p.returncode == 0 and "AgentMemory" not in p.stdout
    # 1 without node on PATH: the step reports what's missing, the installer still succeeds
    p, _ = sandbox(env={"INSTALL_AGENT_MEMORY": "1"})
    assert p.returncode == 0, p.stderr
    assert "Node.js >= 20 with npm is required" in p.stdout
    assert "AgentMemory setup did not finish" in p.stderr


def test_claude_on_path_is_reported(sandbox):
    _exe(sandbox.stubs / "claude", "#!/bin/sh\necho '2.1.281 (Claude Code)'\n")
    p, _ = sandbox()
    assert f"claude: {sandbox.stubs}/claude" in p.stdout
    assert "not on your PATH" not in p.stderr


def test_migrates_old_config_home(sandbox):
    old = sandbox.home / ".config" / "claude-launcher"
    (old / "profiles").mkdir(parents=True)
    (old / "profiles" / "p.json").write_text(json.dumps({"local": ["x"]}))
    p, _ = sandbox()
    assert p.returncode == 0, p.stderr
    new = sandbox.home / ".claude-launcher"
    assert (new / "profiles" / "p.json").is_file() and old.is_dir()
    assert "~/.personal" in (new / "config.json").read_text()
    assert "copied once from ~/.config/claude-launcher" in p.stderr


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_shellcheck():
    p = REAL_RUN(["shellcheck", "-s", "sh", str(SCRIPT)], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout


def test_posix_syntax():
    p = REAL_RUN(["/bin/sh", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert os.access(SCRIPT, os.X_OK)


def _run_in_pty(sandbox, answers: dict[str, str], stdin_script: bool = True, timeout: float = 60) -> str:
    """Run the installer with a pseudo-terminal as its controlling terminal, so /dev/tty
    works like a user's terminal. stdin_script=True simulates `curl ... | sh` with
    `cat install.sh | sh`: stdin is the script, prompts must be answered via /dev/tty.
    `answers` maps a prompt substring to the keystrokes sent once it appears."""
    import pty
    import select
    import time

    env = {"HOME": str(sandbox.home), "PATH": f"{sandbox.stubs}:{sandbox.home}/.local/bin:/usr/bin:/bin",
           "STUB_LOG": str(sandbox.tmp / "stub.log"), "LANG": "C", "TERM": "dumb"}
    cmd = f'cat "{SCRIPT}" | /bin/sh' if stdin_script else f'/bin/sh "{SCRIPT}"'
    pid, fd = pty.fork()
    if pid == 0:  # child
        os.chdir(str(sandbox.tmp))
        os.execve("/bin/sh", ["/bin/sh", "-c", cmd], env)
    out, pending, deadline = b"", dict(answers), time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            for prompt in list(pending):
                if prompt.encode() in out:
                    os.write(fd, pending.pop(prompt).encode())
        else:
            done, _status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
    else:
        os.kill(pid, 9)
        raise AssertionError("installer timed out: " + out.decode(errors="replace")[-500:])
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass  # already reaped in the loop
    os.close(fd)
    return out.decode(errors="replace")


def test_piped_install_asks_once_on_the_terminal_and_remembers_no(sandbox):
    out = _run_in_pty(sandbox, {"Set up AgentMemory now?": "n\r"})
    assert "Set up AgentMemory now? [y/N]" in out
    assert "not asking again" in out
    cfg = json.loads((sandbox.home / ".claude-launcher" / "config.json").read_text())
    assert cfg["INSTALL_AGENT_MEMORY"] == "0"
    again = _run_in_pty(sandbox, {})
    assert "Set up AgentMemory" not in again and "Done." in again


def test_piped_install_yes_without_node_reports_prerequisite(sandbox):
    out = _run_in_pty(sandbox, {"Set up AgentMemory now?": "y\r"})
    assert "Node.js >= 20 with npm is required" in out
    cfg = json.loads((sandbox.home / ".claude-launcher" / "config.json").read_text())
    assert cfg["INSTALL_AGENT_MEMORY"] == "1"
