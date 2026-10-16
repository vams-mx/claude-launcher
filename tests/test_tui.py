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
