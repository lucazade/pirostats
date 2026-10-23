"""Desktop fit: the pages render to the width a Planar widget really has.

The plasmoid publishes a 5th geom field with its form factor; code 3 means "this
desktop widget keeps the width it is given" (fit mode). Only then does the
published text-area width replace the content-derived canonical width — panels and
a desktop widget in upstream mode (code 2) must be untouched.
"""

import pytest

import config
from config import (
    DESKTOP_WIDTH_MAX,
    FORM_FACTOR_DESKTOP,
    FORM_FACTOR_DESKTOP_FIT,
    TOOLTIP_WIDTH_FLOOR,
    PanelGeometry,
    _apply_desktop_width,
    _parse_geom,
    apply_canonical_width,
    detect_panel_geometry,
)


class _Cfg:
    """Minimal stand-in: _apply_desktop_width only touches display.tooltip_width."""

    def __init__(self, width=50):
        self.display = type("D", (), {"tooltip_width": width})()


# ── Parsing ───────────────────────────────────────────────────────────────────

def test_geom_parses_fifth_field_as_form_factor():
    geo = _parse_geom("900 6.4 0 6.4 2\n")
    assert geo.form_factor == FORM_FACTOR_DESKTOP


def test_geom_without_fifth_field_stays_unknown():
    assert _parse_geom("900 6.4 0 6.4\n").form_factor is None


def test_geom_with_garbage_fifth_field_is_rejected():
    # A malformed extra field must not silently degrade into a partial geometry.
    assert _parse_geom("900 6.4 0 6.4 x\n") is None


# ── Orientation: a desktop geom has no panel edge ─────────────────────────────

def test_desktop_geom_keeps_its_width_despite_orientation(monkeypatch, tmp_path):
    geom = tmp_path / "geom"
    geom.write_text("900 6.4 0 6.4 2\n")     # vertical flag 0, desktop code 2
    monkeypatch.setattr(config, "GEOM_FILE", geom)
    geo = detect_panel_geometry()
    assert geo.usable_px == pytest.approx(900)
    assert geo.form_factor == FORM_FACTOR_DESKTOP


# ── Width resolution ──────────────────────────────────────────────────────────

def test_fit_mode_uses_published_width():
    cfg = _Cfg()
    _apply_desktop_width(cfg, PanelGeometry(usable_px=960, glyph_adv=6.4,
                                            tooltip_adv=8.0,
                                            form_factor=FORM_FACTOR_DESKTOP_FIT))
    assert cfg.display.tooltip_width == 120       # floor(960 / 8)


def test_upstream_desktop_mode_is_left_alone():
    cfg = _Cfg(50)
    _apply_desktop_width(cfg, PanelGeometry(usable_px=960, glyph_adv=6.4,
                                            tooltip_adv=8.0,
                                            form_factor=FORM_FACTOR_DESKTOP))
    assert cfg.display.tooltip_width == 50


def test_panel_geom_is_left_alone():
    cfg = _Cfg(50)
    _apply_desktop_width(cfg, PanelGeometry(usable_px=960, glyph_adv=6.4,
                                            tooltip_adv=8.0, form_factor=1))
    assert cfg.display.tooltip_width == 50


def test_fit_mode_needs_the_tooltip_advance():
    cfg = _Cfg(50)
    _apply_desktop_width(cfg, PanelGeometry(usable_px=960, glyph_adv=6.4,
                                            form_factor=FORM_FACTOR_DESKTOP_FIT))
    assert cfg.display.tooltip_width == 50


def test_fit_mode_clamps_to_floor_and_ceiling():
    narrow = _Cfg()
    _apply_desktop_width(narrow, PanelGeometry(usable_px=40, glyph_adv=6.4,
                                               tooltip_adv=8.0,
                                               form_factor=FORM_FACTOR_DESKTOP_FIT))
    assert narrow.display.tooltip_width == TOOLTIP_WIDTH_FLOOR

    wide = _Cfg()
    _apply_desktop_width(wide, PanelGeometry(usable_px=40000, glyph_adv=6.4,
                                             tooltip_adv=8.0,
                                             form_factor=FORM_FACTOR_DESKTOP_FIT))
    assert wide.display.tooltip_width == DESKTOP_WIDTH_MAX


# ── Wiring: apply_canonical_width must let the fit override the canonical ─────

def test_apply_canonical_width_prefers_the_widget_width_in_fit_mode(monkeypatch):
    monkeypatch.setattr(config, "detect_panel_geometry",
                        lambda: PanelGeometry(usable_px=960, glyph_adv=6.4,
                                              tooltip_adv=8.0,
                                              form_factor=FORM_FACTOR_DESKTOP_FIT))
    cfg = _Cfg()
    cfg.pages = type("P", (), {"graph_width": 0})()
    apply_canonical_width(cfg, 78)
    assert cfg.display.tooltip_width == 120
    assert cfg.pages.graph_width == 960          # 120 cols × 8 px advance


def test_apply_canonical_width_keeps_canonical_for_panels(monkeypatch):
    monkeypatch.setattr(config, "detect_panel_geometry",
                        lambda: PanelGeometry(usable_px=300, glyph_adv=6.4,
                                              tooltip_adv=8.0, form_factor=1))
    cfg = _Cfg()
    cfg.pages = type("P", (), {"graph_width": 0})()
    apply_canonical_width(cfg, 78)
    assert cfg.display.tooltip_width == 78
    assert cfg.pages.graph_width == 624
