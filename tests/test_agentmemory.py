"""`cm setup agentmemory`: gate, install, generated files, service. Everything with
side effects goes through a fake Ctx: no npm, node, launchctl or network."""

import json
import os
import plistlib
import stat
import subprocess
from pathlib import Path

import pytest

from claude_launcher import agentmemory as am
from claude_launcher import cli, config
from claude_launcher.catalog import discover
from claude_launcher.plan import Selection, build_plan

EVENTS = {
    "SessionStart": "session-start", "UserPromptSubmit": "prompt-submit", "PreToolUse": "pre-tool-use",
    "PostToolUse": "post-tool-use", "PostToolUseFailure": "post-tool-failure", "PreCompact": "pre-compact",
    "SubagentStart": "subagent-start", "SubagentStop": "subagent-stop", "Notification": "notification",
    "TaskCompleted": "task-completed", "Stop": "stop", "SessionEnd": "session-end",
}
ENV_SECRET = "env-secret-must-not-be-written-anywhere"


def make_package(root: Path, version: str = "0.9.30") -> Path:
    pkg = root / "@agentmemory" / "agentmemory"
    (pkg / "dist").mkdir(parents=True, exist_ok=True)
    (pkg / "dist" / "cli.mjs").write_text("// cli\n")
    (pkg / "package.json").write_text(json.dumps({"name": am.PACKAGE, "version": version,
                                                   "bin": {"agentmemory": "dist/cli.mjs"}}))
    scripts = pkg / "plugin" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    hooks = {}
    for ev, name in EVENTS.items():
        (scripts / f"{name}.mjs").write_text("// hook\n")
        group = {"hooks": [{"type": "command", "command": f'node "${{CLAUDE_PLUGIN_ROOT}}/scripts/{name}.mjs"'}]}
        if ev == "PreToolUse":
            group = {"matcher": "Edit|Write|Read|Glob|Grep", **group}
        hooks[ev] = [group]
    (pkg / "plugin" / "hooks").mkdir(exist_ok=True)
    (pkg / "plugin" / "hooks" / "hooks.json").write_text(json.dumps({"hooks": hooks}))
    return pkg


class Fake:
    """Scripted ctx: answers to questions in order, recorded commands."""

    def __init__(self, tmp: Path, *, answers=(), installed=True, node="v22.21.1", interactive=True,
                 platform="darwin", backend="backend down", env=None, tools=("node", "npm", "systemctl")):
        self.root = tmp / "npm-global" / "lib" / "node_modules"
        self.root.mkdir(parents=True, exist_ok=True)
        if installed:
            make_package(self.root)
        self.answers = list(answers)
        self.questions: list[str] = []
        self.commands: list[list[str]] = []
        self.out: list[str] = []
        self.node = node
        self.tools = set(tools)
        self.ctx = am.Ctx(run=self.run, which=self.which, ask=self.ask, say=self.out.append,
                          interactive=interactive, platform=platform,
                          env={"AGENTMEMORY_SECRET": ENV_SECRET, **(env or {})},
                          livez=lambda url: backend, uid=501)

    def which(self, name):
        return f"/fake/bin/{name}" if name in self.tools else None

    def ask(self, q, default):
        self.questions.append(q)
        if not self.answers:
            raise AssertionError(f"unexpected question: {q}")
        return self.answers.pop(0)

    def run(self, argv, **kw):
        self.commands.append(list(argv))
        if argv[:2] == ["node", "--version"]:
            return subprocess.CompletedProcess(argv, 0, self.node + "\n", "")
        if argv[:3] == ["npm", "root", "-g"]:
            return subprocess.CompletedProcess(argv, 0, f"{self.root}\n", "")
        if argv[:3] == ["npm", "install", "-g"]:
            make_package(self.root)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    @property
    def text(self):
        return "\n".join(self.out)


@pytest.fixture
def am_home(fake_home):
    """fake_home without its hand-made AgentMemory config."""
    for f in ("claude-memory-mcp.json", "claude-memory-settings.json"):
        (fake_home.home / "extra-mcps" / f).unlink()
    return fake_home


def mcps(h):
    return h.home / ".claude-launcher" / "mcps"


def gate_in_config(h):
    return json.loads((h.home / ".claude-launcher" / "config.json").read_text()).get(am.GATE)


# ---------------------------------------------------------------- gate


def test_unset_decline_persists_zero_and_never_asks_again(am_home, tmp_path):
    f = Fake(tmp_path, answers=[False])
    assert am.run_setup(f.ctx, from_installer=True) == 0
    assert f.questions == ["Set up AgentMemory now?"]
    assert gate_in_config(am_home) == "0"
    assert f.commands == [] and not (mcps(am_home) / am.MCP_FILE).exists()
    f2 = Fake(tmp_path / "again", answers=[])
    assert am.run_setup(f2.ctx, from_installer=True) == 0
    assert f2.questions == [] and f2.out == [] and f2.commands == []  # silent


def test_gate_zero_explicit_command_explains(am_home, tmp_path):
    config.save_config({am.GATE: "0"})
    f = Fake(tmp_path)
    assert am.run_setup(f.ctx) == 0
    assert "INSTALL_AGENT_MEMORY=0" in f.text and f.commands == []


def test_unset_without_terminal_asks_nothing_and_persists_nothing(am_home, tmp_path):
    f = Fake(tmp_path, interactive=False)
    assert am.run_setup(f.ctx, from_installer=True) == 0
    assert f.questions == [] and f.commands == []
    assert gate_in_config(am_home) is None
    assert "cm setup agentmemory" in f.text


def test_unset_accept_persists_one_and_runs(am_home, tmp_path):
    f = Fake(tmp_path, answers=[True, False])  # yes to setup, no to the service
    assert am.run_setup(f.ctx, from_installer=True) == 0
    assert gate_in_config(am_home) == "1"
    assert (mcps(am_home) / am.MCP_FILE).is_file()
    assert any("service" in q or "agent" in q for q in f.questions[1:])


def test_env_overrides_config(am_home, tmp_path):
    config.save_config({am.GATE: "1"})
    f = Fake(tmp_path, env={am.GATE: "0"})
    assert am.run_setup(f.ctx, from_installer=True) == 0
    assert f.commands == [] and f.out == []
    config.save_config({am.GATE: "0"})
    f = Fake(tmp_path / "b", env={am.GATE: "1"}, answers=[False])  # service: no
    assert am.run_setup(f.ctx, from_installer=True) == 0
    assert (mcps(am_home) / am.MCP_FILE).is_file()
    assert gate_in_config(am_home) == "0"  # env never rewrites the stored choice


def test_gate_value_parsing(am_home):
    assert am.gate_value({}) is None
    assert am.gate_value({am.GATE: "1"}) == "1" and am.gate_value({am.GATE: " 0 "}) == "0"
    config.save_config({am.GATE: 1})
    assert am.gate_value({}) == "1" and am.gate_value({am.GATE: ""}) == "1"
    with pytest.raises(am.SetupError):
        am.gate_value({am.GATE: "maybe"})


def test_invalid_gate_exits_2(am_home, tmp_path):
    f = Fake(tmp_path, env={am.GATE: "maybe"})
    assert am.run_setup(f.ctx) == 2


# ---------------------------------------------------------------- prerequisites and install


def test_missing_node_stops_with_instructions(am_home, tmp_path):
    f = Fake(tmp_path, env={am.GATE: "1"}, tools=())
    assert am.run_setup(f.ctx) == 1
    assert "Node.js >= 20" in f.text and not (mcps(am_home) / am.MCP_FILE).exists()


def test_old_node_stops(am_home, tmp_path):
    f = Fake(tmp_path, env={am.GATE: "1"}, node="v18.19.0")
    assert am.run_setup(f.ctx) == 1
    assert "Node.js 18" in f.text


def test_install_is_confirmed(am_home, tmp_path):
    f = Fake(tmp_path, env={am.GATE: "1"}, installed=False, answers=[False])
    assert am.run_setup(f.ctx) == 1
    assert ["npm", "install", "-g", am.PACKAGE] not in f.commands
    f = Fake(tmp_path / "b", env={am.GATE: "1"}, installed=False, answers=[True, False])
    assert am.run_setup(f.ctx) == 0
    assert ["npm", "install", "-g", am.PACKAGE] in f.commands
    assert (mcps(am_home) / am.SETTINGS_FILE).is_file()


def test_no_terminal_skips_install(am_home, tmp_path):
    f = Fake(tmp_path, env={am.GATE: "1"}, installed=False, interactive=False)
    assert am.run_setup(f.ctx) == 1
    assert ["npm", "install", "-g", am.PACKAGE] not in f.commands and "skipped" in f.text


# ---------------------------------------------------------------- generated files


def test_generated_files(am_home, tmp_path):
    f = Fake(tmp_path, env={am.GATE: "1"}, answers=[False])
    assert am.run_setup(f.ctx) == 0
    mcp_file, st_file = mcps(am_home) / am.MCP_FILE, mcps(am_home) / am.SETTINGS_FILE
    for p in (mcp_file, st_file):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert ENV_SECRET not in p.read_text()
    srv = json.loads(mcp_file.read_text())["mcpServers"]["agentmemory"]
    assert srv["command"] == "npx" and srv["args"] == ["-y", "@agentmemory/mcp"]
    assert srv["env"]["AGENTMEMORY_SECRET"] == "${AGENTMEMORY_SECRET:-}"
    assert srv["env"]["AGENTMEMORY_URL"] == "${AGENTMEMORY_URL:-http://localhost:3111}"
    hooks = json.loads(st_file.read_text())["hooks"]
    assert set(hooks) == set(EVENTS)
    plugin = f.root / "@agentmemory" / "agentmemory" / "plugin"
    for ev, name in EVENTS.items():
        cmd = hooks[ev][0]["hooks"][0]["command"]
        assert "CLAUDE_PLUGIN_ROOT" not in cmd
        assert cmd == f'node "{plugin}/scripts/{name}.mjs"'
    assert hooks["PreToolUse"][0]["matcher"] == "Edit|Write|Read|Glob|Grep"


def test_generated_entry_is_selectable_and_hooks_only_when_selected(am_home, tmp_path):
    f = Fake(tmp_path, env={am.GATE: "1"}, answers=[False])
    am.run_setup(f.ctx)
    cat = discover(am_home.project)
    srv = cat.local_by_name("agentmemory")
    assert srv.source == "file:~/.claude-launcher/mcps/agentmemory-mcp.json"
    assert srv.settings_path == mcps(am_home) / am.SETTINGS_FILE
    with_mem = build_plan(cat, Selection(local=["agentmemory"]), claude_bin="claude", session_dir=tmp_path)
    assert set(with_mem.settings_doc["hooks"]) == set(EVENTS)
    assert with_mem.mcp_doc["mcpServers"]["agentmemory"]["env"]["AGENTMEMORY_SECRET"] == "${AGENTMEMORY_SECRET:-}"
    without = build_plan(cat, Selection(local=["gdrive"]), claude_bin="claude", session_dir=tmp_path)
    assert "hooks" not in without.settings_doc


def test_rerun_is_a_no_op(am_home, tmp_path):
    am.run_setup(Fake(tmp_path, env={am.GATE: "1"}, answers=[False]).ctx)
    before = {p.name: p.read_bytes() for p in mcps(am_home).iterdir()}
    f = Fake(tmp_path, env={am.GATE: "1"}, answers=[False])  # same install: only the service question
    assert am.run_setup(f.ctx) == 0
    assert "up to date" in f.text and len(f.questions) == 1
    assert {p.name: p.read_bytes() for p in mcps(am_home).iterdir()} == before


def test_never_overwrites_without_asking(am_home, tmp_path):
    custom = mcps(am_home) / am.MCP_FILE
    custom.write_text(json.dumps({"mcpServers": {"agentmemory": {"command": "mine"}}}))
    f = Fake(tmp_path, env={am.GATE: "1"}, answers=[False, False])  # keep it; no service
    assert am.run_setup(f.ctx) == 0
    assert json.loads(custom.read_text())["mcpServers"]["agentmemory"]["command"] == "mine"
    assert any("exists and differs" in q for q in f.questions)
    f = Fake(tmp_path, env={am.GATE: "1"}, answers=[True, False])  # replace it
    assert am.run_setup(f.ctx) == 0
    assert json.loads(custom.read_text())["mcpServers"]["agentmemory"]["command"] == "npx"
    backups = list(mcps(am_home).glob(am.MCP_FILE + ".bak-*"))
    assert len(backups) == 1 and "mine" in backups[0].read_text()


def test_non_interactive_never_overwrites(am_home, tmp_path):
    custom = mcps(am_home) / am.SETTINGS_FILE
    custom.write_text("{}")
    f = Fake(tmp_path, env={am.GATE: "1"}, interactive=False)
    assert am.run_setup(f.ctx) == 0
    assert custom.read_text() == "{}"
    assert (mcps(am_home) / am.MCP_FILE).is_file()  # a new file in cm's own folder is fine


def test_existing_agentmemory_elsewhere_is_respected(fake_home, tmp_path):
    f = Fake(tmp_path, env={am.GATE: "1"}, answers=[False, False])  # don't duplicate; no service
    assert am.run_setup(f.ctx) == 0
    assert "already defined in file:~/extra-mcps/claude-memory-mcp.json" in f.text
    assert not (mcps(fake_home) / am.MCP_FILE).exists()


def test_missing_hook_script_is_an_error(am_home, tmp_path):
    f = Fake(tmp_path, env={am.GATE: "1"})
    (f.root / "@agentmemory" / "agentmemory" / "plugin" / "scripts" / "stop.mjs").unlink()
    assert am.run_setup(f.ctx) == 1
    assert "stop.mjs" in f.text and not (mcps(am_home) / am.SETTINGS_FILE).exists()


def test_adds_default_folder_to_config_when_missing(am_home, tmp_path):
    config.save_config({"mcp_folders": ["~/extra-mcps"]})
    f = Fake(tmp_path, env={am.GATE: "1"}, answers=[True, False])  # add folder; no service
    assert am.run_setup(f.ctx) == 0
    cfg = json.loads((am_home.home / ".claude-launcher" / "config.json").read_text())
    assert cfg["mcp_folders"] == ["~/.claude-launcher/mcps", "~/extra-mcps"]


# ---------------------------------------------------------------- service


def test_running_backend_is_left_alone(am_home, tmp_path):
    f = Fake(tmp_path, env={am.GATE: "1"}, backend="backend up")
    assert am.run_setup(f.ctx) == 0
    assert f.questions == [] and "already running" in f.text
    assert not any(c[0] in ("launchctl", "systemctl") for c in f.commands)


def test_existing_service_definition_is_left_alone(am_home, tmp_path):
    la = am_home.home / "Library" / "LaunchAgents"
    la.mkdir(parents=True)
    (la / "com.someone.agentmemory.plist").write_text("x")
    f = Fake(tmp_path, env={am.GATE: "1"})
    assert am.run_setup(f.ctx) == 0
    assert f.questions == [] and "left alone" in f.text


def test_macos_launchd_agent(am_home, tmp_path):
    f = Fake(tmp_path, env={am.GATE: "1"}, answers=[True, True])
    assert am.run_setup(f.ctx) == 0
    plist = am_home.home / "Library" / "LaunchAgents" / f"{am.SERVICE_LABEL}.plist"
    doc = plistlib.loads(plist.read_bytes())
    cli_js = f.root / "@agentmemory" / "agentmemory" / "dist" / "cli.mjs"
    assert doc["ProgramArguments"] == ["/fake/bin/node", str(cli_js)]
    assert doc["Label"] == am.SERVICE_LABEL and doc["RunAtLoad"] is True
    assert ENV_SECRET not in plist.read_text()
    assert ["launchctl", "bootstrap", "gui/501", str(plist)] in f.commands


def test_service_question_declined(am_home, tmp_path):
    f = Fake(tmp_path, env={am.GATE: "1"}, answers=[False])
    assert am.run_setup(f.ctx) == 0
    assert not (am_home.home / "Library" / "LaunchAgents").exists()
    assert not any(c[0] == "launchctl" for c in f.commands)


def test_no_service_flag_skips_question(am_home, tmp_path):
    f = Fake(tmp_path, env={am.GATE: "1"})
    assert am.run_setup(f.ctx, with_service=False) == 0
    assert f.questions == []


def test_linux_systemd_user_unit(am_home, tmp_path):
    f = Fake(tmp_path, env={am.GATE: "1"}, platform="linux", answers=[True, False])  # register, don't start
    assert am.run_setup(f.ctx) == 0
    unit = am_home.home / ".config" / "systemd" / "user" / am.SYSTEMD_UNIT
    text = unit.read_text()
    assert 'ExecStart="/fake/bin/node"' in text and "WantedBy=default.target" in text
    assert ENV_SECRET not in text
    assert ["systemctl", "--user", "daemon-reload"] in f.commands
    assert ["systemctl", "--user", "enable", am.SYSTEMD_UNIT] in f.commands
    assert ["systemctl", "--user", "start", am.SYSTEMD_UNIT] not in f.commands
    assert "start it later" in f.text


def test_linux_without_systemd(am_home, tmp_path):
    f = Fake(tmp_path, env={am.GATE: "1"}, platform="linux", tools=("node", "npm"))
    assert am.run_setup(f.ctx) == 0
    assert f.questions == [] and "systemd not found" in f.text


# ---------------------------------------------------------------- cli wiring


def test_cli_setup_subcommands(am_home, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(am, "run_setup", lambda **kw: calls.append(kw) or 0)
    assert cli.main(["setup", "agentmemory", "--from-installer", "--no-service"]) == 0
    assert calls == [{"from_installer": True, "with_service": False}]
    assert cli.main(["setup", "init"]) == 0
    assert "already set up" in capsys.readouterr().out


def test_cli_setup_init_fresh(tmp_path, monkeypatch, capsys):
    for var in list(os.environ):
        if var.startswith(("CM_", "XDG_CONFIG_HOME")):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["setup", "init"]) == 0
    assert "created" in capsys.readouterr().out
    assert (tmp_path / ".claude-launcher" / "config.json").is_file()
    assert (tmp_path / ".claude-launcher" / "mcps").is_dir()


# ---------------------------------------------------------------- review regressions


def _install(tmp_path: Path, dirname: str, commands: dict[str, str]) -> am.Install:
    plugin = tmp_path / dirname / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "hooks").mkdir()
    hooks = {ev: [{"hooks": [{"type": "command", "command": cmd}]}] for ev, cmd in commands.items()}
    (plugin / "hooks" / "hooks.json").write_text(json.dumps({"hooks": hooks}))
    return am.Install(plugin.parent, "0.9.30", plugin, plugin / "hooks" / "hooks.json", None)


def test_hook_paths_with_spaces_and_quotes_stay_one_shell_word(tmp_path):
    import shlex

    inst = _install(tmp_path, "Jane Doe's \"npm\" $x", {
        "SessionStart": "node ${CLAUDE_PLUGIN_ROOT}/scripts/a.mjs",
        "Stop": 'node "${CLAUDE_PLUGIN_ROOT}/scripts/b.mjs" --flag',
        "SessionEnd": "node '$CLAUDE_PLUGIN_ROOT/scripts/c.mjs'",
    })
    for n in "abc":
        (inst.plugin_dir / "scripts" / f"{n}.mjs").write_text("// hook\n")
    hooks = am.build_settings(inst)["hooks"]
    words = {ev: shlex.split(hooks[ev][0]["hooks"][0]["command"]) for ev in hooks}
    scripts = inst.plugin_dir / "scripts"
    assert words["SessionStart"] == ["node", f"{scripts}/a.mjs"]
    assert words["Stop"] == ["node", f"{scripts}/b.mjs", "--flag"]
    assert words["SessionEnd"] == ["node", f"{scripts}/c.mjs"]


def test_missing_unquoted_hook_script_is_an_error(tmp_path):
    inst = _install(tmp_path, "npm", {"Stop": "node ${CLAUDE_PLUGIN_ROOT}/scripts/gone.mjs"})
    with pytest.raises(am.SetupError, match="gone.mjs"):
        am.build_settings(inst)


def test_agentmemory_in_claude_json_does_not_block_setup(am_home, tmp_path):
    cj = am_home.home / ".claude.json"
    doc = json.loads(cj.read_text())
    doc["mcpServers"]["agentmemory"] = {"command": "npx", "args": ["-y", "@agentmemory/mcp"]}
    cj.write_text(json.dumps(doc))
    before = cj.read_bytes()
    f = Fake(tmp_path, env={am.GATE: "1"}, interactive=False)
    assert am.run_setup(f.ctx) == 0
    assert (mcps(am_home) / am.MCP_FILE).is_file() and cj.read_bytes() == before
    assert "take precedence over ~/.claude.json" in f.text
    srv = discover(am_home.project).local_by_name("agentmemory")  # ...and that is what discovery does
    assert srv.source == "file:~/.claude-launcher/mcps/agentmemory-mcp.json"
    assert srv.also_in == ["user (~/.claude.json)"]
