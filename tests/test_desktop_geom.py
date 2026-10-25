"""The desktop widget publishes its own glyph advance (DESKTOP_GEOM_FILE) and, when it
exists, that number — not the panel's — sizes the graphs page."""

import pytest

import config
from config import (
    Config, PanelGeometry, TOOLTIP_WIDTH_FLOOR, _read_desktop_tooltip_adv,
    apply_canonical_width,
)


def _publish(monkeypatch, tmp_path, text):
    f = tmp_path / "geom-desktop"
    f.write_text(text)
    monkeypatch.setattr(config, "DESKTOP_GEOM_FILE", f)


def _panel(monkeypatch, tooltip_adv):
    monkeypatch.setattr(config, "detect_panel_geometry",
                        lambda: PanelGeometry(tooltip_adv=tooltip_adv))


def test_reads_the_published_advance(monkeypatch, tmp_path):
    _publish(monkeypatch, tmp_path, "9.2\n")
    assert _read_desktop_tooltip_adv() == pytest.approx(9.2)


def test_absent_file_is_none():
    # the autouse fixture points it at a path that does not exist
    assert _read_desktop_tooltip_adv() is None


@pytest.mark.parametrize("text", ["", "\n", "abc", "0", "-3", "nan", "inf", "x 9"])
def test_unusable_content_is_none(monkeypatch, tmp_path, text):
    _publish(monkeypatch, tmp_path, text)
    assert _read_desktop_tooltip_adv() is None


def test_desktop_advance_sizes_the_graphs(monkeypatch, tmp_path):
    _publish(monkeypatch, tmp_path, "7.0\n")
    _panel(monkeypatch, 11.0)
    cfg = Config()
    apply_canonical_width(cfg, TOOLTIP_WIDTH_FLOOR + 6)
    assert cfg.pages.graph_width == round((TOOLTIP_WIDTH_FLOOR + 6) * 7.0)


def test_panel_advance_is_used_without_a_desktop_widget(monkeypatch):
    _panel(monkeypatch, 11.0)
    cfg = Config()
    apply_canonical_width(cfg, TOOLTIP_WIDTH_FLOOR + 6)
    assert cfg.pages.graph_width == round((TOOLTIP_WIDTH_FLOOR + 6) * 11.0)


def test_a_broken_desktop_file_falls_back_to_the_panel(monkeypatch, tmp_path):
    _publish(monkeypatch, tmp_path, "garbage")
    _panel(monkeypatch, 11.0)
    cfg = Config()
    apply_canonical_width(cfg, TOOLTIP_WIDTH_FLOOR)
    assert cfg.pages.graph_width == round(TOOLTIP_WIDTH_FLOOR * 11.0)


def test_nothing_published_keeps_the_default_width(monkeypatch):
    _panel(monkeypatch, None)
    cfg = Config()
    default = cfg.pages.graph_width
    apply_canonical_width(cfg, TOOLTIP_WIDTH_FLOOR + 6)
    assert cfg.pages.graph_width == default
