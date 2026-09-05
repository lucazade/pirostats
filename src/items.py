"""The cell-factory library: the building blocks registry.py composes into rows
for each `metric:form`. The item registry itself does NOT live here (that's the
dispatch table in registry.py, keyed by metric × form) — only the reusable pieces.

Two-level model (a single verb: "concatenate"):

    cell      : (f, ident, r, tooltip, key) -> Cell | None   (None = no cell)
    row-group : (f, ident, r, tooltip)      -> list[Row]      (multi-row lives here)

`row(*cells)` makes a single N-cell row; `per(source, *cells)` makes N rows, one
per instance (disks, fans). The cell-factories (`label`, `value`, `spark`,
`braille`, `freq_value`, `disk_*`, …) call back into `PanelFormatter`'s helpers
(`_label_cell`, `_spark_html_for`, `_fmt_freq`, `_disk_label`, …) via the `f`
instance they receive: the formatter isn't imported here (no import cycle), and
the pure cell helpers (`_val_cell`/`_aux_cell`/`_fmt_perc`) live in render_model.
"""
from __future__ import annotations

from typing import Callable, Optional

from render_model import (
    Cell,
    EMPTY_VALUE,
    PERCENT_PANEL_WIDTH,
    _aux_cell,
    _fmt_perc,
    _val_cell,
    css_class_active,
    css_class_from_thresholds,
)
import traces
from units import TEMP_SCALE

# (f, ident, r, tooltip, instance-key) -> Cell | None
CellFn = Callable[..., Optional[Cell]]
# (f, ident, r, tooltip) -> list[Row]
GroupFn = Callable[..., list]
# ── row-group: the two ways to produce rows ───────────────────────────────────

def row(*cells: CellFn) -> GroupFn:
    """A single row: concatenates cells (dropping the ones that return None)."""
    def group(f, ident, r, tooltip) -> list:
        cs = [c for cf in cells if (c := cf(f, ident, r, tooltip, None)) is not None]
        return [cs] if cs else []
    return group


def per(source: Callable[..., object], *cells: CellFn) -> GroupFn:
    """N rows: the same cell-row repeated for every instance of `source(f, r)`,
    which receives the instance key as the cell-factories' 5th argument."""
    def group(f, ident, r, tooltip) -> list:
        rows: list = []
        for key in source(f, r):
            cs = [c for cf in cells if (c := cf(f, ident, r, tooltip, key)) is not None]
            if cs:
                rows.append(cs)
        return rows
    return group


# ── generic cell-factories ────────────────────────────────────────────────────

def label(text_fn: Optional[Callable[..., str]] = None) -> CellFn:
    """Label cell: glyph in the panel, glyph+word(+delimiter) in the tooltip
    (decided by _label_cell). `text_fn(f, r, key)` is for dynamic (per-instance)
    labels; without it, uses the item name's configured label.

    In the panel with `glyphs = false` (the horizontal one by default, where the
    space is tightest) the cell isn't emitted (None) → only the value remains.
    This only affects items that HAVE a label: bars/columns/sparks are composed
    without `label()`, so they stay unaffected."""
    def cell(f, ident, r, tooltip, key):
        if not tooltip and not f._cfg.panel.glyphs:
            return None
        text = text_fn(f, r, key) if text_fn else None
        return f._label_cell(ident, tooltip, text)
    return cell


def value(attr: str, unit, thr_fn: Callable[..., object]) -> CellFn:
    """Value cell of the _std family: percentage/temperature/number with a
    unit. `unit` is a string (e.g. "%") or a (cfg)->str callable for units that
    depend on config (the temperature scale). `thr_fn(cfg)` returns the
    threshold (tuple → 3 bands, int → binary/active, None → no class)."""
    def cell(f, ident, r, tooltip, key):
        cfg = f._cfg
        v = getattr(r, attr)
        if v is None:
            return _val_cell(EMPTY_VALUE, ident=ident)
        u = unit(cfg) if callable(unit) else unit
        if u == "%":
            str_v = _fmt_perc(v, tooltip)
            min_w = PERCENT_PANEL_WIDTH
        elif tooltip:
            str_v = f"{v}°{u}"
            min_w = 0
        else:
            str_v = f"{v}{u}"
            min_w = 0
        thr = thr_fn(cfg)
        if isinstance(thr, int):
            cls = css_class_active(v, thr)
        elif thr is not None:
            cls = css_class_from_thresholds(v, thr)
        else:
            cls = None
        return _val_cell(str_v, cls, ident=ident, min_width=min_w)
    return cell


def spark(hist_attr: str, hist_name: str) -> CellFn:
    """Spark cell (aux role) for the combos: owns its own spacing (pad_left),
    independent of render_three_col_row's gap in front of the extra cell."""
    def cell(f, ident, r, tooltip, key):
        html = traces.spark_html(f, getattr(r, hist_attr), hist_name, tooltip)
        c = _aux_cell(html, ident=ident)
        if c.text:
            c.pad_left = 1
        return c
    return cell


def braille(hist_attr: str, prefix: str) -> CellFn:
    """Braille cell (aux role) for the combos, twin of spark(): same spacing,
    but braille rendering (2 samples/char, grad-<prefix> gradient)."""
    def cell(f, ident, r, tooltip, key):
        html = traces.braille_html(f, getattr(r, hist_attr), prefix, tooltip)
        c = _aux_cell(html, ident=ident)
        if c.text:
            c.pad_left = 1
        return c
    return cell


# ── frequency / turbo cell-factories (reused by several items) ────────────────

def freq_value(attr: str) -> CellFn:
    """Frequency value via _fmt_freq (MHz/GHz, unit only in the tooltip), no
    threshold. Reused by cpu_freq (with turbo) and gpu_intel_freq. _fmt_freq
    already handles None → EMPTY_VALUE."""
    def cell(f, ident, r, tooltip, key):
        return _val_cell(f._fmt_freq(getattr(r, attr), tooltip), ident=ident)
    return cell


def turbo_value() -> CellFn:
    """Turbo state as a value: on/off colored active/crit, EMPTY_VALUE if unknown."""
    def cell(f, ident, r, tooltip, key):
        t = r.cpu_turbo
        if t is None:
            return _val_cell(EMPTY_VALUE, ident=ident)
        return _val_cell("on", "active", ident=ident) if t else _val_cell("off", "crit", ident=ident)
    return cell


def turbo_icon() -> CellFn:
    """Turbo state (aux role) for cpu_freq (intrinsic turbo): "Turbo"/"Slow"
    text colored active/deactive, or an empty cell if unknown. pad_left=1 only
    when text is present, like render_three_col_row."""
    def cell(f, ident, r, tooltip, key):
        t = r.cpu_turbo
        if t is None:
            icon, icon_cls = "", None
        elif t:
            icon, icon_cls = "Turbo", "active"
        else:
            icon, icon_cls = "Slow", "deactive"
        c = _aux_cell(icon, cls=icon_cls, ident=ident)
        if c.text:
            c.pad_left = 1
        return c
    return cell


# ── multi-instance cell-factories (disk_usage / fan / hd_temp) ────────────────

def disk_label() -> CellFn:
    def cell(f, ident, r, tooltip, key):
        return f._label_cell(ident, tooltip, f._disk_label(key))
    return cell


def disk_value(thr_fn: Callable[..., object]) -> CellFn:
    def cell(f, ident, r, tooltip, key):
        cfg = f._cfg
        du = r.disk_usage.get(key)
        pv = du.percent if du else None
        if pv is None:
            return _val_cell(EMPTY_VALUE, ident=ident)
        return _val_cell(_fmt_perc(pv, tooltip), css_class_from_thresholds(pv, thr_fn(cfg)),
                         ident=ident, min_width=PERCENT_PANEL_WIDTH)
    return cell


def disk_space(thr_fn: Callable[..., object]) -> CellFn:
    """Third GB cell: absent in the panel (returns None), present in the
    tooltip — so "2 vs 3 cells depending on mode" stops being a branch. Owns
    its own spacing (pad_left/pad_right) instead of injecting it into the
    text. The used space is colored by threshold (thr_fn), like the % value cell."""
    def cell(f, ident, r, tooltip, key):
        if not tooltip:
            return None
        du = r.disk_usage.get(key)
        used_cls = (css_class_from_thresholds(du.percent, thr_fn(f._cfg))
                    if du and du.percent is not None else None)
        # Max widths of "used" and "total" across ALL disks: line up the `/`
        # vertically and give every cell the same width, so the column stays
        # flush across disks. Left-aligned right after the label column.
        used_w  = max((len(f"{d.used_gb}G")  for d in r.disk_usage.values()
                       if d and d.used_gb  is not None), default=0)
        total_w = max((len(f"{d.total_gb}G") for d in r.disk_usage.values()
                       if d and d.total_gb is not None), default=0)
        space = f._fmt_disk_space(du.used_gb if du else None,
                                  du.total_gb if du else None, used_cls,
                                  used_w, total_w)
        # Leading gap so the longest disk label doesn't touch the GB column
        # (the label column reserves no trailing space of its own).
        return _aux_cell(space, ident=ident, pad_left=1)
    return cell


def mem_space(thr_fn: Callable[..., object]) -> CellFn:
    """GB used/total middle column for mem_usage:value, tooltip-only like
    disk_space (returns None in the panel). Single reading, so no cross-instance
    width alignment: reuse _fmt_disk_space with zero widths."""
    def cell(f, ident, r, tooltip, key):
        if not tooltip or r.mem_used_gb is None or r.mem_total_gb is None:
            return None
        used_cls = (css_class_from_thresholds(r.mem_usage, thr_fn(f._cfg))
                    if r.mem_usage is not None else None)
        space = f._fmt_disk_space(r.mem_used_gb, r.mem_total_gb, used_cls)
        return _aux_cell(space, ident=ident, pad_left=1)
    return cell


FAN_OFF = "off"  # fan stopped (0 rpm / 0%): more readable than a bare "0"


def fan_value(attr: Optional[str] = None) -> CellFn:
    """Fan RPM: "<rpm> rpm" in the tooltip, just the number in the panel, no
    threshold. "off" when stopped (0 rpm), EMPTY_VALUE when absent. Reads the
    per-instance r.fan_speeds[key] by default, or a single named Readings field
    (the AMD GPU fan, which amdgpu reports in RPM where NVML gives a duty %)."""
    def cell(f, ident, r, tooltip, key):
        rpm = getattr(r, attr) if attr else r.fan_speeds.get(key)
        if rpm is None:
            speed = EMPTY_VALUE
        elif rpm == 0:
            speed = FAN_OFF
        elif tooltip:
            speed = f"{rpm} rpm"
        else:
            speed = str(rpm)
        return _val_cell(speed, ident=ident)
    return cell


def gpu_fan_value() -> CellFn:
    """GPU fan in %: "off" when stopped (0%, zero-RPM idle), else "<v>%";
    EMPTY_VALUE when absent. Doesn't go through the generic value() so every
    0% doesn't turn into "off" (cpu/gpu usage must stay "0%")."""
    def cell(f, ident, r, tooltip, key):
        v = r.gpu_fan
        if v is None:
            text = EMPTY_VALUE
        elif v == 0:
            text = FAN_OFF
        else:
            text = _fmt_perc(v, tooltip)
        return _val_cell(text, ident=ident, min_width=PERCENT_PANEL_WIDTH)
    return cell


def hd_temp_value(thr_fn: Callable[..., object]) -> CellFn:
    """Disk temperature from r.hd_temps[key] (the per() source filters out keys
    with no temperature, so v is never None here)."""
    def cell(f, ident, r, tooltip, key):
        cfg = f._cfg
        v = r.hd_temps.get(key)
        str_v = f"{v}°{TEMP_SCALE}" if tooltip else f"{v}{TEMP_SCALE}"
        return _val_cell(str_v, css_class_from_thresholds(v, thr_fn(cfg)), ident=ident)
    return cell




# ── threshold/unit accessors (resolved at render time on the instance's cfg) ──

_TEMP = lambda cfg: TEMP_SCALE
_NONE = lambda cfg: None


def _thr(field: str):
    return lambda cfg: getattr(cfg.thresholds, field)
