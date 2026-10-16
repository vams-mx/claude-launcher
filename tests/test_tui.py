import asyncio

from textual.widgets import Input, RadioSet, SelectionList, Static

from claude_launcher import profiles
from claude_launcher.plan import Selection
from claude_launcher.tui import LauncherApp


def drive(app, steps):
    async def go():
        async with app.run_test(size=(160, 60)) as pilot:
            await pilot.pause()
            await steps(app, pilot)

    asyncio.run(go())
    return app.return_value


def test_tick_agentmemory_and_gdrive_then_enter(catalog):
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        local = app.query_one("#local", SelectionList)
        local.focus()
        names = [s.name for s in catalog.local]
        for target in ("agentmemory", "gdrive"):
            local.highlighted = names.index(target)
            await pilot.press("space")
        await pilot.pause()
        preview = app.query_one("#preview", Static).render()
        assert "agentmemory, gdrive" in str(preview)
        await pilot.press("enter")

    result = drive(app, steps)
    assert result[0] == "launch"
    sel = result[1]
    assert sel.local == ["agentmemory", "gdrive"]
    assert sel.connectors == [] and sel.plugins == [] and sel.cmux is False
    assert sel.model == "default" and sel.permissions == "standard" and sel.session == "new"


def test_escape_quits(catalog):
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await pilot.press("escape")

    assert drive(app, steps) is None


def test_skip_permissions_flagged_and_connector_mode(catalog):
    app = LauncherApp(catalog, Selection(permissions="skip", connectors=["claude.ai Slack"]), claude_bin="claude")

    async def steps(app, pilot):
        text = str(app.query_one("#preview", Static).render())
        assert "dangerously-skip-permissions" in text
        assert "deny-list" in text
        await pilot.press("escape")

    drive(app, steps)


def test_initial_selection_and_radio_state(catalog):
    app = LauncherApp(catalog, Selection(local=["gdrive"], model="haiku", session="resume", resume_id="abc"),
                      claude_bin="claude")

    async def steps(app, pilot):
        assert app.query_one("#local", SelectionList).selected == ["gdrive"]
        assert app.query_one("#model", RadioSet).pressed_button.name == "haiku"
        assert app.query_one("#resume_id", Input).display
        app.action_do_launch()

    result = drive(app, steps)
    assert result[1].resume_id == "abc" and result[1].model == "haiku"


def test_save_profile_from_screen(catalog):
    app = LauncherApp(catalog, Selection(local=["agentmemory"]), claude_bin="claude")

    async def steps(app, pilot):
        app.query_one("#profile_name", Input).value = "mem"
        app.query_one("#profile_name", Input).focus()
        await pilot.press("enter")  # Enter in the name box saves instead of launching
        await pilot.pause()
        assert profiles.list_profiles() == ["mem"]
        await pilot.press("escape")

    assert drive(app, steps) is None
    assert profiles.load("mem").local == ["agentmemory"]


def test_load_profile_applies(catalog):
    profiles.save("both", Selection(local=["agentmemory", "gdrive"], model="sonnet"))
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        from textual.widgets import Select

        app.query_one("#profile", Select).value = "both"
        await pilot.pause()
        await pilot.pause()
        app.action_do_launch()

    result = drive(app, steps)
    assert result[1].local == ["agentmemory", "gdrive"] and result[1].model == "sonnet"


def test_unavailable_cmux_blocks(catalog):
    catalog.cmux.available = False
    catalog.cmux.reason = "not installed"
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        cm = app.query_one("#cmux", SelectionList)
        assert cm.get_option_at_index(0).disabled
        await pilot.press("escape")

    drive(app, steps)


def test_enter_in_profile_dropdown_selects_instead_of_launching(catalog):
    from textual.widgets import Select

    profiles.save("a", Selection(local=["gdrive"]))
    profiles.save("b", Selection(local=["agentmemory"], model="sonnet"))
    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        sel = app.query_one("#profile", Select)
        sel.focus()
        await pilot.press("enter")  # closed Select: Enter opens it
        await pilot.pause()
        assert app.return_value is None and sel.expanded
        await pilot.press("down", "down", "enter")  # blank, a, b -> pick "b"
        await pilot.pause()
        await pilot.pause()
        assert app.return_value is None and not sel.expanded
        assert sel.value == "b" and app.profile_name == "b"
        app.action_do_launch()

    result = drive(app, steps)
    assert result[1].local == ["agentmemory"] and result[1].model == "sonnet"


def test_unverified_connector_from_profile_survives_the_screen(catalog):
    # "claude.ai Gmail" is only in claudeAiMcpEverConnected (unverified) while verified connectors exist
    assert not next(c for c in catalog.connectors if c.short == "Gmail").verified
    profiles.save("mail", Selection(connectors=["claude.ai Gmail", "claude.ai Slack"]))
    app = LauncherApp(catalog, Selection(connectors=["claude.ai Gmail"]), claude_bin="claude")

    async def steps(app, pilot):
        from textual.widgets import Select

        assert app.query_one("#connectors", SelectionList).selected == ["claude.ai Gmail"]
        app.query_one("#profile", Select).value = "mail"  # loading a profile later keeps it too
        await pilot.pause()
        await pilot.pause()
        app.action_do_launch()

    result = drive(app, steps)
    assert set(result[1].connectors) == {"claude.ai Gmail", "claude.ai Slack"}


def test_refresh_reads_connectors_off_the_ui_thread_and_updates_the_catalog_in_place(catalog, monkeypatch):
    import json
    import threading

    import claude_launcher.tui as tui
    from claude_launcher.catalog import connectors_cache_path, discover_connectors

    def fake_refresh(claude_bin):
        connectors_cache_path().write_text(json.dumps({"refreshed_at": "2026-10-15T12:00:00+00:00", "connectors": [
            {"name": "claude.ai Slack", "status": "Connected"}, {"name": "claude.ai Notion", "status": "Connected"}]}))

    threads = []

    def spy(warnings):
        threads.append(threading.current_thread())
        return discover_connectors(warnings)

    monkeypatch.setattr(tui, "refresh_connectors", fake_refresh)
    monkeypatch.setattr(tui, "discover_connectors", spy)
    local_before = catalog.local
    app = LauncherApp(catalog, Selection(connectors=["claude.ai Slack"]), claude_bin="claude")

    async def steps(app, pilot):
        app.action_refresh_connectors()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.pause()
        rows = app.query_one("#connectors", SelectionList)
        values = [rows.get_option_at_index(i).value for i in range(rows.option_count)]
        assert "claude.ai Notion" in values and rows.selected == ["claude.ai Slack"]
        rows.select("claude.ai Notion")
        await pilot.pause()
        app.action_do_launch()

    result = drive(app, steps)
    assert threads and threads[0] is not threading.main_thread()
    assert app.cat is catalog and catalog.local is local_before  # same Catalog the CLI launches with
    assert "claude.ai Notion" in {c.name for c in catalog.connectors}
    assert catalog.connectors_refreshed_at == "2026-10-15T12:00:00+00:00"
    assert result[1].connectors == ["claude.ai Notion", "claude.ai Slack"]  # catalog order


def test_textual_floor_has_select_null():
    # tui.py uses Select.NULL, which replaced Select.BLANK in Textual 8.0.0 (verified against 7.5.0 and 8.0.0)
    import re
    import tomllib
    from pathlib import Path

    from textual.widgets import Select

    doc = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    spec = next(d for d in doc["project"]["dependencies"] if d.startswith("textual"))
    floor = tuple(int(x) for x in re.search(r">=\s*([\d.]+)", spec).group(1).split("."))
    assert floor >= (8, 0) and hasattr(Select, "NULL")


def test_sober_default_theme_and_ctrl_t_cycles_and_saves(catalog):
    from claude_launcher import config, themes

    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        assert app.theme == themes.DEFAULT_THEME == "cm-graphite"
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.theme == themes.THEME_CYCLE[1]
        assert config.load_config()["theme"] == themes.THEME_CYCLE[1]
        await pilot.press("escape")

    drive(app, steps)
    assert LauncherApp(catalog, Selection(), claude_bin="claude").theme == themes.THEME_CYCLE[1]  # remembered


def test_unknown_configured_theme_falls_back_with_a_note(catalog):
    from claude_launcher import config, themes

    config.save_config({"theme": "nope"})
    app = LauncherApp(catalog, Selection(), claude_bin="claude")
    assert app.theme == themes.DEFAULT_THEME and any("nope" in n for n in app.notes)


def test_theme_cycle_wraps_and_only_lists_real_themes():
    from textual.app import App

    from claude_launcher import themes

    app = App()
    for t in themes.CUSTOM:
        app.register_theme(t)
    assert set(themes.THEME_CYCLE) <= set(app.available_themes)
    assert themes.next_theme(themes.THEME_CYCLE[-1]) == themes.THEME_CYCLE[0]
    assert themes.next_theme("not-in-cycle") == themes.THEME_CYCLE[0]


def test_theme_change_recolours_rows_and_keeps_the_selection(catalog):
    from claude_launcher import themes

    app = LauncherApp(catalog, Selection(local=["gdrive"]), claude_bin="claude")

    async def steps(app, pilot):
        assert app._palette()["cmd"] == themes.GRAPHITE.primary
        app.theme = themes.PAPER.name
        await pilot.pause()
        assert app._palette()["cmd"] == themes.PAPER.primary
        assert app.query_one("#local", SelectionList).selected == ["gdrive"]
        app.action_do_launch()

    assert drive(app, steps)[1].local == ["gdrive"]


def test_theme_set_directly_like_the_palette_is_saved(catalog):
    from claude_launcher import config

    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        app.theme = "nord"  # what Ctrl+P → Theme does
        await pilot.pause()
        assert config.load_config()["theme"] == "nord"
        await pilot.press("escape")

    drive(app, steps)


def test_enter_in_the_command_palette_never_launches(catalog):
    from claude_launcher import config

    app = LauncherApp(catalog, Selection(), claude_bin="claude")

    async def steps(app, pilot):
        await pilot.press("ctrl+p")
        await pilot.pause()
        for ch in "theme":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.pause()
        await pilot.press("enter")  # opens the theme list
        await pilot.pause()
        await pilot.pause()
        assert app.return_value is None and len(app.screen_stack) > 1
        for ch in "cm-sand":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.pause()
        await pilot.press("enter")  # picks the theme
        await pilot.pause()
        await pilot.pause()
        assert app.return_value is None and app.theme == "cm-sand"
        assert config.load_config()["theme"] == "cm-sand"
        await pilot.press("escape")

    drive(app, steps)


def test_non_string_theme_in_config_falls_back_instead_of_crashing(catalog):
    from claude_launcher import config, themes

    for bad in (["cm-paper"], {"x": 1}, 7):
        config.save_config({"theme": bad})
        app = LauncherApp(catalog, Selection(), claude_bin="claude")
        assert app.theme == themes.DEFAULT_THEME and any("unknown theme" in n for n in app.notes)


def _contrast(a: str, b: str) -> float:
    def lum(h: str) -> float:
        h = h.lstrip("#")
        ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        ch = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]

    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_cm_themes_keep_text_readable():
    from claude_launcher import themes

    for t in themes.CUSTOM:
        for role in ("foreground", "primary", "secondary", "accent", "warning", "error", "success"):
            for bg in ("background", "surface", "panel"):
                ratio = _contrast(getattr(t, role), getattr(t, bg))
                assert ratio >= 4.5, f"{t.name}: {role} on {bg} is {ratio:.2f}:1"
