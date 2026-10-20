"""The "cswap-dark" Textual theme and shared color constants.

A subtle modern dark theme: neutral charcoal backgrounds in the VS Code
register, one warm terracotta accent (the same xterm-173 tone printer.py has
always used for the CLI — a deliberate nod to Claude Code's orange, used
sparingly), and desaturated severity colors so usage bars read calmly on a
dark background. Deliberately *not* a wholesale copy of any other tool's
palette.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from textual.theme import Theme

# Core palette (single source of truth — widgets import these for rich
# renderables, the Theme below maps them onto Textual's design tokens).
ACCENT = "#d7875f"  # warm terracotta (xterm 173)
FOREGROUND = "#e8e4de"  # soft, slightly warm off-white
MUTED = "#8a8a8a"  # secondary text
BACKGROUND = "#141414"
SURFACE = "#1e1e1e"
PANEL = "#262626"

# Usage severity ramp (desaturated for dark backgrounds).
SEV_OK = "#87af87"  # calm green: plenty of headroom
SEV_WARN = "#d7af5f"  # amber: climbing (>= 70%)
SEV_CRIT = "#d75f5f"  # soft red: near the limit (>= 90%)
TRACK = "#3a3a3a"  # unfilled bar track

# Severity band edges. WARN mirrors where a user starts caring; CRIT mirrors
# the auto-switch default threshold so bar color and switch behavior agree.
WARN_PCT = 70.0
CRIT_PCT = 90.0


@dataclass(frozen=True)
class Palette:
    """Resolved colors for Rich renderables, keyed to a Textual theme.

    Rich renderables bake color into styles at render time, so they can't read
    Textual's ``$variables`` the way the .tcss layer does. A Palette carries the
    active theme's colors so the same render code paints correctly in either
    theme. Resolve from the Theme object (never App.theme_variables, which lags
    the deferred CSS refresh)."""

    accent: str
    foreground: str
    muted: str
    sev_ok: str
    sev_warn: str
    sev_crit: str
    track: str
    # Bar glyphs and how a stale (old) measurement is marked. Dark keeps the
    # thin line bar and ``dim``; the e-ink palette uses solid blocks and italic.
    bar_filled: str = "━"
    bar_half: str = "╸"
    bar_empty: str = "─"
    bar_tick: str = "┃"
    stale_attr: str = "dim"
    plain_bar: bool = False

    DARK: ClassVar["Palette"]
    LIGHT: ClassVar["Palette"]

    def stale(self, style: str) -> str:
        """``style`` marked as a stale measurement."""
        return f"{style} {self.stale_attr}".strip()

    def bar_style(self, pct: float | None) -> str:
        """Fill style for a usage bar. The e-ink palette keeps the bar plain
        (a reversed fill would paint the blocks white); severity shows in the
        percentage text instead."""
        if self.plain_bar:
            return self.foreground
        return self.severity(pct)

    def severity(self, pct: float | None) -> str:
        if pct is None:
            return self.muted
        if pct >= CRIT_PCT:
            return self.sev_crit
        if pct >= WARN_PCT:
            return self.sev_warn
        return self.sev_ok

    @classmethod
    def from_theme(cls, theme: Theme) -> "Palette":
        if theme.name == CSWAP_LIGHT.name:
            return cls.LIGHT
        return cls(
            accent=theme.primary,
            foreground=theme.foreground,
            muted=theme.secondary,
            sev_ok=theme.success,
            sev_warn=theme.warning,
            sev_crit=theme.error,
            track=theme.variables.get("track", TRACK),
        )


CSWAP_DARK = Theme(
    name="cswap-dark",
    primary=ACCENT,
    secondary=MUTED,
    accent=ACCENT,
    foreground=FOREGROUND,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    success=SEV_OK,
    warning=SEV_WARN,
    error=SEV_CRIT,
    dark=True,
    variables={
        # Footer keys pick up the accent instead of the default blue.
        "footer-key-foreground": ACCENT,
        "block-cursor-background": PANEL,
        "block-cursor-foreground": FOREGROUND,
        "block-cursor-text-style": "none",
        "track": TRACK,
    },
)

# Light theme: pure black and white, no colour and no grey (e-ink style).
# Severity is carried by text weight, not hue: normal text, bold for a warning,
# reversed (white on black) for danger. Selected rows and cursors are reversed.
BLACK = "#000000"
WHITE = "#ffffff"


def _eink_variables() -> dict[str, str]:
    """Every Textual design token of the light theme pinned to black or white.

    Textual derives dozens of tints (``$panel-lighten-1``, ``$text-muted``,
    scrollbar shades, ...) by blending, which would introduce greys. Resolve
    the full derived set once and force each name onto black or white."""
    base = Theme(
        name="cswap-light", primary=BLACK, secondary=BLACK, accent=BLACK,
        foreground=BLACK, background=WHITE, surface=WHITE, panel=WHITE,
        success=BLACK, warning=BLACK, error=BLACK, dark=False,
    )
    light_families = ("background", "surface", "panel")
    out: dict[str, str] = {}
    for name, value in base.to_color_system().generate().items():
        family = name.split("-")[0]
        if family in ("ansi", "boost") or name.startswith("footer-"):
            continue
        if not value.startswith(("#", "auto")):
            continue  # text styles and keywords such as link-style: keep Textual's
        out[name] = WHITE if family in light_families else BLACK
    out.update({
        "footer-background": WHITE,
        "footer-foreground": BLACK,
        "footer-key-foreground": BLACK,
        "footer-key-background": WHITE,
        "footer-description-foreground": BLACK,
        "footer-description-background": WHITE,
        "footer-item-background": WHITE,
        "boost": "#00000000",
        "boost-darken-1": "#00000000",
        "boost-darken-2": "#00000000",
        "boost-darken-3": "#00000000",
        "boost-lighten-1": "#00000000",
        "boost-lighten-2": "#00000000",
        "boost-lighten-3": "#00000000",
        # Selection and cursors are reversed: white on black.
        "block-cursor-background": BLACK,
        "block-cursor-foreground": WHITE,
        "block-cursor-text-style": "none",
        "block-cursor-blurred-background": BLACK,
        "block-cursor-blurred-foreground": WHITE,
        "block-cursor-blurred-text-style": "none",
        "block-hover-background": BLACK,
        "input-cursor-background": BLACK,
        "input-cursor-foreground": WHITE,
        "input-selection-background": BLACK,
        "input-selection-foreground": WHITE,
        "screen-selection-background": BLACK,
        "screen-selection-foreground": WHITE,
        "scrollbar-background": WHITE,
        "scrollbar-background-hover": WHITE,
        "scrollbar-background-active": WHITE,
        "scrollbar-corner-color": WHITE,
        "button-foreground": BLACK,
        "button-color-foreground": BLACK,
        "link-color": BLACK,
        "link-color-hover": WHITE,
        "link-background": WHITE,
        "link-background-hover": BLACK,
        "markdown-h1-background": WHITE,
        "markdown-h2-background": WHITE,
        "markdown-h3-background": WHITE,
        "markdown-h4-background": WHITE,
        "markdown-h5-background": WHITE,
        "markdown-h6-background": WHITE,
        "border": BLACK,
        "border-blurred": BLACK,
        "track": BLACK,
    })
    return out


CSWAP_LIGHT = Theme(
    name="cswap-light",
    primary=BLACK,
    secondary=BLACK,
    accent=BLACK,
    foreground=BLACK,
    background=WHITE,
    surface=WHITE,
    panel=WHITE,
    success=BLACK,
    warning=BLACK,
    error=BLACK,
    dark=False,
    variables=_eink_variables(),
)

Palette.DARK = Palette(
    accent=ACCENT, foreground=FOREGROUND, muted=MUTED,
    sev_ok=SEV_OK, sev_warn=SEV_WARN, sev_crit=SEV_CRIT, track=TRACK,
)

# Empty style = inherit the widget's colours, so text flips to white-on-black
# automatically inside a reversed (selected) row.
Palette.LIGHT = Palette(
    accent="", foreground="", muted="",
    sev_ok="", sev_warn="bold", sev_crit="reverse", track="",
    bar_filled="█", bar_half="▌", bar_empty="─", bar_tick="┃",
    stale_attr="italic", plain_bar=True,
)
