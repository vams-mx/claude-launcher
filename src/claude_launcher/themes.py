"""Colour themes for the interactive screen.

cm ships sober themes of its own (dark: graphite, slate, moss, ink; light: paper,
sand, mist) and can use any built-in Textual theme. The choice is stored as
`theme` in config.json, whether it comes from Ctrl+T (cycles THEME_CYCLE) or the
command palette (Ctrl+P → Theme).
"""

from __future__ import annotations

from textual.theme import Theme


def _theme(name: str, dark: bool, **colours: str) -> Theme:
    return Theme(name=name, dark=dark, **colours)


# Every text colour keeps a WCAG contrast of at least 4.5:1 against background,
# surface and panel (tests/test_tui.py checks it).
GRAPHITE = _theme(  # dark, neutral greys with a steel-blue accent
    "cm-graphite", True,
    primary="#8FA3B8", secondary="#929DA8", accent="#A9B8C6", foreground="#D4D7DB",
    background="#1B1D20", surface="#222529", panel="#2C3035",
    warning="#C9A86A", error="#CC8A8A", success="#8FAF8A",
)
SLATE = _theme(  # dark, cool blue-grey
    "cm-slate", True,
    primary="#8EA8C3", secondary="#96A3B1", accent="#A7BCD1", foreground="#D3DAE2",
    background="#1A1F26", surface="#212831", panel="#2A323D",
    warning="#CDB078", error="#D08F8F", success="#93B79A",
)
MOSS = _theme(  # dark, warm olive-grey
    "cm-moss", True,
    primary="#A3B18A", secondary="#9EA394", accent="#B9C59F", foreground="#D6D8CC",
    background="#1C1E1A", surface="#23261F", panel="#2D3128",
    warning="#CFB06E", error="#CF8F7F", success="#9DBB8C",
)
INK = _theme(  # dark, almost monochrome
    "cm-ink", True,
    primary="#B0B0B0", secondary="#9A9A9A", accent="#C8C8C8", foreground="#DADADA",
    background="#121212", surface="#1A1A1A", panel="#242424",
    warning="#C8B07A", error="#C98C8C", success="#9DBA9D",
)
PAPER = _theme(  # light, off-white paper with slate accents
    "cm-paper", False,
    primary="#4A5A6A", secondary="#585E65", accent="#3E5568", foreground="#2B2D30",
    background="#F4F2EE", surface="#ECE9E3", panel="#DEDAD2",
    warning="#7A5714", error="#9E3B3B", success="#3F6639",
)
SAND = _theme(  # light, warm beige
    "cm-sand", False,
    primary="#5B4E3A", secondary="#5F584D", accent="#6B5A3E", foreground="#2F2A22",
    background="#F3EEE4", surface="#EBE5D8", panel="#DED6C5",
    warning="#755313", error="#963A2E", success="#3F6639",
)
MIST = _theme(  # light, cool grey-blue
    "cm-mist", False,
    primary="#3D5470", secondary="#535E6B", accent="#34506E", foreground="#1F2630",
    background="#EEF1F4", surface="#E5E9EE", panel="#D7DDE4",
    warning="#70521A", error="#933C3C", success="#3B6440",
)

CUSTOM = (GRAPHITE, SLATE, MOSS, INK, PAPER, SAND, MIST)
DEFAULT_THEME = GRAPHITE.name
# Ctrl+T order: cm's own (dark, then light), then the calmer built-ins, then Textual's original.
THEME_CYCLE = [t.name for t in CUSTOM] + ["nord", "gruvbox", "flexoki", "solarized-light", "textual-dark"]


def next_theme(current: str) -> str:
    try:
        return THEME_CYCLE[(THEME_CYCLE.index(current) + 1) % len(THEME_CYCLE)]
    except ValueError:
        return THEME_CYCLE[0]
