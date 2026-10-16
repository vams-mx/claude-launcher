"""The interactive screen (Textual).

One screen: grouped checklists on the left, model / permissions / session /
profiles on the right, and a live preview (exact command, isolation mode,
estimated context cost, warnings) at the bottom. Space toggles, Enter launches.
"""

from __future__ import annotations

from rich.text import Text
from textual import on, work
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.timer import Timer
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button, Footer, Header, Input, Label, RadioButton, RadioSet, Select, SelectionList, Static,
)
from textual.widgets.selection_list import Selection as Item

from . import compat, health, profiles
from . import config as config_mod
from . import themes
from . import costs as costs_mod
from .catalog import Catalog, Connector, discover_connectors, refresh_connectors
from .plan import PlanError, Selection
from .preview import preview_plan, render

MODEL_LABELS = {"default": "Default", "sonnet": "Sonnet", "opus": "Opus", "haiku": "Haiku"}
PERM_LABELS = {"standard": "Standard (ask before acting)", "skip": "⚠ Skip permissions (--dangerously-skip-permissions)"}
SESSION_LABELS = {"new": "New session", "continue": "Continue last (--continue)", "resume": "Resume… (native picker)"}

def _edge(options: list, last: bool) -> int | None:
    """Index of the first (or last) enabled option, or None if none is enabled."""
    idx = [i for i, o in enumerate(options) if not o.disabled]
    return (idx[-1] if last else idx[0]) if idx else None


class FlowSelectionList(SelectionList):
    """Up/Down past the first/last row moves focus to the previous/next box."""

    BINDINGS = [Binding("down", "flow(1)", show=False), Binding("up", "flow(-1)", show=False)]

    def action_flow(self, direction: int) -> None:
        edge = _edge(self.options, last=direction > 0)
        if self.highlighted is None or self.highlighted == edge:
            self.app.flow_focus(direction)
        elif direction > 0:
            self.action_cursor_down()
        else:
            self.action_cursor_up()

    def enter_from(self, direction: int) -> None:
        self.highlighted = _edge(self.options, last=direction < 0)


class FlowRadioSet(RadioSet):
    """Like FlowSelectionList, for radio groups (Left/Right still cycle inside).

    Uses RadioSet's private `_selected` (the highlighted button), which Textual 8
    has; pyproject caps textual below 9 and test_arrows_flow_from_box_to_box fails
    loudly if it changes."""

    BINDINGS = [Binding("down", "flow(1)", show=False), Binding("up", "flow(-1)", show=False)]

    def action_flow(self, direction: int) -> None:
        buttons = list(self.query(RadioButton))
        edge = _edge(buttons, last=direction > 0)
        if self._selected is None or self._selected == edge:
            self.app.flow_focus(direction)
        elif direction > 0:
            self.action_next_button()
        else:
            self.action_previous_button()

    def enter_from(self, direction: int) -> None:
        self._selected = _edge(list(self.query(RadioButton)), last=direction < 0)


class FlowColumn(VerticalScroll):
    """A scrolling column whose Up/Down move between boxes instead of scrolling it.
    (Focus changes scroll the focused box into view; PageUp/PageDown still scroll.)"""

    BINDINGS = [Binding("down", "app.flow(1)", show=False), Binding("up", "app.flow(-1)", show=False)]


class FlowSelect(Select):
    """Up/Down move between boxes; Enter or Space opens the list."""

    BINDINGS = [Binding("down", "flow(1)", show=False), Binding("up", "flow(-1)", show=False)]

    def action_flow(self, direction: int) -> None:
        self.app.flow_focus(direction)


CSS = """
Screen { layout: vertical; }
#body { height: 1fr; }
#left { width: 3fr; padding: 0 1; }
#right { width: 2fr; padding: 0 1; border-left: tall $panel; }
.group { margin-top: 1; text-style: bold; color: $accent; }
.note { color: $text-muted; padding: 0 1; }
SelectionList { height: auto; max-height: 12; border: round $panel; }
SelectionList:focus { border: round $accent; background: $boost; }
RadioSet { width: 100%; border: round $panel; }
RadioSet:focus { border: round $accent; background: $boost; }
#perm RadioButton.danger { color: $error; }
#resume_id { margin-top: 0; }
#profile_row { height: auto; }
#profile_row Input { width: 1fr; }
#profile_row Button { min-width: 8; margin-left: 1; }
#preview { height: auto; max-height: 14; border: round $primary; padding: 0 1; overflow-y: auto; }
#preview.error { border: round $error; }
#hint { color: $text-muted; padding: 0 1; height: 1; }
"""


class LauncherApp(App[tuple[str, Selection] | None]):
    TITLE = "cm · Claude Code launcher"
    CSS = CSS
    BINDINGS = [
        Binding("enter", "launch", "Launch", priority=True),
        Binding("escape", "quit_app", "Quit"),
        Binding("ctrl+s", "focus_save", "Save profile"),
        Binding("ctrl+r", "refresh_connectors", "Refresh connectors"),
        Binding("ctrl+n", "select_none", "Clear all"),
        Binding("ctrl+t", "cycle_theme", "Theme"),
        Binding("down", "flow(1)", show=False),
        Binding("up", "flow(-1)", show=False),
    ]

    def __init__(self, cat: Catalog, initial: Selection, *, claude_bin: str, profile: str | None = None,
                 notes: list[str] | None = None, auto_refresh: bool = False) -> None:
        super().__init__()
        self.cat = cat
        self.sel = initial
        self.claude_bin = claude_bin
        self.costs = costs_mod.load()
        self.profile_name = profile
        self.notes = notes or []
        self.health: dict[str, str] = {}
        self.plan_ok = True
        self.refresh_on_start = auto_refresh
        self._armed_delete: str | None = None  # profile name armed by a first Delete press
        self._disarm_timer: Timer | None = None
        for t in themes.CUSTOM:
            self.register_theme(t)
        self.theme = self._configured_theme()

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            with FlowColumn(id="left") as left:
                left.can_focus = False  # arrows/Tab go box to box, not to the column
                yield Label("Local MCP servers", classes="group")
                yield FlowSelectionList(*self._local_items(), id="local")
                yield Label("claude.ai connectors", classes="group")
                yield FlowSelectionList(*self._connector_items(), id="connectors")
                yield Static(self._connector_note(), classes="note", id="conn_note")
                yield Label("Plugins", classes="group")
                yield FlowSelectionList(*self._plugin_items(), id="plugins")
                # hidden entirely where cmux isn't installed (e.g. Linux)
                for w in (Label("cmux computer use", classes="group"), FlowSelectionList(*self._cmux_items(), id="cmux")):
                    w.display = self.cat.cmux.installed
                    yield w
            with FlowColumn(id="right") as right:
                right.can_focus = False
                yield Label("Model", classes="group")
                with FlowRadioSet(id="model"):
                    for k, v in MODEL_LABELS.items():
                        yield RadioButton(v, value=self.sel.model == k, name=k)
                yield Label("Permissions", classes="group")
                with FlowRadioSet(id="perm"):
                    for k, v in PERM_LABELS.items():
                        yield RadioButton(v, value=self.sel.permissions == k, name=k,
                                          classes="danger" if k == "skip" else "")
                yield Label("Session", classes="group")
                with FlowRadioSet(id="session"):
                    for k, v in SESSION_LABELS.items():
                        yield RadioButton(v, value=self.sel.session == k, name=k)
                yield Input(placeholder="resume: session id (blank = picker)", id="resume_id",
                            value=self.sel.resume_id or "")
                yield Label("Profile", classes="group")
                yield FlowSelect([(p, p) for p in profiles.list_profiles()], prompt="load profile…",
                             id="profile", allow_blank=True,
                             value=self.profile_name if self.profile_name in profiles.list_profiles() else Select.NULL)
                with Horizontal(id="profile_row"):
                    yield Input(placeholder="name, e.g. memory+gdrive", id="profile_name",
                                value=self.profile_name or "")
                    yield Button("Save", id="save", variant="primary")
                    yield Button("Default", id="default")
                    yield Button("Delete", id="delete", variant="error")
                yield Static(self._default_line(), classes="note", id="default_line")
        yield Static(id="preview")
        yield Static("↑↓ move through all boxes · Space toggle · Enter launch · Ctrl+S save profile · Ctrl+R refresh connectors · Ctrl+T theme · Esc quit",
                     id="hint")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = str(self.cat.cwd)
        self._sync_resume_visibility()
        self._update_preview()
        self.query_one("#local", SelectionList).focus()
        for n in self.notes:
            self.notify(n, severity="warning", timeout=6)
        self._check_health()
        self._check_version()
        self.theme_changed_signal.subscribe(self, self._on_theme_changed)
        if self.refresh_on_start:
            self.action_refresh_connectors()

    # ------------------------------------------------------------------ items
    def _palette(self) -> dict[str, str]:
        """Colour roles for item and preview text, taken from the current theme."""
        th = self.current_theme
        return {"cmd": th.primary, "ok": th.success or "green", "warn": th.warning or "yellow",
                "bad": th.error or "red", "cost": th.secondary or th.primary}

    def _restyle(self) -> None:
        """Rebuild the rows and the preview so their colours follow a theme change."""
        self.sel = self._read_selection()
        for wid, items in (("local", self._local_items), ("connectors", self._connector_items),
                           ("plugins", self._plugin_items), ("cmux", self._cmux_items)):
            sl = self.query_one(f"#{wid}", SelectionList)
            highlighted = sl.highlighted
            sl.clear_options()
            sl.add_options(items())
            if highlighted is not None:
                sl.highlighted = highlighted
        self.query_one("#conn_note", Static).update(self._connector_note())
        self._update_preview()

    def _cost(self, key: str) -> Text:
        return Text("  " + costs_mod.fmt(self.costs.get(key)), style=self._palette()["cost"] if key in self.costs else "dim")

    def _local_items(self) -> list[Item]:
        items = []
        for s in self.cat.local:
            t = Text(s.name, style="bold")
            t.append_text(self._cost(s.key))
            if s.settings_path:
                t.append("  +hooks", style=self._palette()["ok"])
            if s.name in self.health:
                h = self.health[s.name]
                t.append(f"  {h}", style=self._palette()["ok" if h == "backend up" else "bad"])
            t.append(f"  {s.summary()}", style="dim")
            t.append(f"  [{s.source}]", style="dim italic")
            items.append(Item(t, s.name, s.name in self.sel.local))
        if not items:
            items.append(Item(Text("no local MCP servers found", style="dim"), "__none__", False, disabled=True))
        return items

    def _connector_items(self) -> list[Item]:
        items = []
        # Once `claude mcp list` verified some, unverified names are listed only when selected (e.g. by a
        # profile); without a row they would be silently dropped from the launch.
        shown = [c for c in self.cat.connectors if c.verified or c.name in self.sel.connectors]
        if not any(c.verified for c in self.cat.connectors):
            shown = self.cat.connectors
        for c in shown:
            t = Text(c.short, style="bold" if c.verified else "dim")
            t.append_text(self._cost(c.key))
            if c.status:
                t.append(f"  {c.status}", style="dim" if c.verified else self._palette()["warn"])
            items.append(Item(t, c.name, c.name in self.sel.connectors))
        if not items:
            items.append(Item(Text("none known: press Ctrl+R to refresh", style="dim"), "__none__", False, disabled=True))
        return items

    def _connector_note(self) -> Text:
        t = Text("Per-connector selection works via deniedMcpServers (verified). Selecting any connector "
                 "switches from --strict-mcp-config to name-based blocking of everything else.", style="dim")
        if self.cat.connectors_refreshed_at:
            t.append(f"  List refreshed {self.cat.connectors_refreshed_at}.", style="dim")
        else:
            t.append("  List not refreshed yet (Ctrl+R).", style=self._palette()["warn"])
        return t

    def _plugin_items(self) -> list[Item]:
        items = []
        for p in self.cat.plugins:
            t = Text(p.name, style="bold")
            t.append_text(self._cost(p.key))
            if p.mcp_servers:
                t.append(f"  +{len(p.mcp_servers)} MCP", style=self._palette()["warn"])
            t.append(f"  {p.id.split('@', 1)[-1]}  [{'/'.join(p.enabled_in)}]", style="dim")
            items.append(Item(t, p.id, p.id in self.sel.plugins))
        if not items:
            items.append(Item(Text("no enabled plugins", style="dim"), "__none__", False, disabled=True))
        return items

    def _cmux_items(self) -> list[Item]:
        st = self.cat.cmux
        t = Text("cmux-cua", style="bold" if st.available else "dim")
        t.append_text(self._cost("cmux"))
        t.append("  computer use via cmux's bundled helper", style="dim")
        if st.reason:
            t.append(f"  ({st.reason})", style=self._palette()["warn" if st.available else "bad"])
        return [Item(t, "cmux", self.sel.cmux and st.available, disabled=not st.available)]

    def _default_line(self) -> Text:
        d = profiles.get_default()
        return Text(f"default profile: {d}" if d else "no default profile", style="dim")

    # ------------------------------------------------------------------ state
    def _read_selection(self) -> Selection:
        def vals(wid: str) -> list[str]:
            return [v for v in self.query_one(f"#{wid}", SelectionList).selected if v != "__none__"]

        def radio(wid: str, fallback: str) -> str:
            rs = self.query_one(f"#{wid}", RadioSet)
            btn = rs.pressed_button
            return btn.name if btn and btn.name else fallback

        local = vals("local")
        conns = vals("connectors")
        plugins = vals("plugins")
        # keep catalog order for stable previews
        return Selection(
            local=[s.name for s in self.cat.local if s.name in local],
            connectors=[c.name for c in self.cat.connectors if c.name in conns],
            plugins=[p.id for p in self.cat.plugins if p.id in plugins],
            cmux="cmux" in vals("cmux"),
            model=radio("model", "default"),
            permissions=radio("perm", "standard"),
            session=radio("session", "new"),
            resume_id=(self.query_one("#resume_id", Input).value.strip() or None),
            extra_args=list(self.sel.extra_args),
        )

    def _update_preview(self) -> None:
        self.sel = self._read_selection()
        if self.sel.session != "resume":
            self.sel.resume_id = None
        pv = self.query_one("#preview", Static)
        try:
            plan = preview_plan(self.cat, self.sel, self.claude_bin)
        except PlanError as exc:
            self.plan_ok = False
            pv.add_class("error")
            pv.update(Text(f"Cannot launch: {exc}", style=f"bold {self._palette()['bad']}"))
            return
        self.plan_ok = True
        pv.remove_class("error")
        pv.update(render(self.cat, self.sel, plan, self.costs, self._palette()))

    def _sync_resume_visibility(self) -> None:
        self.query_one("#resume_id", Input).display = self._read_selection().session == "resume"

    def _rebuild_connectors(self) -> None:
        sl = self.query_one("#connectors", SelectionList)
        sl.clear_options()
        sl.add_options(self._connector_items())  # ticks follow self.sel.connectors

    def _apply(self, sel: Selection) -> None:
        self.sel = sel
        self._rebuild_connectors()  # the profile may use an unverified connector that has no row yet
        for wid, values in (("local", sel.local), ("plugins", sel.plugins),
                            ("cmux", ["cmux"] if sel.cmux else [])):
            sl = self.query_one(f"#{wid}", SelectionList)
            sl.deselect_all()
            for v in values:
                try:
                    sl.select(v)
                except Exception:  # value not present in this catalog
                    pass
        for wid, value in (("model", sel.model), ("perm", sel.permissions), ("session", sel.session)):
            for b in self.query_one(f"#{wid}", RadioSet).query(RadioButton):
                if b.name == value:
                    b.value = True
        self._sync_resume_visibility()
        self._update_preview()

    # ------------------------------------------------------------------ events
    @on(SelectionList.SelectedChanged)
    def _sl_changed(self) -> None:
        self._update_preview()

    @on(RadioSet.Changed)
    def _rs_changed(self) -> None:
        self._sync_resume_visibility()
        self._update_preview()

    @on(Input.Changed, "#resume_id")
    def _resume_changed(self) -> None:
        self._update_preview()

    @on(Select.Changed, "#profile")
    def _profile_selected(self, event: Select.Changed) -> None:
        if event.value is Select.NULL or event.value == self.profile_name:
            return
        name = str(event.value)
        try:
            loaded, notes = profiles.reconcile(profiles.load(name), self.cat)
        except profiles.ProfileError as exc:
            self.notify(str(exc), severity="error")
            return
        loaded.extra_args = list(self.sel.extra_args) or loaded.extra_args
        self.profile_name = name
        self.query_one("#profile_name", Input).value = name
        self._apply(loaded)
        self.notify(f"loaded profile {name}")
        for n in notes:
            self.notify(n, severity="warning")

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        name = self.query_one("#profile_name", Input).value.strip()
        try:
            profiles.save(name, self._read_selection())
        except profiles.ProfileError as exc:
            self.notify(str(exc), severity="error")
            return
        self.profile_name = name
        self._refresh_profile_select(name)
        self.notify(f"saved profile {name}")

    @on(Button.Pressed, "#default")
    def _make_default(self) -> None:
        name = self.query_one("#profile_name", Input).value.strip()
        try:
            if name not in profiles.list_profiles():
                profiles.save(name, self._read_selection())
                self._refresh_profile_select(name)
            profiles.set_default(name)
        except profiles.ProfileError as exc:
            self.notify(str(exc), severity="error")
            return
        self.query_one("#default_line", Static).update(self._default_line())
        self.notify(f"{name} is now the default profile")

    @on(Button.Pressed, "#delete")
    def _delete(self) -> None:
        name = self.query_one("#profile_name", Input).value.strip()
        btn = self.query_one("#delete", Button)
        if not name:
            self.notify("type a profile name to delete", severity="warning")
            return
        if self._armed_delete != name:  # first press only arms it
            self._armed_delete = name
            btn.label = "Confirm?"
            # shown under the buttons, not as a toast: a toast would sit on top of Delete
            self.query_one("#default_line", Static).update(
                Text(f"press Delete again within 4 s to delete profile {name!r}", style=self._palette()["warn"]))
            if self._disarm_timer is not None:
                self._disarm_timer.stop()  # an older arm must not cut this one short
            self._disarm_timer = self.set_timer(4, self._disarm_delete)
            return
        self._disarm_delete()
        try:
            profiles.delete(name)
        except profiles.ProfileError as exc:
            self.notify(str(exc), severity="error")
            return
        self.profile_name = None
        self._refresh_profile_select(None)
        self.query_one("#default_line", Static).update(self._default_line())
        self.notify(f"deleted profile {name}")

    def _disarm_delete(self) -> None:
        if self._disarm_timer is not None:
            self._disarm_timer.stop()
            self._disarm_timer = None
        self._armed_delete = None
        self.query_one("#delete", Button).label = "Delete"
        self.query_one("#default_line", Static).update(self._default_line())

    def _refresh_profile_select(self, value: str | None) -> None:
        sel = self.query_one("#profile", Select)
        sel.set_options([(p, p) for p in profiles.list_profiles()])
        if value:
            sel.value = value

    # ------------------------------------------------------------------ actions
    def action_launch(self) -> None:
        if len(self.screen_stack) > 1:
            # A modal screen (e.g. the Ctrl+P command palette) is on top: Enter belongs to it.
            raise SkipAction()
        focused = self.focused
        if isinstance(focused, Input):
            if focused.id == "profile_name":
                self._save()
            else:
                self._update_preview()
                self.action_do_launch()
            return
        if isinstance(focused, Button):
            focused.press()
            return
        if focused is not None and any(isinstance(n, Select) for n in focused.ancestors_with_self):
            # A closed Select opens on Enter; an open one has focused its overlay, which picks the option.
            raise SkipAction()
        self.action_do_launch()

    def action_do_launch(self) -> None:
        self._update_preview()
        if not self.plan_ok:
            self.bell()
            self.notify("fix the error shown in the preview first", severity="error")
            return
        self.exit(("launch", self.sel))

    def action_quit_app(self) -> None:
        self.exit(None)

    def action_focus_save(self) -> None:
        self.query_one("#profile_name", Input).focus()

    def _configured_theme(self) -> str:
        name = config_mod.load_config().get("theme") or themes.DEFAULT_THEME
        if not isinstance(name, str) or name not in self.available_themes:
            self.notes.append(f"unknown theme {name!r} in config.json; using {themes.DEFAULT_THEME}")
            return themes.DEFAULT_THEME
        return name

    def flow_focus(self, direction: int) -> None:
        """Move focus to the next/previous box, landing on its first/last row.
        Stops at the ends instead of wrapping (Up at the top must not land on Delete)."""
        chain = self.screen.focus_chain
        if chain and self.focused is (chain[-1] if direction > 0 else chain[0]):
            return
        if direction > 0:
            self.screen.focus_next()
        else:
            self.screen.focus_previous()
        if isinstance(self.focused, (FlowSelectionList, FlowRadioSet)):
            self.focused.enter_from(direction)

    def action_flow(self, direction: int) -> None:
        if len(self.screen_stack) > 1:
            raise SkipAction()  # a modal (palette, Select overlay) owns the arrows
        self.flow_focus(direction)

    def action_cycle_theme(self) -> None:
        self.theme = themes.next_theme(self.theme)

    def _on_theme_changed(self, theme) -> None:
        """Any theme change (Ctrl+T or the command palette) recolours the rows and is saved."""
        self._restyle()
        try:
            config_mod.save_config({"theme": theme.name})
        except (OSError, config_mod.ConfigError) as exc:
            self.notify(f"theme {theme.name} (not saved: {exc})", severity="warning")
            return
        self.notify(f"theme: {theme.name}", timeout=2)

    def action_select_none(self) -> None:
        for wid in ("local", "connectors", "plugins", "cmux"):
            self.query_one(f"#{wid}", SelectionList).deselect_all()

    @work(thread=True, exclusive=True, group="refresh")
    def action_refresh_connectors(self) -> None:
        self.call_from_thread(self.notify, "refreshing claude.ai connectors (claude mcp list)…")
        warnings: list[str] = []
        try:
            refresh_connectors(self.claude_bin)
            conns, refreshed_at = discover_connectors(warnings)  # file I/O stays off the UI thread
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.call_from_thread(self.notify, f"refresh failed: {exc}", severity="error")
            return
        self.call_from_thread(self._reload_connectors, conns, refreshed_at, warnings)

    def _reload_connectors(self, conns: list[Connector], refreshed_at: str | None, warnings: list[str]) -> None:
        self.sel = self._read_selection()
        # Swap only the connectors, in place: local/plugin rows stay consistent with self.cat, and the
        # caller launches with this same Catalog object.
        self.cat.connectors, self.cat.connectors_refreshed_at = conns, refreshed_at
        self._rebuild_connectors()
        self.query_one("#conn_note", Static).update(self._connector_note())
        for w in warnings:
            self.notify(w, severity="warning")
        self._update_preview()
        self.notify(f"{len(self.cat.connectors)} connectors")

    @work(thread=True, exclusive=True, group="version")
    def _check_version(self) -> None:
        # `claude --version` is a process (up to 5 s): keep it off the startup path
        if w := compat.check(self.claude_bin):
            self.call_from_thread(self.notify, w, severity="warning", timeout=6)

    @work(thread=True, exclusive=True, group="health")
    def _check_health(self) -> None:
        results = {s.name: h for s in self.cat.local if (h := health.check(s))}
        if results:
            self.call_from_thread(self._apply_health, results)

    def _apply_health(self, results: dict[str, str]) -> None:
        self.health = results
        keep = self._read_selection()
        sl = self.query_one("#local", SelectionList)
        highlighted = sl.highlighted
        sl.clear_options()
        self.sel = keep
        sl.add_options(self._local_items())
        if highlighted is not None:
            sl.highlighted = highlighted


def run_tui(cat: Catalog, initial: Selection, *, claude_bin: str, profile: str | None = None,
            notes: list[str] | None = None, auto_refresh: bool = False) -> Selection | None:
    app = LauncherApp(cat, initial, claude_bin=claude_bin, profile=profile, notes=notes, auto_refresh=auto_refresh)
    result = app.run()
    if result and result[0] == "launch":
        return result[1]
    return None
