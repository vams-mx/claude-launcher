"""Config home, one-time migration, multiple MCP folders, `cm --where`, Linux paths."""

import json
import os
from pathlib import Path

import pytest

from claude_launcher import cli, config, paths, profiles
from claude_launcher.catalog import discover
from tests.conftest import MEM_SECRET, SECRET


@pytest.fixture
def bare_home(tmp_path, monkeypatch):
    """A HOME with no cm state at all (no CM_CONFIG_DIR)."""
    home = tmp_path / "home"
    home.mkdir()
    for var in list(os.environ):
        if var.startswith(("CM_", "XDG_CONFIG_HOME", "INSTALL_AGENT_MEMORY", "CLAUDE")):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CM_MANAGED_DIR", str(tmp_path / "managed"))
    monkeypatch.setenv("CM_CMUX_WRAPPER", str(tmp_path / "no-cmux"))
    monkeypatch.chdir(tmp_path)
    import claude_launcher.cmux as cmux

    monkeypatch.setattr(cmux, "DEFAULT_APP_BIN", tmp_path / "no-cmux-app")
    return home


def _legacy(home: Path, catalog: dict | None = None) -> Path:
    old = home / ".config" / "claude-launcher"
    (old / "profiles").mkdir(parents=True)
    (old / "profiles" / "memory+gdrive.json").write_text(json.dumps({"local": ["agentmemory", "gdrive"]}))
    (old / "connectors.json").write_text(json.dumps({"refreshed_at": "x", "connectors": [
        {"name": "claude.ai Slack", "status": "Connected"}]}))
    (old / "costs.json").write_text(json.dumps({"items": {"mcp:gdrive": {"tokens": 34, "tools": 1}}}))
    (old / "config.json").write_text(json.dumps({"default_profile": "memory+gdrive"}))
    if catalog is not None:
        (old / "catalog.json").write_text(json.dumps(catalog))
    return old


def _snapshot(d: Path) -> dict:
    return {p.relative_to(d): p.read_bytes() for p in d.rglob("*") if p.is_file()}


# ---------------------------------------------------------------- paths


def test_default_config_home_and_override(bare_home, monkeypatch, tmp_path):
    assert paths.config_dir() == bare_home / ".claude-launcher"
    assert config.mcp_folders({}) == [bare_home / ".claude-launcher" / "mcps"]
    monkeypatch.setenv("CM_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert paths.config_dir() == tmp_path / "elsewhere"
    assert config.mcp_folders({}) == [tmp_path / "elsewhere" / "mcps"]


def test_no_personal_defaults_in_code(bare_home):
    (bare_home / ".personal").mkdir()
    (bare_home / ".personal" / "x-mcp.json").write_text(json.dumps({"mcpServers": {"x": {"command": "x"}}}))
    cat = discover(bare_home)
    assert cat.local_by_name("x") is None
    assert cat.mcp_folders == [bare_home / ".claude-launcher" / "mcps"]


def test_managed_dir_per_platform(monkeypatch):
    monkeypatch.delenv("CM_MANAGED_DIR", raising=False)
    assert paths.managed_dir("darwin") == Path("/Library/Application Support/ClaudeCode")
    assert paths.managed_dir("linux") == Path("/etc/claude-code")
    assert paths.managed_dir("linux2") == Path("/etc/claude-code")
    monkeypatch.setattr("sys.platform", "linux")
    assert paths.managed_dir() == Path("/etc/claude-code")
    monkeypatch.setenv("CM_MANAGED_DIR", "/x")
    assert paths.managed_dir() == Path("/x")


def test_managed_names_and_sideload_dropin_are_read(fake_home):
    md = Path(os.environ["CM_MANAGED_DIR"])
    (md / "managed-settings.d").mkdir(parents=True)
    (md / "managed-mcp.json").write_text(json.dumps({"mcpServers": {"corp": {"command": "corp"}}}))
    (md / "managed-settings.d" / "10-policy.json").write_text(json.dumps({"disableSideloadFlags": True}))
    cat = discover(fake_home.project)
    assert "corp" in cat.other_server_names
    assert any("disableSideloadFlags" in w for w in cat.warnings)


# ---------------------------------------------------------------- migration


def test_migration_copies_once_and_keeps_old_dir(bare_home, capsys):
    old = _legacy(bare_home)
    before = _snapshot(old)
    assert config.migrate_legacy() is True
    new = bare_home / ".claude-launcher"
    assert (new / "profiles" / "memory+gdrive.json").is_file()
    assert (new / "connectors.json").is_file() and (new / "costs.json").is_file()
    assert (new / "mcps").is_dir()
    cfg = json.loads((new / "config.json").read_text())
    assert cfg["default_profile"] == "memory+gdrive"
    assert cfg["mcp_folders"] == ["~/.claude-launcher/mcps", "~/.personal/claude-mcps", "~/.personal"]
    assert "_comment" in cfg
    err = capsys.readouterr().err
    assert "copied once from ~/.config/claude-launcher" in err
    assert _snapshot(old) == before  # never modified
    # second run: no-op, no notice
    assert config.migrate_legacy() is False
    assert capsys.readouterr().err == ""
    assert old.is_dir()
    assert not list(bare_home.glob(".claude-launcher-migrate-*"))


def test_migration_keeps_legacy_catalog_folders_and_sources(bare_home):
    _legacy(bare_home, {"mcp_folders": ["~/mine"], "sources": [{"mcp_config": "~/x-mcp.json"}]})
    config.migrate_legacy(lambda m: None)
    cfg = json.loads((bare_home / ".claude-launcher" / "config.json").read_text())
    assert cfg["mcp_folders"] == ["~/.claude-launcher/mcps", "~/mine"]
    assert cfg["sources"] == [{"mcp_config": "~/x-mcp.json"}]
    assert not (bare_home / ".claude-launcher" / "catalog.json").exists()


def test_migration_respects_xdg_and_skips_when_new_exists_or_override(bare_home, monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    (xdg / "claude-launcher").mkdir(parents=True)
    (xdg / "claude-launcher" / "costs.json").write_text("{}")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("CM_CONFIG_DIR", str(tmp_path / "custom"))
    assert config.migrate_legacy(lambda m: None) is False  # explicit override: never migrate
    monkeypatch.delenv("CM_CONFIG_DIR")
    (bare_home / ".claude-launcher").mkdir()
    assert config.migrate_legacy(lambda m: None) is False  # new home already exists
    (bare_home / ".claude-launcher").rmdir()
    assert config.migrate_legacy(lambda m: None) is True
    assert (bare_home / ".claude-launcher" / "costs.json").is_file()


def test_migrated_setup_keeps_working_through_cli(bare_home, capsys, monkeypatch):
    _legacy(bare_home)
    personal = bare_home / ".personal"
    personal.mkdir()
    (personal / "claude-memory-mcp.json").write_text(json.dumps({"mcpServers": {"agentmemory": {
        "command": "npx", "args": ["-y", "@agentmemory/mcp"], "env": {"AGENTMEMORY_SECRET": MEM_SECRET}}}}))
    (personal / "claude-memory-settings.json").write_text(json.dumps({"hooks": {"Stop": []}}))
    (bare_home / ".claude.json").write_text(json.dumps({"projects": {str(bare_home): {"mcpServers": {
        "gdrive": {"command": "npx", "args": ["-y", "gdrive"]}}}}}))
    launched = []
    monkeypatch.setattr(cli, "launch", lambda cat, sel, *, claude_bin: launched.append(sel) or 0)
    assert cli.main(["--claude-bin", "claude", "--list", "--json"]) == 0
    captured = capsys.readouterr()
    assert "config home is now ~/.claude-launcher" in captured.err
    doc = json.loads(captured.out)
    assert {x["name"] for x in doc["local"]} == {"agentmemory", "gdrive"}
    assert doc["default_profile"] == "memory+gdrive"
    assert MEM_SECRET not in captured.out
    assert cli.main(["--claude-bin", "claude", "--profile", "memory+gdrive"]) == 0
    assert launched[-1].local == ["agentmemory", "gdrive"]
    assert "config home" not in capsys.readouterr().err  # notice printed only once


# ---------------------------------------------------------------- config file


def test_init_home_creates_example_and_never_overwrites(bare_home):
    created = config.init_home()
    home = bare_home / ".claude-launcher"
    assert (home / "mcps").is_dir()
    cfg = json.loads((home / "config.json").read_text())
    assert cfg["mcp_folders"] == ["~/.claude-launcher/mcps"] and cfg["sources"] == []
    assert any("mcp_folders" in line for line in cfg["_comment"])
    assert len(created) == 3
    (home / "config.json").write_text(json.dumps({"mcp_folders": ["~/a"], "mine": True}))
    assert config.init_home() == []
    assert json.loads((home / "config.json").read_text()) == {"mcp_folders": ["~/a"], "mine": True}


def test_save_config_keeps_other_keys(bare_home):
    config.init_home()
    config.save_config({"INSTALL_AGENT_MEMORY": "0"})
    profiles.save("p", profiles.Selection())
    profiles.set_default("p")
    cfg = json.loads((bare_home / ".claude-launcher" / "config.json").read_text())
    assert cfg["INSTALL_AGENT_MEMORY"] == "0" and cfg["default_profile"] == "p" and "_comment" in cfg
    profiles.set_default(None)
    assert "default_profile" not in json.loads((bare_home / ".claude-launcher" / "config.json").read_text())


def test_broken_config_is_a_warning_not_a_crash(fake_home):
    (fake_home.home / ".claude-launcher" / "config.json").write_text("{nope")
    cat = discover(fake_home.project)
    assert any("config.json" in w for w in cat.warnings)


# ---------------------------------------------------------------- multiple folders and sources


def test_multiple_folders_all_discovered_in_order(fake_home):
    h = fake_home.home
    for d, name in (("a", "alpha"), ("b", "beta"), ("c", "alpha")):
        (h / d).mkdir()
        (h / d / f"{name}.json").write_text(json.dumps({"mcpServers": {name: {"command": f"{d}-bin"}}}))
    (h / "b" / "beta.settings.json").write_text(json.dumps({"hooks": {"Stop": []}}))
    config.save_config({"mcp_folders": ["~/a", "~/b", "~/missing", "~/c", "~/extra-mcps"]})
    cat = discover(fake_home.project)
    assert cat.local_by_name("alpha").config == {"command": "a-bin"}  # first folder wins
    assert cat.local_by_name("alpha").also_in == ["file:~/c/alpha.json"]
    assert cat.local_by_name("beta").settings_path == h / "b" / "beta.settings.json"
    assert cat.local_by_name("agentmemory") is not None
    assert cat.mcp_folders == [h / "a", h / "b", h / "missing", h / "c", h / "extra-mcps"]


def test_folder_list_edge_cases(fake_home):
    config.save_config({"mcp_folders": []})
    cat = discover(fake_home.project)
    assert cat.mcp_folders == [] and cat.local_by_name("agentmemory") is None
    config.save_config({"mcp_folders": "~/extra-mcps"})
    assert discover(fake_home.project).local_by_name("agentmemory") is not None
    config.save_config({"mcp_folders": None})  # removed -> default folder only
    assert discover(fake_home.project).mcp_folders == [fake_home.home / ".claude-launcher" / "mcps"]


def test_explicit_sources_with_settings(fake_home):
    h = fake_home.home
    (h / "x").mkdir()
    (h / "x" / "srv.json").write_text(json.dumps({"mcpServers": {"srv": {"command": "srv"}}}))
    (h / "x" / "hooks.json").write_text(json.dumps({"hooks": {}}))
    config.save_config({"sources": [{"mcp_config": "~/x/srv.json", "settings": "~/x/hooks.json",
                                     "description": "d"}, {"mcp_config": "~/x/gone.json"}]})
    cat = discover(fake_home.project)
    s = cat.local_by_name("srv")
    assert s.settings_path == h / "x" / "hooks.json" and s.description == "d"
    assert any("gone.json" in w for w in cat.warnings)


def test_legacy_catalog_json_in_config_home_still_read(fake_home):
    h = fake_home.home
    config.save_config({"mcp_folders": None})
    (h / ".claude-launcher" / "catalog.json").write_text(json.dumps({"mcp_folders": ["~/extra-mcps"]}))
    assert discover(fake_home.project).local_by_name("agentmemory") is not None


# ---------------------------------------------------------------- cm --where


def test_where_shows_home_folders_and_origins(fake_home, capsys):
    assert cli.main(["--claude-bin", "claude", "--where"]) == 0
    out = capsys.readouterr().out
    assert "config home   ~/.claude-launcher" in out
    assert "1. ~/.claude-launcher/mcps  [0 server file(s)]" in out
    assert "2. ~/extra-mcps  [1 server file(s)]" in out
    assert "~/extra-mcps/claude-memory-mcp.json  + settings ~/extra-mcps/claude-memory-settings.json" in out
    assert "gdrive" in out and "~/.claude.json (local scope of ~)" in out
    for secret in (SECRET, MEM_SECRET, "/secret/path.json", "token=abc"):
        assert secret not in out


def test_where_json(fake_home, capsys):
    config.save_config({"mcp_folders": ["~/extra-mcps", "~/nope"]})
    assert cli.main(["--claude-bin", "claude", "--where", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [f["path"] for f in doc["mcp_folders"]] == ["~/extra-mcps", "~/nope"]
    assert doc["mcp_folders"][1]["exists"] is False
    am = next(s for s in doc["servers"] if s["name"] == "agentmemory")
    assert am["from"] == "~/extra-mcps/claude-memory-mcp.json"
    assert doc["claude"]["version"] == "2.1.281"


# ---------------------------------------------------------------- review regressions


def test_override_with_legacy_home_says_so_instead_of_migrating(bare_home, monkeypatch, tmp_path):
    _legacy(bare_home)
    custom = tmp_path / "custom"
    monkeypatch.setenv("CM_CONFIG_DIR", str(custom))
    said: list[str] = []
    assert config.migrate_legacy(said.append) is False
    assert not custom.exists() and not (bare_home / ".claude-launcher").exists()
    assert len(said) == 1 and "CM_CONFIG_DIR is set" in said[0] and "~/.config/claude-launcher" in said[0]
    custom.mkdir()  # once the chosen home exists: quiet
    assert config.migrate_legacy(said.append) is False and len(said) == 1


def test_failed_migration_stops_cm_and_is_retried(bare_home, monkeypatch, capsys):
    import shutil

    _legacy(bare_home)
    real = shutil.copytree

    def broken(*a, **k):
        raise shutil.Error([("a", "b", "permission denied")])

    monkeypatch.setattr(shutil, "copytree", broken)
    assert cli.main(["setup", "init"]) == 1  # would otherwise create the new home without the old state
    assert "could not migrate" in capsys.readouterr().err
    assert not (bare_home / ".claude-launcher").exists()
    monkeypatch.setattr(shutil, "copytree", real)
    assert cli.main(["setup", "init"]) == 0
    assert (bare_home / ".claude-launcher" / "profiles" / "memory+gdrive.json").is_file()


def test_single_folder_string_in_migration_and_config(bare_home):
    _legacy(bare_home, {"mcp_folders": "~/mine"})
    config.migrate_legacy(lambda m: None)
    cfg = json.loads((bare_home / ".claude-launcher" / "config.json").read_text())
    assert cfg["mcp_folders"] == ["~/.claude-launcher/mcps", "~/mine"]
    assert config.mcp_folders({"mcp_folders": "~/mine"}) == [bare_home / "mine"]
    assert config.mcp_folders({"mcp_folders": 5}) == []  # unusable value: no crash
