"""Light theme = pure black/white e-ink style; dark theme output is locked.

Covers the CLI printer, the Rich palette/bar renderers, the Textual theme
tokens, and a rendered-screen scan of the real TUI under ``cswap-light``.
"""
from __future__ import annotations

import json
import re

import pytest

from claude_swap import printer
from claude_swap.tui.theme import BLACK, CSWAP_DARK, CSWAP_LIGHT, WHITE, Palette
from claude_swap.tui.widgets import account_card_text, bar_cells, mini_account_text, usage_bar
from tests.test_tui import (
    FakeSwitcher,
    fake_engine,  # noqa: F401  (fixture)
    make_account,
    make_app,
    make_entry,
    menu_select,
    settle,
)

_COLOUR_CODE = re.compile(r"\033\[(?:[0-9;]*;)?(?:38|48);[25];")


@pytest.fixture
def forced(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setattr(printer, "_colors_enabled", None)

    def use(theme: str) -> None:
        monkeypatch.setattr(printer, "_theme", theme)

    return use


# --- CLI ------------------------------------------------------------------


class TestCliLight:
    def test_no_colour_codes_and_no_dim(self, forced):
        forced("light")
        out = "".join(
            f(s)
            for f in (
                printer.accent, printer.muted, printer.dimmed, printer.bolded,
                printer.bold_accent, printer.yellowed,
            )
            for s in ("x",)
        )
        assert not _COLOUR_CODE.search(out)
        assert "\033[2m" not in out
        assert "\033[3" not in out and "\033[9" not in out

    def test_plain_helpers_emit_bare_text(self, forced):
        forced("light")
        assert printer.accent("x") == "x"
        assert printer.muted("x") == "x"
        assert printer.dimmed("x") == "x"

    def test_warning_is_bold(self, forced, capsys):
        forced("light")
        printer.warning("careful")
        assert capsys.readouterr().out == "\033[1mcareful\033[0m\n"
        assert printer.yellowed("w") == "\033[1mw\033[0m"

    def test_error_is_reversed(self, forced, capsys):
        forced("light")
        printer.error("boom")
        assert capsys.readouterr().err == "\033[7mboom\033[0m\n"


class TestCliDarkLocked:
    """Dark output must stay byte-identical to the pre-e-ink behaviour."""

    def test_codes(self, forced, capsys):
        forced("dark")
        assert printer.accent("x") == "\033[38;5;173mx\033[0m"
        assert printer.muted("x") == "\033[38;5;250mx\033[0m"
        assert printer.dimmed("x") == "\033[2mx\033[0m"
        assert printer.bolded("x") == "\033[1mx\033[0m"
        assert printer.bold_accent("x") == "\033[1m\033[38;5;173mx\033[0m"
        assert printer.yellowed("x") == "\033[33mx\033[0m"
        printer.warning("w")
        printer.error("e")
        captured = capsys.readouterr()
        assert captured.out == "\033[33mw\033[0m\n"
        assert captured.err == "\033[31me\033[0m\n"


# --- Rich palette / bars --------------------------------------------------


def _spans(text):
    return [(text.plain[s.start:s.end], str(s.style)) for s in text.spans]


class TestLightPalette:
    def test_severity_uses_weight_not_hue(self):
        p = Palette.LIGHT
        assert p.severity(10.0) == ""  # plain
        assert p.severity(75.0) == "bold"
        assert p.severity(95.0) == "reverse"
        assert p.severity(None) == ""

    def test_from_theme_resolves_light(self):
        assert Palette.from_theme(CSWAP_LIGHT) is Palette.LIGHT
        assert Palette.from_theme(CSWAP_DARK).sev_ok == Palette.DARK.sev_ok

    def test_bar_uses_solid_fill_and_different_unused_glyph(self):
        text = bar_cells(50.0, 10, palette=Palette.LIGHT)
        assert text.plain == "█" * 5 + "─" * 5
        # fill and track are both black (plain, inheriting black) - never grey
        assert all(s.style == "" for s in text.spans)

    def test_bar_stays_plain_at_every_severity(self):
        for pct in (10.0, 75.0, 95.0):
            text = bar_cells(pct, 10, palette=Palette.LIGHT)
            assert "reverse" not in {str(s.style) for s in text.spans}

    def test_no_dim_when_stale(self):
        text = usage_bar("5h", 95.0, "resets 1h", 10, stale=True, palette=Palette.LIGHT)
        styles = {str(s.style) for s in text.spans}
        assert not any("dim" in s for s in styles)
        assert "reverse italic" in styles

    def test_card_and_mini_have_no_colour_or_dim(self):
        acc = make_account(
            1, active=True, entry=make_entry(pct5=95.0, pct7=75.0, age_s=900.0)
        )
        for text in (
            account_card_text(acc, 100, palette=Palette.LIGHT),
            mini_account_text(acc, 0.0, palette=Palette.LIGHT),
        ):
            for _chunk, style in _spans(text):
                assert "#" not in style and "dim" not in style
                assert "color" not in style


class TestDarkPaletteLocked:
    def test_bar_glyphs_and_styles(self):
        text = bar_cells(50.0, 10, threshold=94.0, palette=Palette.DARK)
        assert text.plain == "━" * 5 + "─" * 4 + "┃"
        assert [str(s.style) for s in text.spans][0] == "#87af87"
        assert [str(s.style) for s in text.spans][-1] == "#d7af5f"

    def test_stale_still_dim(self):
        text = bar_cells(95.0, 4, stale=True, palette=Palette.DARK)
        assert str(text.spans[0].style) == "#d75f5f dim"

    def test_usage_bar_pct_style(self):
        text = usage_bar("5h", 75.0, None, 4, stale=True)
        assert str(text.spans[-1].style) == "#d7af5f dim"

    def test_theme_tokens_unchanged(self):
        assert (CSWAP_DARK.primary, CSWAP_DARK.background, CSWAP_DARK.panel) == (
            "#d7875f", "#141414", "#262626",
        )
        assert CSWAP_DARK.variables["track"] == "#3a3a3a"


# --- Textual theme tokens -------------------------------------------------


class TestLightThemeTokens:
    def test_named_tokens_are_black_or_white(self):
        for name in (
            "primary", "secondary", "accent", "foreground", "background",
            "surface", "panel", "success", "warning", "error",
        ):
            assert getattr(CSWAP_LIGHT, name) in (BLACK, WHITE), name

    def test_every_colour_variable_is_black_or_white(self):
        base = CSWAP_LIGHT.to_color_system().generate()
        base.update(CSWAP_LIGHT.variables)
        offenders = {
            k: v
            for k, v in base.items()
            if isinstance(v, str)
            and v.startswith(("#", "auto"))
            and v.lower() not in (BLACK, WHITE)
            and not (len(v) == 9 and v.endswith("00"))  # fully transparent
        }
        assert offenders == {}

    def test_cursor_and_selection_are_reversed(self):
        v = CSWAP_LIGHT.variables
        assert (v["block-cursor-background"], v["block-cursor-foreground"]) == (BLACK, WHITE)
        assert v["block-hover-background"] == BLACK
        assert v["track"] == BLACK


# --- Rendered TUI ---------------------------------------------------------


def _scan(app):
    """(colours, dim_seen, invisible_texts) over everything the screen paints."""
    colours: set[str] = set()
    dim = False
    invisible: list[str] = []
    for strip in app.screen._compositor.render_strips():
        for seg in strip:
            st = seg.style
            if st is None:
                continue
            pair = []
            for c in (st.color, st.bgcolor):
                if c is not None:
                    hexv = c.get_truecolor().hex
                    colours.add(hexv)
                    pair.append(hexv)
            dim = dim or bool(st.dim)
            text = seg.text.strip()
            # '█' is the thick list-item border / filled bar: solid by design
            if text and text.strip("█▏") and len(pair) == 2 and pair[0] == pair[1] and not st.reverse:
                invisible.append(seg.text)
    return colours, dim, invisible


def _assert_eink(app, label):
    colours, dim, invisible = _scan(app)
    assert colours <= {BLACK, WHITE}, (label, colours)
    assert not dim, label
    assert not invisible, (label, invisible)


@pytest.mark.asyncio
class TestRenderedLightTui:
    def _app(self, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"ui": {"theme": "light"}}))
        accounts = [
            make_account(1, active=True, entry=make_entry(pct5=95.0, pct7=75.0)),
            make_account(2, entry=make_entry(pct5=50.0, age_s=900.0)),
            make_account(3, entry=make_entry(pct5=20.0, age_s=900.0)),
        ]
        return make_app(FakeSwitcher(accounts, tmp_path))

    async def test_dashboard_switch_sessions_confirm(self, tmp_path):
        app = self._app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            assert app.theme == "cswap-light"
            _assert_eink(app, "dashboard")
            await pilot.press("down")
            await pilot.pause()
            _assert_eink(app, "dashboard-moved")
            await menu_select(pilot, "switch")
            await settle(pilot)
            _assert_eink(app, "switch")
            await pilot.press("escape")
            await pilot.pause()
            await menu_select(pilot, "sessions")
            await settle(pilot)
            _assert_eink(app, "sessions")
            await pilot.press("escape")
            await pilot.pause()
            await menu_select(pilot, "remove-menu")
            await menu_select(pilot, "remove:2")
            await settle(pilot)
            _assert_eink(app, "confirm-modal")

    async def test_auto_screen(self, tmp_path, fake_engine):  # noqa: F811
        app = self._app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("g")
            await settle(pilot)
            _assert_eink(app, "auto")

    async def test_dark_screen_keeps_colour(self, tmp_path):
        accounts = [make_account(1, active=True, entry=make_entry(pct5=95.0))]
        app = make_app(FakeSwitcher(accounts, tmp_path))
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            assert app.theme == "cswap-dark"
            colours, _dim, _inv = _scan(app)
            assert "#d75f5f" in colours  # severity red still used in dark
