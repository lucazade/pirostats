"""
Output formatter: builds Row/Cell data (see render_model.py) for each enabled
item, then serializes it to HTML — table-free monospace (mono_render) for the
tooltip and the vertical panel, inline <span> (render_row_inline) for the
horizontal panel (a single physical line). Color is a CSS class name, defined
visually only in style-dark.css — this module never decides what a color
looks like.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional
import base64
import copy
import html
import re
import time

import chart
from config import Config
from render_model import (
    Cell,
    EMPTY_VALUE,
    Ident,
    PERCENT_PANEL_WIDTH,
    Row,
    SEPARATOR_ITEMS,
    Separator,
    _aux_cell,
    _fmt_perc,
    _nbsp,
    _val_cell,
    css_class_active,
    css_class_battery,
    css_class_from_thresholds,
    group_rows_into_blocks,
    render_row_inline,
    render_three_col_row,
    render_two_pair_row,
)
from mono_render import global_width_of, render_blocks_monospace
from traces import braille_html
from units import TEMP_SCALE
from items import FAN_OFF
from registry import item_gate as _registry_gate, render_item as _registry_render
from sensors import BatterySys, BatteryPeriph, HardwareInfo, Readings, TOP_PROCESS_PAGE_ROWS, timed_section

# Top-processes page column layout: PID | 2sp | COMMAND | 1sp | %CPU(4) | 1sp |
# %MEM(5). _TOP_PROCESS_MIN_PID reserves a 6-digit PID so the width is stable; the
# COMMAND column is elastic — it GROWS to fill the tooltip's canonical width (the
# main page can be wider), showing longer names, and never shrinks below its min.
# _TOP_PROCESS_MIN_WIDTH (the all-columns-minimal width) is also the floor folded
# into canonical_width, so a sparse main page can't leave the page wider than the
# box either.
_TOP_PROCESS_MIN_PID   = 6
_TOP_PROCESS_COMM_MIN  = 15                    # base COMMAND column (grows to fill)
_TOP_PROCESS_SIDE_COLS = 2 + 1 + 4 + 1 + 5     # gaps + %CPU + %MEM around PID/COMMAND
_TOP_PROCESS_MIN_WIDTH = _TOP_PROCESS_MIN_PID + _TOP_PROCESS_COMM_MIN + _TOP_PROCESS_SIDE_COLS  # = 34

# Seconds each half of the panel battery's %/watt alternation stays on screen.
# How the item behaves, not a setting: the panel has room for one value at a
# time, and this is the dwell that reads as deliberate rather than flickery.
# The tooltip has the room for both, so it shows them side by side instead.
_BATTERY_ALTERNATE_SECONDS = 5


def _net_fmt(bps: int) -> str:
    k = bps // 1000
    if k >= 1000:
        return f"{k // 1000}M"
    if k > 0:
        return f"{k}K"
    return "0"


def _maxed_readings(r: Readings, hw: HardwareInfo) -> Readings:
    """A copy of `r` with every volatile, width-driving field pushed to its
    bounded maximum: usages/signals to 100%, temps/rates/freqs to full digits,
    the IPv4 to 255.255.255.255, disks to 100% used. Hardware-fixed facts keep
    their real values — RAM/disk totals (only the 'used' side is maxed), and the
    already-capped interface/SSID identity (the discovered name, so the row is
    counted even if the live reading is momentarily empty). Feeds
    PanelFormatter.canonical_width; see it for the why."""
    m = copy.deepcopy(r)
    m.cpu_usage = m.mem_usage = m.swap_usage = 100
    m.cpu_temp = m.gpu_temp = 100
    m.gpu_usage = m.gpu_mem = m.gpu_dec = m.gpu_fan = 100
    m.gpu_intel_usage = m.gpu_intel_dec_usage = 100
    m.gpu_intel_freq = 9999
    m.gpu_amd_usage = m.gpu_amd_mem_usage = m.gpu_amd_temp = 100
    m.gpu_amd_freq = 9999
    m.gpu_amd_fan_speed = 9999      # RPM, four digits like the chassis fans
    m.cpu_freq = 9999.0
    m.screen_brightness = m.wifi_signal = 100
    m.net_up_bps = m.net_down_bps = 999_000_000          # -> "999M"
    m.disk_read_bps = m.disk_write_bps = 999_000_000
    m.ip_address = "255.255.255.255"                     # widest IPv4
    m.net_device = m.net_device or hw.net_device         # stable even if link momentarily down
    if m.mem_total_gb is not None:                       # RAM size is fixed; max only the used side
        m.mem_used_gb = m.mem_total_gb
    for du in m.disk_usage.values():
        if du is not None:
            du.percent = 100
            if du.total_gb is not None:
                du.used_gb = du.total_gb
    # Seed the multi-instance rows from hw, not the live reading, so the width is
    # the same whether or not a slow sensor has reported yet (first paint skips
    # them). disk_usage's mounts aren't in hw, so it stays reading-driven above.
    m.hd_temps   = {label: 100  for label in hw.hd_temp_paths}
    m.fan_speeds = {idx:   9999 for idx   in hw.fan_paths}
    m.disk_smart = {label: True for label in hw.disk_smart_drives}
    for b in m.battery_sys:                              # reserve the widest row: the rate/limit
        b.perc, b.rate, b.state = "100%", 99, "discharging"  # middle column shows only while (dis)charging
        b.limit = 80 if b.limit is None else b.limit
    if m.battery_mouse is not None:
        m.battery_mouse.perc = "100%"
    if m.battery_kbd is not None:
        m.battery_kbd.perc = "100%"
    if m.top_process:                                    # kernel-capped comm (15) at full CPU, per shown row
        m.top_process = [("X" * 15, 100) for _ in m.top_process]
    cores = hw.cpu_count or 1
    m.load_avg = (float(cores), float(cores), float(cores))
    m.uptime = 999 * 86400 + 23 * 3600 + 59 * 60         # "999d 23h 59m"
    return m


def _separator_size(name: str) -> Optional[str]:
    """Map the TOML item name 'separator_small' / 'separator_big' to its
    Separator size, or None for a real item. Lets a section's `items` list
    interleave explicit gaps between rows (drawn by mono_render as a
    .separator-rule-small/-big div). Only the vertical panel and the tooltip
    render them; the horizontal panel skips non-row entries (see format_panel),
    so a panel separator is naturally vertical-only. Duplicates in the list are
    fine — repeat the name wherever a gap is wanted."""
    return SEPARATOR_ITEMS.get(name)


# Separator sizes ordered weak→strong, so collapsing a run of consecutive
# separators can keep the largest gap (e.g. a section's trailing 'small' meeting
# the tooltip's auto 'big' yields 'big').
_SEP_RANK = {"small": 0, "big": 1}


def _normalize_separators(entries: list[Row | Separator]) -> list[Row | Separator]:
    """A separator may only sit BETWEEN two real rows. Drop any at the very
    start or end (nothing to separate — e.g. a separator declared at the edge of
    the first/last section, or stranded when its neighbour section collapsed)
    and collapse consecutive separators into one, keeping the largest. This is
    what lets a separator placed at a section edge — the end of one section or
    the start of the next — become the single gap between two concatenated panel
    sections, instead of being silently dropped per-section."""
    out: list[Row | Separator] = []
    pending: Optional[Separator] = None
    for entry in entries:
        if isinstance(entry, Separator):
            if pending is None or _SEP_RANK[entry.size] > _SEP_RANK[pending.size]:
                pending = entry
            continue
        if pending is not None and out:   # emit only once a real row precedes it
            out.append(pending)
        pending = None
        out.append(entry)
    # a trailing `pending` separates nothing → dropped
    return out


# ── Panel/tooltip formatter ────────────────────────────────────────────────────

class PanelFormatter:
    def __init__(self, cfg: Config, hw: HardwareInfo):
        self._cfg = cfg
        self._hw  = hw
        # Orientation resolved once in load_config (auto-detected from the Plasma
        # panel edge, or forced via render --layout) and stored on the Config.
        self._vertical = cfg.vertical
        # canonical_width memo: cfg/hw are fixed per instance (the daemon rebuilds
        # the formatter on reload/rescan), so the width changes only with the few
        # reading-derived inputs in _canonical_sig — cache keyed on those.
        self._canonical_key: object = None
        self._canonical_cache = 0

    def format_panel(self, r: Readings, css: str = "", timings: Optional[dict[str, float]] = None) -> str:
        entries = self._build_entries(r, tooltip=False, timings=timings)
        if self._vertical:
            blocks = group_rows_into_blocks(entries)
            # Table-free monospace, same as the tooltip (see mono_render): the
            # vertical panel pays the live <table> re-layout tax every poll,
            # 24/7. Smaller than the tooltip's (fewer/tinier tables) but
            # always-on, so worth shedding. Horizontal panel is already
            # table-free (render_row_inline below).
            # min_width: floors the global_width so that, absent the bar (the
            # widest item), the glyph↔value pad doesn't shrink to zero and
            # make them touch — same mechanism as the tooltip's
            # display.panel_min_width knob.
            body = render_blocks_monospace(blocks, min_width=self._cfg.display.panel_min_width)
            root_class = "panel panel-v"
        else:
            # Horizontal strip: rows joined by an inter-item gap span whose width
            # is CSS letter-spacing on an &nbsp; anchor — Qt's RichText ignores
            # CSS width, and font-size would inflate the row height, while
            # letter-spacing adds pure horizontal advance. `.item-gap` is the
            # default gap (set in style-dark.css); a separator_small/big dropped
            # between two items swaps that one gap
            # for `.separator-rule-small/-big` (wider, for grouping). The `.gap`
            # tag also lets the inspection overlay colour it; consecutive/edge
            # separators are already collapsed/dropped by _normalize_separators.
            parts: list[str] = []
            pending: Optional[str] = None   # separator size waiting before the next row
            for entry in entries:
                if isinstance(entry, Separator):
                    pending = entry.size
                    continue
                if parts:
                    cls = f"separator-rule-{pending}" if pending else "item-gap"
                    parts.append(f'<span class="gap {cls}">&nbsp;</span>')
                parts.append(render_row_inline(entry))
                pending = None
            body = "".join(parts)
            root_class = "panel panel-h"
        style = f"<style>{css}</style>" if css else ""
        return f'{style}<div class="{root_class}">{body}</div>'

    def _wrap_tooltip(self, body: str, css: str, header: str = "", footer: str = "") -> str:
        """The tooltip shell shared by the full view and the deep-dive pages:
        the stylesheet plus a `.tooltip` div. `header` (the page title) leads and
        `footer` (the pager row) trails, both inside the div so they inherit the
        tooltip styling."""
        style = f"<style>{css}</style>" if css else ""
        return f'{style}<div class="tooltip">{header}{body}{footer}</div>'

    def format_page(self, inner: str, css: str = "", header: str = "", footer: str = "") -> str:
        """Wrap arbitrary inner HTML (a page's command output) in the same shell
        as format_tooltip, so a deep-dive page inherits the tooltip look."""
        return self._wrap_tooltip(inner, css, header, footer)

    def format_cpu_cores(self, r: Readings, css: str = "", header: str = "",
                         pager_fn: Optional[Callable[[int], str]] = None) -> str:
        """The cpu_cores page: one row per core — 'Core N:', a braille history
        spark (cpu gradient, same as the panel's cpu spark) and the current % —
        one column, the spark stretched to fill the page to the main tooltip's
        floor width so no core row leaves an empty right margin. Hand-laid (not
        mono_render) since the spark sizing is bespoke; widths are in mono chars."""
        usage = r.cpu_core_usage
        if not usage:
            return self._wrap_tooltip('<div class="page">cpu cores: no data yet</div>',
                                      css, header=header)
        hist    = r.cpu_core_history or []
        thr     = tuple(self._cfg.thresholds.cpu_usage)
        n       = len(usage)
        label_w = len(f"Core {n - 1}:")
        val_w   = 4
        # Stretch the braille so the row exactly fills tooltip_width (the same
        # width the other pages size to). None (width off) → the config length.
        # sensors over-provisions the per-core history so the longer spark has data.
        min_w     = self._cfg.display.tooltip_width
        braille_w = max(1, min_w - label_w - val_w - 2) if min_w > 0 else None

        def cell(i: int) -> str:
            label = f"Core {i}:"
            lbl   = f'<span class="label">{label}{"&nbsp;" * (label_w - len(label) + 1)}</span>'
            spark = braille_html(self, hist[i] if i < len(hist) else None, "cpu", True,
                                 chars=braille_w)
            val   = f"{usage[i]}%"
            cls   = css_class_from_thresholds(usage[i], thr)
            return (f'{lbl}{spark}<span class="gap">&nbsp;</span>'
                    f'<span class="val {cls}">{"&nbsp;" * (val_w - len(val))}{val}</span>')

        bw      = braille_w if braille_w is not None else self._cfg.braille_tooltip.cpu_braille_length
        width   = (label_w + 1) + bw + 1 + val_w
        lines   = [cell(i) for i in range(n)]
        body    = '<div class="page">' + "<br>".join(lines) + "</div>"
        footer  = pager_fn(width) if pager_fn else ""
        return self._wrap_tooltip(body, css, header=header, footer=footer)

    def format_top_process(self, r: Readings, css: str = "", header: str = "",
                           pager_fn: Optional[Callable[[int], str]] = None) -> str:
        """The top-processes page: PID (label color) · COMMAND (base color) ·
        %CPU and %MEM colored by the per-process top_process_cpu / top_process_mem
        thresholds (a CPU or memory hog stands out red). Fixed row count; the
        COMMAND column is elastic — it grows to fill the tooltip's canonical width
        (tooltip_width), so the page matches the — usually wider — main page
        instead of sitting narrow, and longer names show when there's room."""
        rows = r.top_process_full
        if not rows:
            return self._wrap_tooltip('<div class="page">top processes: no data yet</div>',
                                      css, header=header)
        cpu_thr = tuple(self._cfg.thresholds.top_process_cpu)
        mem_thr = tuple(self._cfg.thresholds.top_process_mem)
        shown   = rows[:TOP_PROCESS_PAGE_ROWS]
        # Reserve room for a 6-digit PID so the column width stays stable
        # regardless of the PIDs shown; grow only for the rare wider PID.
        pid_w   = max(_TOP_PROCESS_MIN_PID, *(len(str(pid)) for pid, *_ in shown))
        # COMMAND fills whatever's left after the fixed columns at the tooltip's
        # width, down to its minimum — so the page's total width equals the tooltip.
        comm_w  = max(_TOP_PROCESS_COMM_MIN,
                      self._cfg.display.tooltip_width - pid_w - _TOP_PROCESS_SIDE_COLS)

        def field(text: str, w: int, cls: Optional[str] = None, right: bool = True) -> str:
            pad  = "&nbsp;" * max(0, w - len(text))
            body = html.escape(text)
            if cls:                       # no class = the tooltip's base color
                body = f'<span class="{cls}">{body}</span>'
            return pad + body if right else body + pad

        def line(pid: str, comm: str, cpu: str, mem: str, cpu_cls: str, mem_cls: str,
                 comm_cls: Optional[str]) -> str:
            return (field(pid, pid_w, "label") + "&nbsp;&nbsp;"
                    + field(comm, comm_w, comm_cls, right=False)
                    + "&nbsp;" + field(cpu, 4, cpu_cls) + "&nbsp;" + field(mem, 5, mem_cls))

        def clip(name: str) -> str:                 # trailing ellipsis: keep the head (binary + first args)
            return name if len(name) <= comm_w else name[:comm_w - 1] + "…"

        lines = [line("PID", "COMMAND", "%CPU", "%MEM", "label", "label", "label")]
        for pid, comm, cpu, mem in shown:
            lines.append(line(str(pid), clip(comm), str(cpu), f"{mem:.1f}",
                              f"val {css_class_from_thresholds(cpu, cpu_thr)}",
                              f"val {css_class_from_thresholds(mem, mem_thr)}",
                              None))     # COMMAND stays the base color
        body   = '<div class="page">' + "<br>".join(lines) + "</div>"
        footer = pager_fn(pid_w + comm_w + _TOP_PROCESS_SIDE_COLS) if pager_fn else ""
        return self._wrap_tooltip(body, css, header=header, footer=footer)

    # Graph dimensions (px). Qt RichText honors only fixed px on <img>; the image
    # is the widest element on the page, so its width sets the tooltip width. The
    # width comes from cfg.pages.graph_width (derived from the main page's floor
    # and the live tooltip glyph advance), so wheeling onto graphs doesn't resize
    # the box. _GRAPH_LEFT_PAD reserves room for the baked y-axis labels ("100").
    _GRAPH_H = 84
    _GRAPH_LEFT_PAD = 18

    def _graph_val(self, cur: Optional[int], thr) -> str:
        """A legend `.val` span, threshold-colored like the stat cells: tuple
        thr → good/warn/crit bands, int thr → active (binary), None → empty."""
        if cur is None:
            return f'<span class="val">{EMPTY_VALUE}</span>'
        cls = css_class_active(cur, thr) if isinstance(thr, int) else css_class_from_thresholds(cur, thr)
        return f'<span class="val {cls or ""}">{cur}%</span>'

    def _gpu_graph(self, r: Readings):
        """(usage_hist, dec_hist, usage_cur, dec_cur, usage_thr, dec_thr) for the
        active GPU, or None when there's no GPU. Discrete beats integrated
        (Nvidia, then AMD, then Intel), matching sensors._sample_gpu_history."""
        thr = self._cfg.thresholds
        if self._hw.has_nvidia:
            return (r.gpu_usage_history, r.gpu_dec_history, r.gpu_usage, r.gpu_dec,
                    tuple(thr.gpu_nvidia_usage), thr.gpu_nvidia_dec_usage)
        if self._hw.amd_gpu_busy_path:
            # No decoder series: amdgpu exposes no per-engine video utilization,
            # so the chart is the usage area with no overlay line.
            return (r.gpu_usage_history, [], r.gpu_amd_usage, None,
                    tuple(thr.gpu_amd_usage), None)
        if self._hw.intel_gpu_pci:
            return (r.gpu_usage_history, r.gpu_dec_history, r.gpu_intel_usage, r.gpu_intel_dec_usage,
                    tuple(thr.gpu_intel_usage), thr.gpu_intel_dec_usage)
        return None

    def format_graphs(self, r: Readings, css: str = "", header: str = "",
                      pager_fn: Optional[Callable[[int], str]] = None) -> str:
        """The graphs page: systemmonitor-style history area charts stacked —
        CPU, memory, and (when present) the active GPU with usage as the filled
        area plus decoder as an overlaid line. Each is a PNG (chart.py) embedded
        as a data: URI with a text legend below; only the plot is in the image,
        the labels and current values are HTML, threshold-colored."""
        cfg = self._cfg
        w, h, lp = cfg.pages.graph_width, self._GRAPH_H, self._GRAPH_LEFT_PAD

        def png_img(png: bytes) -> str:
            uri = "data:image/png;base64," + base64.b64encode(png).decode()
            return f'<div><img src="{uri}" width="{w}" height="{h}"></div>'

        def legend(entries: list) -> str:
            """One entry per line (so a two-series legend doesn't widen the
            tooltip past the graph), each optionally prefixed by a series-color
            dot mapping it to its line."""
            parts = []
            for color, lbl, val in entries:
                dot = (f'<span style="color:rgb({color[0]},{color[1]},{color[2]})">●</span>&nbsp;'
                       if color else "")
                parts.append(f'{dot}<span class="label">{lbl}:</span>&nbsp;{val}')
            return '<div class="page">' + "<br>".join(parts) + "</div>"

        blocks = [
            png_img(chart.area_chart_png(list(r.cpu_history or []), w, h, left_pad=lp,
                                         line=chart.BLUE_LINE, fill=chart.BLUE_FILL))
            + legend([(chart.BLUE_LINE, "CPU usage", self._graph_val(r.cpu_usage, tuple(cfg.thresholds.cpu_usage)))]),
            png_img(chart.area_chart_png(list(r.mem_history or []), w, h, left_pad=lp,
                                         line=chart.PURPLE_LINE, fill=chart.PURPLE_FILL))
            + legend([(chart.PURPLE_LINE, "Memory usage", self._graph_val(r.mem_usage, tuple(cfg.thresholds.mem_usage)))]),
        ]

        gpu = self._gpu_graph(r)
        if gpu is not None:
            u_hist, d_hist, u_cur, d_cur, u_thr, d_thr = gpu
            png = chart.area_chart_png(list(u_hist or []), w, h, left_pad=lp,
                                       line=chart.GREEN_LINE, fill=chart.GREEN_FILL,
                                       overlay=list(d_hist or []), overlay_line=chart.ORANGE_LINE)
            entries = [(chart.GREEN_LINE, "GPU usage", self._graph_val(u_cur, u_thr))]
            # Skipped for a vendor with no decoder counter (AMD): the row would
            # read "--" forever.
            if d_thr is not None:
                entries.append((chart.ORANGE_LINE, "Decoder", self._graph_val(d_cur, d_thr)))
            blocks.append(png_img(png) + legend(entries))

        if self._hw.net_device:
            # Byte-rate scale is dynamic, so auto-fit vmax to the window peak and
            # skip the (percent) y-labels; the current rates go in the legend.
            # Only the 0 baseline is drawn — the 25/50/75/100 lines would mark
            # fractions of an ever-changing peak (meaningless) and, sitting on
            # top of the auto-scaled fill, would flicker in and out as the fill
            # height changes frame to frame.
            down_h, up_h = list(r.net_down_history or []), list(r.net_up_history or [])
            peak = max([*down_h, *up_h, 1])
            png = chart.area_chart_png(down_h, w, h, left_pad=lp, vmax=peak, label_values=False,
                                       grid_levels=(0,), line=chart.TEAL_LINE, fill=chart.TEAL_FILL,
                                       overlay=up_h, overlay_line=chart.RED_LINE)
            rate = lambda bps: f'<span class="val">{_net_fmt(bps or 0)}</span>'
            blocks.append(png_img(png) + legend([
                (chart.TEAL_LINE, "Download", rate(r.net_down_bps)),
                (chart.RED_LINE,  "Upload",   rate(r.net_up_bps)),
            ]))

        # a small gap below the title, then blank lines between stacked graphs
        top_gap = '<div style="font-size:6px">&nbsp;</div>'
        spacer  = '<div style="font-size:16px">&nbsp;</div>'
        # Centre the pager over the column count the width was derived from
        # (tooltip_width = graph_width / tooltip glyph advance), so the dots sit
        # under the chart like the other pages; a px/char estimate is the fallback
        # when the width isn't set.
        cols    = cfg.display.tooltip_width or (w // 9)
        footer  = pager_fn(cols) if pager_fn else ""
        return self._wrap_tooltip(top_gap + spacer.join(blocks), css, header=header, footer=footer)

    def canonical_width(self, r: Readings) -> int:
        """The tooltip's width in monospace columns: the widest surface it holds
        on this machine, so every page renders at one width and none resizes as
        content fluctuates. Dominated by the full page, rendered against
        _maxed_readings (every volatile field at its bounded max); because the
        maxed fields don't depend on the live values, this is stable across polls
        — it moves only when the machine's item set does (a disk mounted, hardware
        rescanned). The fixed-width processes page can be wider than a sparse main
        page, so its width is folded in when active. 0 when there's nothing to
        measure. Everything else (deep-dive pages' floor, the graphs PNG) sizes to
        this — there is no hand-set width.

        Memoized: a full synthetic render is ~a tooltip build, but the result only
        moves with _canonical_sig (the disk mounts + totals, the net/wifi identity,
        RAM size), so on the common poll where none changed it's a dict-cheap sig
        compare, not a rebuild."""
        key = self._canonical_sig(r)
        if key != self._canonical_key:
            entries = self._build_entries(_maxed_readings(r, self._hw), tooltip=True)
            w = global_width_of(group_rows_into_blocks(entries), 0)
            if "processes" in self._cfg.pages.order:
                w = max(w, _TOP_PROCESS_MIN_WIDTH)
            self._canonical_key, self._canonical_cache = key, w
        return self._canonical_cache

    def _canonical_sig(self, r: Readings):
        """The reading-derived inputs canonical_width depends on (everything else
        it maxes to a constant or seeds from the fixed cfg/hw). Cheap to build and
        compare, so recompute only fires on a real change — a disk mounted/removed
        or resized, the interface/SSID switching, never on a plain value tick."""
        return (
            tuple(sorted((m, du.total_gb) for m, du in r.disk_usage.items() if du)),
            r.net_device, r.wifi_ssid, r.mem_total_gb,
        )

    def format_tooltip(self, r: Readings, css: str = "", timings: Optional[dict[str, float]] = None,
                       pager_fn: Optional[Callable[[int], str]] = None) -> str:
        entries = self._build_entries(r, tooltip=True, timings=timings)
        blocks = group_rows_into_blocks(entries)
        # Fully table-free: Qt Quick's live RichText engine re-balances <table>
        # columns on every content change, costing ~20% plasmashell CPU while
        # the tooltip is open and refreshing (see mono_render / project
        # memory). The values' shared right edge is &nbsp; padding to a global
        # width inside render_blocks_monospace. The 8px visual inset comes from
        # the plasmoid QML (tooltipText padding), not a table here.
        # min_width floors the global width so the tooltip doesn't visibly
        # narrow when wide rows are absent (e.g. the network lines at boot,
        # before the link is up); 0 = off. Panel path above is left unfloored.
        min_width = self._cfg.display.tooltip_width
        body = render_blocks_monospace(blocks, min_width=min_width)
        # The pager centers within the body's monospace width (not the tooltip
        # box), so it doesn't slide while Plasma lazily resizes the popup.
        footer = pager_fn(global_width_of(blocks, min_width)) if pager_fn else ""
        return self._wrap_tooltip(body, css, footer=footer)

    def _build_entries(
        self, r: Readings, tooltip: bool, timings: Optional[dict[str, float]] = None,
    ) -> list[Row | Separator]:
        """Walk the surface's sections (cfg.tooltip vs cfg.panel) in order. Each
        section renders the rows of its enabled items — enabled = listed in the
        section's `items` (membership) AND, for hardware-bound items, present
        (see _available); items whose renderer yields no row are dropped too.
        A section that ends up with zero rows is skipped entirely, title and
        separator included — this is what collapses an empty 'Batteries' section
        on a machine with no batteries.

        The tooltip draws each section's title (a full-width 'title'-role row,
        its own block via the shape-change rule in group_rows_into_blocks) and a
        big Separator between two rendered sections. The panel is a continuous
        compact strip: sections there only drive ordering and hw-collapse — no
        titles, no separators between them. The glyph-only (panel) vs glyph+word
        (tooltip) label form is handled downstream by _label_cell()/_render_item().

        When `timings` is passed, records elapsed ms per item (used by the profiling subcommand)."""
        surface = self._cfg.tooltip if tooltip else self._cfg.panel
        entries: list[Row | Separator] = []
        any_section_rendered = False

        for section in surface.sections:
            section_rows: list[Row | Separator] = []
            has_rows = False
            for name in section.items:
                size = _separator_size(name)
                if size is not None:
                    section_rows.append(Separator(size=size))
                    continue
                if not self._available(name, r):
                    continue
                with timed_section(timings, name):
                    rows = self._render_item(name, r, tooltip)
                if rows:
                    has_rows = True
                    section_rows.extend(rows)
            if not has_rows:
                continue
            if tooltip and any_section_rendered:
                entries.append(Separator(size="big"))
            if tooltip and section.title:
                entries.append([Cell(text=section.title, css_class="title")])
                # Underline rule under the title: a full-width coloured bar, its
                # own 'title-rule'-role single-cell row (its own block via the
                # shape-change rule in group_rows_into_blocks). The text is left
                # empty here — mono_render emits it as a width="100%" div whose
                # background-color/height come from .tooltip .title-rule in
                # style-dark.css (see _emit there).
                entries.append([Cell(text="", css_class="title-rule")])
            entries.extend(section_rows)
            any_section_rendered = True

        return _normalize_separators(entries)

    def _available(self, name: str, r: Readings) -> bool:
        """Hardware gate: True if `name` may render on this machine. Delegates to
        the item registry's per-item `gate` (items.py): hardware-bound items
        return False when their device/sysfs path wasn't discovered (or, for swap,
        when there's no swap), so config can list them generously and each machine
        only shows what it actually has. Items not bound to specific hardware
        (cpu/mem usage, history, uptime, the composed net/wifi/pair rows…) default to True — for
        them visibility is pure config membership. Multi-instance items (fan,
        battery_sys, hd_temp/hd_temp_smart) self-empty in their per-device loop when
        nothing is present."""
        return _registry_gate(self, name, r)

    def _render_item(self, name: str, r: Readings, tooltip: bool) -> list[Row]:
        """Render the rows for one item. Delegates to the item registry
        (items.py): the regular items are data (a list of cells via row()/per()),
        the irregular ones (net/wifi joins, bar/history rows, batteries,
        top_process) stay explicit methods on this class, wrapped as registry
        entries. An unknown name renders nothing."""
        return _registry_render(self, name, r, tooltip)

    # ── Item renderers ────────────────────────────────────────────────────────

    def _label_cell(
        self, ident: Ident, tooltip: bool, text: Optional[str] = None, glyph: Optional[str] = None,
    ) -> Cell:
        """Resolve an item's label cell: glyph only in the panel, glyph + word
        (+ tooltip delimiter, e.g. ':') in the tooltip. Glyph and word both come
        from ident.metric (icons/labels have one row per metric); combo
        sub-labels (live/history) arrive via text= from the call site. The CSS
        class is the final two-axis one (ident.css = item-<metric> form-<form>)."""
        if glyph is None:
            glyph = self._cfg.icons.get(ident.metric, "")
        css_class = f"label {ident.css}"
        if not tooltip:
            return Cell(text=glyph, css_class=css_class)
        word = text if text is not None else self._cfg.labels.get(ident.metric, "")
        dlm  = self._cfg.labels.get("delimiter", "")
        return Cell(text=f"{glyph} {word}{dlm}", css_class=css_class)

    def _battery_sys_is_full(self, bat: BatterySys, pv: int) -> bool:
        """True when at 100%, truly fully-charged, or capped at its charge limit."""
        return pv >= 100 or bat.state == "fully-charged" or (bat.limit is not None and pv >= bat.limit)

    def _battery_sys_icon(self, bat: BatterySys, pv: int) -> str:
        """Pick the battery_sys glyph: charging/full(AC) icons override the
        level icon, which otherwise follows the charge percentage rounded to
        the nearest decile (nf-md-battery_10.._90)."""
        ic = self._cfg.icons
        if bat.state == "charging":
            return ic.get("battery_sys_charging", "")
        if self._battery_sys_is_full(bat, pv):
            return ic.get("battery_sys_full", "")
        level = max(10, min(90, round(pv / 10) * 10))
        return ic.get(f"battery_sys_{level}", "")

    # Long auto-discovered mount basenames (e.g. 'bazzite-nvidia_fedora') would
    # blow out the tooltip's label column; truncate with an ellipsis past this.
    _DISK_LABEL_MAX = 12

    # The two raw, otherwise-unbounded string fields on the full page — cap them
    # so the tooltip width stays predictable (see canonical_width / tooltip_width).
    # Middle ellipsis, not trailing: an SSID's or interface's
    # head and tail ('MyHome…-5GHz', 'br-1a…5e6f') identify it better than its
    # centre. The interface name is kernel-bounded at 15 (IFNAMSIZ) but usually
    # short; this trims the rare long predictable/bridge name.
    _SSID_MAX = 16
    _NETDEV_MAX = 12
    # Per-field caps applied to _string_row's value (ip_address stays raw: an
    # IPv4 is already ≤15). Keyed by the item name _string_row is called with.
    _STR_CAPS = {"net_device": _NETDEV_MAX, "wifi_ssid": _SSID_MAX}

    @staticmethod
    def _middle_ellipsis(s: str, n: int) -> str:
        """`s` shortened to at most `n` chars, keeping the head and tail with a
        single '…' bridging the elided middle (e.g. _middle_ellipsis('abcdefgh',
        6) -> 'abc…gh')."""
        if len(s) <= n:
            return s
        if n <= 1:
            return "…"
        keep = n - 1
        head = (keep + 1) // 2
        tail = keep - head
        return s[:head] + "…" + (s[-tail:] if tail else "")

    @staticmethod
    def _disk_label(mount: str) -> str:
        """Friendly label = basename of the mountpoint (e.g. '/mnt/data' ->
        'Data', '/run/media/user/Backup' -> 'Backup'); '/' -> 'Root'. First
        letter capitalized (rest left as-is). Over-long names are truncated to
        _DISK_LABEL_MAX with a trailing '…'."""
        if mount == "/":
            return "Root"
        label = mount.rstrip("/").rsplit("/", 1)[-1] or mount
        if len(label) > PanelFormatter._DISK_LABEL_MAX:
            label = label[: PanelFormatter._DISK_LABEL_MAX - 1] + "…"
        return label[:1].upper() + label[1:]

    def _disk_smart_icon(self, healthy: Optional[bool]) -> str:
        if healthy is None:
            return ""
        return "OK" if healthy else "KO"

    @staticmethod
    def _disk_smart_class(healthy: Optional[bool]) -> Optional[str]:
        """Binary OK/KO color (active/deactive, same pair as cpu_freq's
        turbo label), independent from hd_temp's own %-based threshold class on
        the value cell right next to it."""
        if healthy is None:
            return None
        return "active" if healthy else "deactive"

    @staticmethod
    def _fmt_disk_space(used_gb: Optional[int], total_gb: Optional[int],
                        used_cls: Optional[str] = None,
                        used_w: int = 0, total_w: int = 0) -> str:
        """Middle column of the disk_usage item: "<used>G / <total>G". The used
        space carries the threshold class (good/warn/crit) in its own span, so
        the color follows the disk's state; the "/" and the total stay on the
        .extra cell's default color.

        used_w/total_w (max widths across all disks, computed by the caller)
        right-align "used" and left-align "total": the `/` characters line up
        vertically and every cell has the same width, so the block stays
        centerable as a unit (see disk_space + centermid in mono_render)."""
        if used_gb is None or total_gb is None:
            return ""
        used_str, total_str = f"{used_gb}G", f"{total_gb}G"
        used = f'<span class="{used_cls}">{used_str}</span>' if used_cls else used_str
        lead  = _nbsp(used_w - len(used_str))
        trail = _nbsp(total_w - len(total_str))
        return f"{lead}{used} / {total_str}{trail}"

    @staticmethod
    def _hd_label(label: str) -> str:
        """Short display label: nvme namespace block device -> controller
        name, then strip the trailing device index — e.g. 'nvme0n1' -> 'nvme0'
        -> 'Nvme'; 'sda' -> 'Sda' (unchanged but capitalized). First letter
        capitalized (rest left as-is)."""
        m = re.match(r"^(nvme\d+)n\d+$", label)
        base = m.group(1) if m else label
        base = base.rstrip("0123456789")
        return base[:1].upper() + base[1:]

    def _pair_grid(
        self,
        ident: Ident,
        keys: Iterable[str],
        read: Callable[[str], object],
        label_text: Callable[[str], str],
        make_value: Callable[[object], Cell],
        tooltip: bool,
    ) -> list[Row]:
        """Shared two-per-row grid behind the pair-form items (hd_temp:pair,
        fan_speed:pair, disk_smart:pair): walk `keys`, skip any whose `read`
        returns None (instance present but no reading — self-empties the item),
        and build a (label, value) pair for the rest. `label_text` formats the
        per-instance name and `make_value` builds the value cell; the glyph,
        tooltip delimiter and css class come from `ident`.

        A single instance becomes a full-width [label, value] row so its value
        sits at the global right edge (a lone item in the 2-per-row grid would
        float mid-width). Two or more pack two per row, an odd last one getting
        a blank filler half so every row keeps the same 2-pair shape (and stays
        in one aligned block); the first half's value gets a trailing gap so it
        doesn't touch the second half's label."""
        dlm    = self._cfg.labels.get("delimiter", "") if tooltip else ""
        glyph  = self._cfg.icons.get(ident.metric, "")
        prefix = f"{glyph} " if glyph else ""   # no leading space when glyph empty
        css    = f"label {ident.css}"
        pairs: list[tuple[Cell, Cell]] = []
        for key in keys:
            v = read(key)
            if v is None:
                continue
            lbl = Cell(text=f"{prefix}{label_text(key)}{dlm}", css_class=css)
            pairs.append((lbl, make_value(v)))
        if not pairs:
            return []
        if len(pairs) == 1:
            return [list(pairs[0])]

        blank_lbl = Cell(text="", css_class=css)
        rows: list[Row] = []
        for i in range(0, len(pairs), 2):
            l1, v1 = pairs[i]
            v1.pad_right += 2
            if i + 1 < len(pairs):
                l2, v2 = pairs[i + 1]
            else:
                l2, v2 = blank_lbl, _val_cell("", ident=ident)
            rows.append(render_two_pair_row(l1, v1, l2, v2))
        return rows

    def _disk_smart_pair(self, r: Readings, tooltip: bool) -> list[Row]:
        """SMART health for every disk that reports it, packed two drives per
        row to save vertical space (e.g. 'nvme: ✓  sda: ✓'). Temperature is NOT
        here — that's hd_temp (thermal); this is the disk-health counterpart and
        belongs with disk_usage under Drives. Disks with no SMART result (virtual
        disks, SMART disabled) are skipped (see _pair_grid). Drive name only, no
        'Disk' word: the glyph already says it."""
        cfg = self._cfg
        if not cfg.disks.smart:
            return []
        labels = sorted(set(self._hw.hd_temp_paths) | set(self._hw.disk_smart_drives))
        ident = Ident("disk_smart", "pair")
        return self._pair_grid(
            ident, labels, r.disk_smart.get, self._hd_label,
            lambda healthy: _val_cell(self._disk_smart_icon(healthy),
                                      self._disk_smart_class(healthy),
                                      ident=ident),
            tooltip,
        )

    def _hd_temp_pair(self, r: Readings, tooltip: bool) -> list[Row]:
        """Tooltip-only two-per-row variant of hd_temp: every disk's
        temperature packed two per row (same grid as _disk_smart_pair, see
        _pair_grid) instead of one row each — meant for when there are many
        disks (4+). Same thresholds as hd_temp (thresholds.hd_temp), but
        WITHOUT the "Disk" prefix: repeating it on every disk in a pair row is
        just noise, the name alone is enough (like _disk_smart_pair)."""
        cfg = self._cfg
        ident = Ident("hd_temp", "pair")
        return self._pair_grid(
            ident, self._hw.hd_temp_paths, r.hd_temps.get, self._hd_label,
            lambda v: _val_cell(f"{v}°{TEMP_SCALE}",
                                css_class_from_thresholds(v, cfg.thresholds.hd_temp),
                                ident=ident),
            tooltip,
        )

    def _fan_speed_pair(self, r: Readings, tooltip: bool) -> list[Row]:
        """Tooltip-only two-per-row variant of fan_speed: every fan's RPM
        packed two per row (same grid as _hd_temp_pair / _disk_smart_pair, see
        _pair_grid) instead of one row each. Dedicated glyph icons["fan_speed"].
        Number only (no "rpm"): repeating it in a pair row would crowd the row."""
        ident = Ident("fan_speed", "pair")
        return self._pair_grid(
            ident, self._hw.fan_paths, r.fan_speeds.get,
            lambda key: f"Fan{key}",
            lambda rpm: _val_cell(FAN_OFF if rpm == 0 else str(rpm), ident=ident),
            tooltip,
        )

    def _string_row(self, name: str, v: Optional[str], tooltip: bool) -> list[Row]:
        """Plain (label, value) row for non-numeric values (net_device,
        ip_address, wifi_ssid) — no unit, no threshold color."""
        ident = Ident(name, "value")
        label_cell = self._label_cell(ident, tooltip)
        if v:
            cap = self._STR_CAPS.get(name)
            text = self._middle_ellipsis(v, cap) if cap else v
        else:
            text = EMPTY_VALUE
        return [[label_cell, _val_cell(text, ident=ident)]]

    def _wifi_signal(self, r: Readings, tooltip: bool) -> list[Row]:
        cfg = self._cfg
        ident = Ident("wifi_signal", "value")
        label_cell = self._label_cell(ident, tooltip)
        v = r.wifi_signal
        if v is None:
            return [[label_cell, _val_cell(EMPTY_VALUE, ident=ident)]]
        thr_low, thr_high = cfg.thresholds.wifi_signal
        cls = css_class_battery(v, thr_low, thr_high)
        return [[label_cell, _val_cell(_fmt_perc(v, tooltip), cls, ident=ident,
                                       min_width=PERCENT_PANEL_WIDTH)]]

    def _net_device_ip(self, r: Readings, tooltip: bool) -> list[Row]:
        """net_device + ip_address on one 2-cell row instead of two, e.g.
        'wlan0 - 192.168.1.5' — saves a row's worth of vertical space."""
        # The interface is middle-truncated to _NETDEV_MAX so a long
        # predictable/bridge name can't blow out the width (the IPv4 is ≤15).
        ident = Ident("net_device_ip", "value")
        label_cell = self._label_cell(ident, tooltip)
        device = self._middle_ellipsis(r.net_device, self._NETDEV_MAX) if r.net_device else EMPTY_VALUE
        ip = r.ip_address or EMPTY_VALUE
        return [[label_cell, _val_cell(f" {device} - {ip}", ident=ident)]]

    def _wifi_ssid_signal(self, r: Readings, tooltip: bool) -> list[Row]:
        """wifi_ssid + wifi_signal on one 2-cell row, e.g. 'MyWifi - 80%' —
        same threshold color as _wifi_signal, applied only to the % part via
        an inline span (the rest of the value cell has no color of its own).
        The SSID is middle-truncated to _SSID_MAX so a long name can't blow out
        the tooltip width."""
        cfg = self._cfg
        label_cell = self._label_cell(Ident("wifi_ssid_signal", "value"), tooltip)
        ssid = self._middle_ellipsis(r.wifi_ssid, self._SSID_MAX) if r.wifi_ssid else EMPTY_VALUE
        v = r.wifi_signal
        if v is None:
            signal_text = EMPTY_VALUE
        else:
            thr_low, thr_high = cfg.thresholds.wifi_signal
            cls = css_class_battery(v, thr_low, thr_high)
            signal_text = f'<span class="{cls}">{_fmt_perc(v, tooltip)}</span>'
        return [[label_cell, _val_cell(f"{ssid} - {signal_text}", ident=Ident("wifi_ssid_signal", "value"))]]

    def _fmt_freq(self, mhz: Optional[float], tooltip: bool) -> str:
        """MHz -> string: GHz with 1 decimal from 1000 MHz up, integer MHz
        below; the unit (' GHz'/' MHz') only appears in the tooltip, not the
        compact panel. Shared by cpu_freq (with turbo) and gpu_intel_freq."""
        if mhz is None:
            return EMPTY_VALUE
        if mhz >= 1000:
            return f"{mhz / 1000:.1f}" + (" GHz" if tooltip else "")
        return f"{int(mhz)}" + (" MHz" if tooltip else "")

    def _uptime(self, r: Readings, tooltip: bool) -> Row:
        label_cell = self._label_cell(Ident("uptime", "value"), tooltip)
        if r.uptime is None:
            return [label_cell, _val_cell(EMPTY_VALUE, ident=Ident("uptime", "value"))]
        days, rem = divmod(r.uptime, 86400)
        hours, minutes = divmod(rem // 60, 60)
        parts = [f"{days}d"] if days else []
        parts += [f"{hours}h", f"{minutes}m"]
        return [label_cell, _val_cell(" ".join(parts), ident=Ident("uptime", "value"))]

    def _load_avg(self, r: Readings, tooltip: bool) -> Row:
        cfg   = self._cfg
        thr   = cfg.thresholds
        label_cell = self._label_cell(Ident("load_avg", "value"), tooltip)
        if r.load_avg is None:
            return [label_cell, _val_cell(EMPTY_VALUE, ident=Ident("load_avg", "value"))]
        one, five, fifteen = r.load_avg
        cores = self._hw.cpu_count

        def colored(v: float, band: list[float]) -> str:
            cls = css_class_from_thresholds(v / cores, tuple(band))
            return f'<span class="{cls}">{v:.2f}</span>'

        joined = " ".join([
            colored(one, thr.load_avg_1),
            colored(five, thr.load_avg_5),
            colored(fifteen, thr.load_avg_15),
        ])
        return [label_cell, _val_cell(joined, ident=Ident("load_avg", "value"))]

    def _top_process(self, r: Readings, tooltip: bool) -> list[Row]:
        cfg = self._cfg
        max_len = cfg.display.top_process_name_max_len
        rows: list[Row] = []
        for i, (name, pct) in enumerate(r.top_process or [], start=1):
            label_cell = self._label_cell(Ident("top_process", "value"), tooltip, text=f"Top {i}")
            if max_len > 0 and len(name) > max_len:
                name = name[:max_len - 1] + "…"
            # Uneven split: the process name needs the most room, label and
            # percentage are always short.
            name_cell = _aux_cell(name, ident=Ident("top_process", "value"))
            val_cell = _val_cell(f"{pct}%", ident=Ident("top_process", "value"))
            rows.append(render_three_col_row(label_cell, name_cell, val_cell))
        # Only processes with measurable CPU this tick — no '--' padding to a
        # fixed count (the popup's height varies anyway now that empty sections
        # collapse, and its width is pinned by content, not by these rows).
        return rows

    def _dual_rate_rows(
        self, name1: str, bps1: Optional[int], name2: str, bps2: Optional[int],
        tooltip: bool,
    ) -> list[Row]:
        """Two byte-rate metrics (net_speed's Up/Down, disk_io's Read/Write)
        formatted via _net_fmt. Tooltip and horizontal panel: the two side by
        side on one paired 4-cell row (render_two_pair_row; mono_render splits
        it into two halves). Vertical panel: two plain 2-cell rows, same shape
        as every other item, so they line up in the same column layout instead
        of forming a separate (and possibly wider) block."""
        id1, id2 = Ident(name1), Ident(name2)
        lbl1 = self._label_cell(id1, tooltip)
        lbl2 = self._label_cell(id2, tooltip)
        val1 = _val_cell(_net_fmt(bps1 or 0), ident=id1)
        val2 = _val_cell(_net_fmt(bps2 or 0), ident=id2)
        if tooltip or not self._vertical:
            # Trailing gap on val1: without it, val1 (right-aligned) and lbl2
            # (left-aligned) sit flush in adjacent columns with nothing between
            # them (e.g. "9KDown:").
            val1.pad_right += 2
            return [render_two_pair_row(lbl1, val1, lbl2, val2)]
        return [[lbl1, val1], [lbl2, val2]]

    def _net_speed(self, r: Readings, tooltip: bool) -> list[Row]:
        return self._dual_rate_rows(
            "net_speed_up", r.net_up_bps, "net_speed_down", r.net_down_bps, tooltip)

    def _disk_io(self, r: Readings, tooltip: bool) -> list[Row]:
        return self._dual_rate_rows(
            "disk_io_read", r.disk_read_bps, "disk_io_write", r.disk_write_bps, tooltip)

    def _battery_sys(self, bat: Optional[BatterySys], tooltip: bool, idx: int = 0) -> Row:
        cfg   = self._cfg
        empty = EMPTY_VALUE

        if bat is None or not bat.perc:
            label_cell = self._label_cell(Ident("battery_sys", "value"), tooltip, text=f"Battery {idx}")
            if tooltip:
                return render_three_col_row(
                    label_cell, _aux_cell("", ident=Ident("battery_sys", "value")),
                    _val_cell(empty, ident=Ident("battery_sys", "value")))
            return [label_cell, _val_cell(empty, ident=Ident("battery_sys", "value"))]

        pv    = int(bat.perc.rstrip("%"))
        label_cell = self._label_cell(
            Ident("battery_sys", "value"), tooltip, text=f"Battery {idx}", glyph=self._battery_sys_icon(bat, pv))
        rate_str = ""
        if bat.rate > 0:
            if bat.state == "charging":
                rate_str = f"+{bat.rate}W"
            elif bat.state == "discharging":
                rate_str = f"-{bat.rate}W"
            else:
                rate_str = f"{bat.rate}W"

        if self._battery_sys_is_full(bat, pv):
            cls = None
        else:
            thr_low, thr_high = cfg.thresholds.battery_sys
            cls = css_class_battery(pv, thr_low, thr_high)

        if tooltip:
            # Rate and limit get their own middle column (label | rate/limit
            # | value), same render_three_col_row shape as top_process, so a
            # long combination doesn't crowd out the percentage's own
            # right-aligned column.
            extra_parts = [rate_str] if rate_str else []
            if bat.limit is not None and pv >= bat.limit:
                extra_parts.append(f"{cfg.icons.get('battery_sys_limit', '')} {bat.limit}%")
            extra_cell = _aux_cell(" ".join(extra_parts), ident=Ident("battery_sys", "value"))
            return render_three_col_row(
                label_cell, extra_cell, _val_cell(bat.perc, cls, ident=Ident("battery_sys", "value")))

        if rate_str and (int(time.time()) // _BATTERY_ALTERNATE_SECONDS) % 2 == 0:
            val = rate_str
        else:
            val = _fmt_perc(pv, tooltip=False)
        return [label_cell, _val_cell(val, cls, ident=Ident("battery_sys", "value"))]

    def _battery_periph(
        self,
        bat: Optional[BatteryPeriph],
        key: str,
        tooltip: bool,
    ) -> Row:
        cfg   = self._cfg
        thr_low, thr_high = getattr(cfg.thresholds, key, [20, 80])
        default_cell = self._label_cell(Ident(key, "value"), tooltip)

        if bat is None or not bat.perc:
            return [default_cell, _val_cell(EMPTY_VALUE, ident=Ident(key, "value"))]

        label_cell = (self._label_cell(Ident(key, "value"), tooltip, text=bat.name) if tooltip and bat.name else default_cell)
        pv    = int(bat.perc.rstrip("%"))
        cls   = None if pv >= 100 else css_class_battery(pv, thr_low, thr_high)
        return [label_cell, _val_cell(_fmt_perc(pv, tooltip), cls, ident=Ident(key, "value"))]

    def _system_updates(self, r: Readings, tooltip: bool) -> Row:
        label_cell = self._label_cell(Ident("system_updates", "value"), tooltip)
        if r.system_updates is None:
            return [label_cell, _val_cell(EMPTY_VALUE, ident=Ident("system_updates", "value"))]
        cls = "crit" if r.system_updates >= 1 else None
        return [label_cell, _val_cell(str(r.system_updates), cls, ident=Ident("system_updates", "value"))]

    def _server_check(self, r: Readings, tooltip: bool) -> Row:
        label_cell = self._label_cell(Ident("server_check", "value"), tooltip)
        if r.server_ok is None:
            return [label_cell, _val_cell(EMPTY_VALUE, ident=Ident("server_check", "value"))]
        cls = None if r.server_ok else "crit"
        return [label_cell, _val_cell("Ok" if r.server_ok else "KO", cls, ident=Ident("server_check", "value"))]
