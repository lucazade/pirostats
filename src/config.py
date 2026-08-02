"""
Configuration loading. Assets live under two roots: the shipped tree (CODE_ROOT
— the repo in dev, /usr/lib/pirostats when packaged) carries the read-only
defaults (config/, style/, lang/); the user's writable XDG dir
(~/.config/pirostats/) carries optional overrides. See default_config_path /
resolve_style / _load_machines for the per-asset resolution.
Supports optional per-machine overrides (machines.toml) auto-detected via the DMI
board/product name.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from runtime import GEOM_FILE


# ── Asset roots ───────────────────────────────────────────────────────────────
# The shipped tree: src/, config/, style/ and lang/ all sit under it, resolved
# relative to this file so the clone/package can live anywhere (dev under
# /mnt/..., a package under /usr/lib/pirostats).
CODE_ROOT = Path(__file__).resolve().parent.parent
# The user's writable override dir. A packaged install ships read-only defaults
# under CODE_ROOT; the user drops config.toml / machines.toml / style/ here to
# customize without touching /usr (the conky model — see the PKGBUILD notes).
XDG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "pirostats"
SHIPPED_CONFIG   = CODE_ROOT / "config" / "config.toml"
SHIPPED_MACHINES = CODE_ROOT / "config" / "machines.toml"


def default_config_path() -> Path:
    """The config.toml loaded when the CLI gives no --config: the user's XDG copy
    (~/.config/pirostats/config.toml) if present, else the shipped default. The
    XDG file replaces the shipped one wholesale (conky model) — the user copies
    the default and edits it, rather than layering on top."""
    xdg = XDG_DIR / "config.toml"
    return xdg if xdg.exists() else SHIPPED_CONFIG


def resolve_style(name: str) -> Path:
    """Path to a style/ asset (a CSS file or icons.toml): the user's XDG override
    (~/.config/pirostats/style/<name>) if present, else the shipped one under
    CODE_ROOT. Resolved independently of the config path so it stays correct when
    config.toml itself is loaded from XDG."""
    xdg = XDG_DIR / "style" / name
    return xdg if xdg.exists() else CODE_ROOT / "style" / name


def user_machines_path() -> Path:
    """The user's own machines (~/.config/pirostats/machines.toml), merged on top of
    the shipped base. Absent on a fresh install; nothing personal ships."""
    return XDG_DIR / "machines.toml"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _from_dict(cls, data: dict):
    """Instantiate a dataclass from a dict, ignoring unknown keys."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


# ── Config dataclasses ────────────────────────────────────────────────────────

# Built-in lower bound for the tooltip width (monospace columns): keeps the
# tooltip from looking cramped on a sparse config, before the main page's
# canonical width (usually larger) takes over. Not a user knob — a sensible
# minimum. See DisplayConfig.tooltip_width / config.apply_canonical_width.
TOOLTIP_WIDTH_FLOOR = 30


@dataclass
class DisplayConfig:
    poll_interval: float = 1.5
    history_interval: float = 1.5       # cadence at which the shared history buffer takes one sample, independent of poll_interval. Read by every consumer of that buffer: the spark and braille forms on both surfaces, and the graphs page (so a spark's time window = its length × this).
    language: str = "en"                # label language: loads lang/<language>.toml (i18n); see Config.labels
    top_process_name_max_len: int = 20  # compact top_process item (Top 1/2/3) only: truncate its comm names past this (0 = off). Its names are /proc/stat comm, kernel-capped at 15, so this only bites below 15; the processes PAGE names from cmdline and sizes its own elastic column. Not in config.toml.
    panel_font_size: int = 13           # alignment divisor for values at the bar's edge (the bar spans width*height/panel_font_size columns, see traces.bar_row). AUTO-derived by the vertical Plasma panel's auto-fit (config._auto_fit_panel); this default is only the fallback outside Plasma. Not set in config.toml.
    tooltip_width: int = TOOLTIP_WIDTH_FLOOR  # the RESOLVED tooltip width every page + the graphs PNG render to = max(TOOLTIP_WIDTH_FLOOR, main-page canonical). Set at runtime by apply_canonical_width ← PanelFormatter.canonical_width (the widest tooltip surface on this machine), so the pages all size to the main page and none resizes as content fluctuates. The default is the bare floor, used until it's computed. Not from config.toml; tooltip only, not the vertical panel.
    panel_min_width: int = 5            # minimum VERTICAL PANEL width in monospace columns (0 = off): the panel twin of tooltip_width on mono_render's global_width. AUTO-derived (= columns that fit the panel) by the Plasma auto-fit; this default is the fallback outside Plasma. Not set in config.toml.
    overlay: bool = False               # inspection overlay: style-overlay.css's per-cell diagnostic backgrounds on the live panel/tooltip. Toggle here (hot-reloaded), watch it on the widget. Off in production.


@dataclass
class PagesConfig:
    """The tooltip's deep-dive pages: which ones the mouse wheel cycles through,
    and the knobs of the only page that has any (graphs). The full stats view is
    always page 0 and is never listed in `order`."""
    # Deep-dive pages in wheel order; this lists what follows page 0. Remove/reorder
    # freely; an empty list means no pager at all (just the full view). Unknown ids
    # are ignored.
    order: list[str] = field(default_factory=lambda: ["processes", "cpu_cores", "connections", "fastfetch"])
    graph_history_length: int = 60      # samples the graphs page's history charts keep; only extends the shared cpu/mem history buffer when "graphs" is enabled (see sensors._read_cpu_usage/_read_mem_usage)
    graph_width: int = 315              # graphs page PNG width in px. AUTO-derived (= display.tooltip_width cols × the tooltip glyph advance the plasmoid publishes) so the charts match the main page's width at any tooltip font size; this default is the fallback until a geom with the tooltip advance arrives. Not set in config.toml.


@dataclass
class BarConfig:
    """Visual-only knobs for the cpu_usage:bar/mem_usage:bar bar — whether/where it
    renders at all is decided by the cpu_usage:bar/mem_usage:bar item in
    [panel]/[tooltip], not by anything here (see traces.bar_row).

    height is the bar glyphs' font-size in px (Qt RichText ignores CSS height,
    so the visual height of '█'/empty is its font-size); 0 = inherit the
    surface default, no inline style emitted. Besides making the bar shorter, a
    small font-size also makes its N chars much narrower in pixels — which is
    what keeps a wide bar from driving the vertical panel's width. See
    traces.bar_html.

    width is AUTO-derived in the vertical Plasma panel (config._auto_fit_panel
    sizes it to fill the real panel width); this default is only the fallback used
    outside Plasma, hence non-zero so the bar still renders there. Not in config.toml.

    Only the bar's size lives here: its glyphs are the form itself, fixed as
    traces.BAR_FILL_CHAR/BAR_EMPTY_CHAR, and its colour is CSS."""
    width: int = 22
    height: int = 0


@dataclass
class SparkConfig:
    """Visual-only knobs for the block spark (cpu_usage:spark/mem_usage:spark) —
    length in chars; the color comes from the cpu_spark/mem_spark
    thresholds. Enable/disable lives in the item toggle, not here. The sampling
    cadence of the shared history buffer is display.history_interval."""
    cpu_spark_length: int = 5
    mem_spark_length: int = 5


@dataclass
class BrailleConfig:
    """Visual-only knobs for the braille spark (cpu_usage:braille/mem_usage:braille) —
    length in chars (2 samples/char internally). Colored by the grad-* gradient,
    no threshold. Independent from SparkConfig so the two can have different
    widths; both read the same history buffer (display.history_interval)."""
    cpu_braille_length: int = 5
    mem_braille_length: int = 5


@dataclass
class ColumnConfig:
    """Visual-only knobs for the cpu_usage/mem_usage column (the :bar form in the
    horizontal panel, rendered as a vertical column):

    width  — how many glyphs wide the column is (its thickness); the manual knob,
             the horizontal-panel twin of bar_panel.height. Default 1 (one glyph).
    height — the block glyph's font-size in px (Qt RichText ignores CSS height), i.e.
             how tall the column stands; 0 = inherit, no inline style. AUTO-fit in
             the horizontal Plasma panel (config._auto_fit_panel sizes it to the digit
             height so the column matches the values beside it) — not in config.toml,
             this default is only the fallback outside Plasma.

    Palette and the grey track background stay in style-dark.css (.item-<metrica>.form-column). See
    traces.column_html."""
    width: int = 1
    height: int = 0


# cpu_usage:braille/mem_usage:braille pack 2 samples/char (see traces.braille_html)
# — to occupy the same visual width as the 1-sample/char block spark at the
# same *_history_length, they need 2x the underlying samples. sensors.py sizes
# its history deque off this so the buffer is never the bottleneck, regardless
# of whether braille items are actually enabled (the extra ints cost nothing).
BRAILLE_LENGTH_MULTIPLIER = 2


# ── Sections (panel/tooltip structure) ────────────────────────────────────────
# A surface (panel or tooltip) is an ordered list of typed Sections; each Section
# has a title (rendered only in the tooltip) and an ordered list of item names
# (membership = the item is enabled; list order = render order). Whether an item
# actually shows is membership AND its hardware gate (see formatter._available):
# a section with no visible item collapses entirely, title and separator included.

@dataclass
class Section:
    key: str
    title: str = ""
    items: list[str] = field(default_factory=list)


@dataclass
class Surface:
    sections: list[Section] = field(default_factory=list)
    # PANEL only: show the label glyph next to each value; False = value only, the
    # label() cell-factory emits no cell at all — a panel label IS just the glyph.
    # Read off the surface so it rides the orientation override like items do
    # ([panel_horizontal] glyphs = false, where the space is tightest). It has no
    # meaning on the tooltip, whose label is glyph+word (dropping the cell there
    # would drop the word too), so cfg.tooltip.glyphs is never read.
    glyphs: bool = True

    def has(self, name: str) -> bool:
        """True if `name` is a member of any section (i.e. enabled by config,
        before the hardware gate)."""
        return any(name in s.items for s in self.sections)

    def item_set(self) -> set[str]:
        return {it for s in self.sections for it in s.items}


@dataclass
class ThresholdConfig:
    """3-band color thresholds: [mid, high]. Below 'mid' -> low color, between mid and high -> mid, from high -> high."""
    cpu_usage: list[int] = field(default_factory=lambda: [70, 90])
    # Sparks sit lower than their value twins on purpose: the block height already
    # carries the level, so the colour is free to mark activity below the point
    # where the number itself deserves attention.
    cpu_spark: list[int] = field(default_factory=lambda: [55, 80])
    mem_spark: list[int] = field(default_factory=lambda: [50, 75])
    mem_usage: list[int] = field(default_factory=lambda: [60, 80])
    # Top-processes page: per-process bands, distinct from the system-wide ones
    # above — a single process rarely reaches 60% RAM, so those would never fire.
    top_process_cpu: list[int] = field(default_factory=lambda: [70, 90])
    top_process_mem: list[int] = field(default_factory=lambda: [25, 50])
    swap_usage: list[int] = field(default_factory=lambda: [50, 70])
    # The temperature and disk bands end where NotifyThresholds fires, so the crit
    # colour and the desktop alert mean the same thing. A usage band's mid, though,
    # depends on what the percentage counts: a rate is healthy at full scale (a
    # pinned CPU is working, nothing runs out) so it sits high, while occupancy of
    # something finite fails there (OOM, VRAM spill, ENOSPC) and the colour tracks
    # the headroom, so it starts lower. disk_usage is the earliest of the occupancy
    # three: the only slow, monotone one, its yellow means "trajectory", not "now".
    disk_usage: list[int] = field(default_factory=lambda: [60, 80])
    cpu_temp: list[int] = field(default_factory=lambda: [70, 80])
    gpu_nvidia_temp: list[int] = field(default_factory=lambda: [70, 80])
    gpu_nvidia_usage: list[int] = field(default_factory=lambda: [70, 90])
    gpu_nvidia_mem_usage: list[int] = field(default_factory=lambda: [60, 80])
    gpu_intel_usage: list[int] = field(default_factory=lambda: [70, 90])
    hd_temp: list[int] = field(default_factory=lambda: [55, 60])
    # Batteries: inverted logic (low charge = alarm): [red, green]. A charge only
    # falls, so a green cutoff at 80 left four fifths of the range amber; the red
    # cutoff is NotifyThresholds' own, making amber "find the charger" and red the
    # alert. battery_sys goes lower on both: a laptop at 20% still has time, a
    # peripheral reports in coarse steps and dies without warning.
    battery_sys: list[int] = field(default_factory=lambda: [10, 30])
    battery_mouse: list[int] = field(default_factory=lambda: [20, 40])
    battery_kbd: list[int] = field(default_factory=lambda: [20, 40])
    # Wifi signal: same inverted logic as batteries (low % = weak signal = alarm).
    wifi_signal: list[int] = field(default_factory=lambda: [30, 60])
    # Single-value binary threshold: v > threshold -> green (active), otherwise no color.
    gpu_nvidia_dec_usage: int = 1
    gpu_intel_dec_usage: int = 1
    # Load avg: thresholds as a fraction of cores (v / nproc), not an absolute value ->
    # [mid, high] stays correct regardless of how many cores the machine has. The longer
    # the window, the lower the thresholds: sustained load is worse than a brief spike.
    load_avg_1: list[float] = field(default_factory=lambda: [1.0, 1.5])
    load_avg_5: list[float] = field(default_factory=lambda: [0.9, 1.3])
    load_avg_15: list[float] = field(default_factory=lambda: [0.8, 1.1])


@dataclass
class NotifyThresholds:
    """Thresholds that trigger a desktop notification (independent of color thresholds)."""
    disk_usage: int = 80
    cpu_temp: int = 80
    gpu_nvidia_temp: int = 80
    hd_temp: int = 60
    battery_sys: int = 10
    battery_mouse: int = 20
    battery_kbd: int = 20
    # Debounce shared by cpu_temp/gpu_nvidia_temp/hd_temp (notifier._sustained):
    # seconds a reading must hold over its threshold to notify, and degrees it
    # must then fall below it to re-arm. Boost bursts cross the trip point for a
    # fraction of a second, so without these an idle machine alerts on noise.
    temp_sustain_seconds: int = 60
    temp_hysteresis: int = 5
    # Load avg 15min: fraction of cores (v / nproc) and minimum duration above
    # threshold. Kept equal to ThresholdConfig.load_avg_15's crit end, so the alert
    # doesn't fire while the row is still amber.
    load_avg_15: float = 1.1
    load_avg_minutes: int = 10


@dataclass
class NotificationConfig:
    disk_usage: bool = True
    disk_smart: bool = True
    # OFF by default: under normal heavy load (gaming, compiling) these fire
    # correctly but constantly and read as noise — see config.toml [notifications].
    cpu_temp: bool = False
    gpu_nvidia_temp: bool = False
    load_avg: bool = False
    hd_temp: bool = True
    battery_sys: bool = True
    battery_mouse: bool = True
    battery_kbd: bool = True
    server_check: bool = False


@dataclass
class SensorOverrides:
    """Manual hwmon sensor spec in 'chip|file' format (same as bash conf)."""
    cpu_temp: str | None = None
    fan1_speed: str | None = None
    fan2_speed: str | None = None
    fan3_speed: str | None = None
    fan4_speed: str | None = None
    hd1_temp: str | None = None
    hd2_temp: str | None = None
    hd3_temp: str | None = None
    hd4_temp: str | None = None


@dataclass
class DiskConfig:
    # "auto" discovers the real current mounts under auto_roots (plus "/" always)
    # via psutil.disk_partitions(), so external drives appear/disappear on their
    # own. Or an explicit list of mountpoints for manual control (e.g. ["/", "/mnt/data"]).
    mounts: list[str] | str = "auto"
    auto_roots: list[str] = field(default_factory=lambda: ["/mnt", "/media", "/run/media"])
    smart: bool = True
    # SMART self-assessment changes on the order of days, not seconds, so it's
    # checked on a long TTL rather than every poll. The SmartUpdate call is cheap
    # on SSD/NVMe (~15-50ms) but slow on spinning HDDs — the ATA command can cost
    # ~0.5s and *wakes the disk* from power-saving — so rotational drives get a
    # much longer TTL (selected per-disk in collect() via the kernel's rotational
    # flag, not by ata/nvme: a SATA SSD is 'ata' but not rotational).
    smart_interval: float = 3600.0       # SSD/NVMe (non-rotational): 1h
    smart_interval_hdd: float = 21600.0  # spinning HDD (rotational): 6h


@dataclass
class BatteryConfig:
    mouse_unifying: str | None = None
    kbd_unifying: str | None = None
    mouse_bolt: int | None = None
    kbd_bolt: int | None = None
    mouse_name: str | None = None
    kbd_name: str | None = None


@dataclass
class SystemUpdatesConfig:
    # Path written by an external updates checker (e.g. a systemd --user timer
    # running `checkupdates | wc -l > FILE`). The daemon only reads this file —
    # no subprocess, no shell — so the poll loop never blocks. Empty = disabled.
    file: str = ""


@dataclass
class ServerCheckConfig:
    # Path written by an external ping checker ("1" = reachable, "0" = not).
    # The daemon only reads this file — no in-loop ping subprocess that would
    # stall the poll for the ping's round-trip. Empty = disabled.
    file: str = ""


@dataclass
class Config:
    display: DisplayConfig = field(default_factory=DisplayConfig)
    bar_panel: BarConfig = field(default_factory=BarConfig)
    column_panel: ColumnConfig = field(default_factory=ColumnConfig)
    bar_tooltip: BarConfig = field(default_factory=BarConfig)
    spark_panel: SparkConfig = field(default_factory=SparkConfig)
    spark_tooltip: SparkConfig = field(default_factory=SparkConfig)
    braille_panel: BrailleConfig = field(default_factory=BrailleConfig)
    braille_tooltip: BrailleConfig = field(default_factory=BrailleConfig)
    panel: Surface = field(default_factory=Surface)
    tooltip: Surface = field(default_factory=Surface)
    pages: PagesConfig = field(default_factory=PagesConfig)
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)
    notify_thresholds: NotifyThresholds = field(default_factory=NotifyThresholds)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    # Glyphs (theme, style/icons.toml) and labels (i18n, lang/<language>.toml):
    # flat metric→string tables, loaded from external files like style-dark.css —
    # NOT part of the config, no override. labels["delimiter"] separates label
    # and value in the tooltip; labels["history"] is the history side of combos.
    icons: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    sensors: SensorOverrides = field(default_factory=SensorOverrides)
    disks: DiskConfig = field(default_factory=DiskConfig)
    battery: BatteryConfig = field(default_factory=BatteryConfig)
    system_updates: SystemUpdatesConfig = field(default_factory=SystemUpdatesConfig)
    server_check: ServerCheckConfig = field(default_factory=ServerCheckConfig)
    machine: str = ""   # name of the matched machine block (machines.toml), "" if none matched
    # Panel orientation resolved once at load (auto-detected from the Plasma
    # panel edge, or forced via load_config(vertical=…)): picks the
    # [panel_horizontal]/[panel_vertical] override AND drives the formatter's
    # root class, so the two never disagree.
    vertical: bool = False


# ── Machine detection ─────────────────────────────────────────────────────────

def detect_machine(machines: dict) -> str | None:
    """The name of the first machine block whose [<name>.detect] matches this host's
    DMI board/product name, or None when none match (only config.toml applies)."""
    try:
        board   = Path("/sys/class/dmi/id/board_name").read_text().strip()
        product = Path("/sys/class/dmi/id/product_name").read_text().strip()
    except OSError:
        return None

    for name, mdata in machines.items():
        if not isinstance(mdata, dict):
            continue
        d = mdata.get("detect", {})
        if not d:
            continue
        if d.get("board_contains", "") and d["board_contains"] in board:
            return name
        if d.get("board_startswith", "") and board.startswith(d["board_startswith"]):
            return name
        if d.get("product_contains", "") and d["product_contains"] in product:
            return name
    return None


# ── TOML loading ──────────────────────────────────────────────────────────────

def _build_section(cls, raw: dict, key: str):
    return _from_dict(cls, raw.get(key, {}))


def _load_toml_at(path: Path) -> dict:
    """Load a flat TOML table (metric→string) from an absolute path, or {} if it's
    missing/unreadable. Used for the glyphs (style/icons.toml, theme) and labels
    (lang/<language>.toml, i18n) — external files like style-dark.css, resolved
    against the asset roots (see resolve_style / CODE_ROOT), not the config."""
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _resolve_items(sec: dict) -> list[str]:
    """Final ordered item list for one section: base `items`, then `items_add`
    (appended if absent) and `items_remove` applied — the additive/subtractive
    override knobs a machine block uses to extend a section without restating it.
    `items` itself, when set by a machine block, has already replaced the base list
    via _deep_merge before we get here."""
    items = list(sec.get("items", []))
    for add in sec.get("items_add", []):
        if add not in items:
            items.append(add)
    for rm in sec.get("items_remove", []):
        if rm in items:
            items.remove(rm)
    return items


def _parse_surface(raw_surface: dict) -> Surface:
    """Build a Surface from a [panel]/[tooltip] table: `order` (+ a machine's
    `order_add`) lists section keys in render order; each key maps to a section
    sub-table with `title` and `items`. Scalar keys beside them are the surface's
    own options (`glyphs`), merged by the machine/orientation overrides like the
    sub-tables and skipped by the `order` walk below."""
    order = list(raw_surface.get("order", []))
    for k in raw_surface.get("order_add", []):
        if k not in order:
            order.append(k)
    sections: list[Section] = []
    for key in order:
        sec = raw_surface.get(key)
        if not isinstance(sec, dict):
            continue
        sections.append(Section(key=key, title=sec.get("title", ""), items=_resolve_items(sec)))
    return Surface(sections=sections, glyphs=bool(raw_surface.get("glyphs", True)))


# ── Panel geometry: real width + glyph advance published by the plasmoid ──────
# The vertical panel's three knobs (bar width, panel_font_size, panel_min_width)
# must make the bar fill the panel and line the values up at its right edge. All
# three follow from two facts only the running widget truly knows:
# the usable width of its text area in px and the on-screen advance of one monospace
# glyph. The panel text is drawn at font.pointSize (advance depends on screen DPI)
# and Plasma eats an unknown margin around the applet, so deriving these from the
# on-disk config would mean guessing both — instead our (self-maintained) applet
# plasmoid measures them live and writes them to GEOM_FILE, which we read here and
# turn into the knobs (see _auto_fit_panel). Only the bar's `height` (strip
# thickness) is left as a manual aesthetic choice.

PLASMA_APPLETSRC = Path.home() / ".config/plasma-org.kde.plasma.desktop-appletsrc"
# GEOM_FILE (runtime.py) is written by the plasmoid: "<usable_px> <glyph_adv_px>
# <vertical 0|1>". usable_px is output.width (the text area's real width),
# glyph_adv_px the real advance of one mono glyph (TextMetrics, so it reflects
# pointSize+DPI), vertical the panel edge.
# Persistent copy of the last valid GEOM_FILE. The runtime dir is tmpfs, cleared at
# logout, so at KDE session start GEOM_FILE is absent until the plasmoid republishes it —
# seeding the boot load from this cache (see _read_geom_file) makes the very
# first paint already width-fitted instead of rendering the unfitted defaults
# and visibly reflowing a moment later. Survives reboots; refreshed by the
# daemon via cache_live_geom() whenever the plasmoid publishes a new geometry.
GEOM_CACHE = Path.home() / ".cache/pirostats/geom"

# The bar's block glyphs render at a CSS px font-size (formatter emits
# font-size:{h}px), whose on-screen advance is h * this — the monospace advance
# ratio, DPI-independent and constant across sizes (measured via QFontMetricsF).
# The main text's advance is NOT this (it's pointSize-based, DPI-scaled); that one
# we take live from the plasmoid rather than compute.
_CSS_ADVANCE_RATIO = 0.6
# Px shaved off the usable width when sizing the bar. The CSS-px advance isn't
# exactly h·0.6 at every height: font hinting rounds each glyph's advance per size,
# so the last of many glyphs can land a pixel past the edge and wrap (seen at even
# heights). floor + this reserve keep the bar just inside for any height, at the
# cost of at most ~1px of fill. Only the bar needs it — spark/braille use the
# real measured glyph_adv, not a fixed ratio.
_BAR_SAFETY_PX = 1
# Horizontal panel: the *_column glyph (a full/eighth block) fills its whole font
# cell, so at the text's own font-size its grey track stands taller than the digits
# beside it (the digit inks only its cap height). This is the ratio of the digit's
# on-screen height to the block cell's at the same font-size (measured from a real
# render): the column's CSS-px font-size is main_px * this so the track matches the
# digit height. Size-independent (both scale linearly with the font), so it holds at
# any plasmoid font size.
_COLUMN_DIGIT_RATIO = 0.612


@dataclass
class PanelGeometry:
    """Panel facts used at load time: orientation (always resolvable, defaults
    vertical) and — when the plasmoid has published GEOM_FILE — the text area's
    usable width and the real glyph advance in px that drive the vertical auto-fit.
    usable_px/glyph_adv are None when the file is absent (widget not up yet, or a
    non-Plasma render), in which case the config's own bar values stand."""
    vertical: bool = True
    usable_px: float | None = None
    glyph_adv: float | None = None
    # Real on-screen advance of one monospace glyph at the TOOLTIP font size
    # (published alongside the panel's), orientation-independent. Sizes the graphs
    # page PNGs to the tooltip's real text width. None on old 3-field geoms.
    tooltip_adv: float | None = None


def _parse_kde_ini(text: str) -> dict[str, dict[str, str]]:
    """KDE's appletsrc as {full-bracketed-header: {key: value}}. Headers keep their
    exact nested form (e.g. '[Containments][2][Applets][25]') so callers match them
    with a regex; a hand parse avoids configparser's greedy-header surprises on the
    '[a][b]' section names."""
    sections: dict[str, dict[str, str]] = {}
    cur: str | None = None
    for line in text.splitlines():
        if line.startswith("[") and line.endswith("]"):
            cur = line
            sections.setdefault(cur, {})
        elif cur is not None and "=" in line:
            k, v = line.split("=", 1)
            sections[cur][k.strip()] = v.strip()
    return sections


def _int_or_none(s: str | None) -> int | None:
    try:
        return int(s) if s is not None else None
    except ValueError:
        return None


# Our applet's section header: the panel containment (group 1) followed by one or
# more [Applets][N] levels (the widget may sit directly in the panel or nested in a
# systemtray). The whole header (group 0) is the applet root for its Configuration.
_APPLET_ROOT_RE = re.compile(r'^(\[Containments\]\[(\d+)\](?:\[Applets\]\[\d+\])+)$')


def _detect_vertical_from_appletsrc() -> bool:
    """Panel orientation from the live appletsrc: our applet's
    containment edge (location 5/6 = left/right → vertical, 3/4 = top/bottom →
    horizontal), defaulting to vertical. The fallback for `vertical` when the
    plasmoid hasn't published GEOM_FILE yet (which also carries the orientation)."""
    try:
        sections = _parse_kde_ini(PLASMA_APPLETSRC.read_text())
    except OSError:
        return True
    for sec, kv in sections.items():
        m = _APPLET_ROOT_RE.match(sec)
        if m and kv.get("plugin") == "com.github.lucazade.pirostats":
            loc = _int_or_none(sections.get(f"[Containments][{m.group(2)}]", {}).get("location"))
            return loc in (5, 6) if loc in (3, 4, 5, 6) else True
    return True


def _parse_geom(text: str) -> PanelGeometry | None:
    """Parse a geom line ("<usable_px> <glyph_adv_px> <vertical 0|1> [tooltip_adv_px]"),
    or None if malformed/degenerate — so a half-written or startup-zero file falls
    back to the config's own values rather than producing a nonsensical fit. The
    tooltip advance is optional (absent on geoms from the pre-tooltip-metrics
    plasmoid)."""
    parts = text.split()
    if len(parts) < 3:
        return None
    try:
        usable, adv = float(parts[0]), float(parts[1])
        tip = float(parts[3]) if len(parts) > 3 else None
    except ValueError:
        return None
    if usable <= 0 or adv <= 0:
        return None
    if tip is not None and tip <= 0:
        tip = None
    return PanelGeometry(vertical=parts[2] == "1", usable_px=usable, glyph_adv=adv,
                         tooltip_adv=tip)


def _read_geom_file() -> PanelGeometry | None:
    """The live GEOM_FILE, or the persisted GEOM_CACHE when it's absent/degenerate
    (None if neither is usable). At session start /tmp is wiped, so only the cache
    carries a fit until the plasmoid republishes — this is what lets the boot
    paint be width-fitted from the previous session instead of the unfitted
    defaults. The cache write-back is the daemon's job (cache_live_geom), not this
    read path, so config stays side-effect-free."""
    try:
        geo = _parse_geom(GEOM_FILE.read_text())
    except OSError:
        geo = None
    if geo is not None:
        return geo
    try:
        return _parse_geom(GEOM_CACHE.read_text())
    except OSError:
        return None


def cache_live_geom() -> None:
    """Persist the current live GEOM_FILE to GEOM_CACHE when it's valid, so the
    next cold start (tmpfs wiped) can seed its first paint from it. Best-effort:
    a read/write failure must never disturb a render. Called by the daemon when
    the plasmoid publishes a fresh geometry."""
    try:
        text = GEOM_FILE.read_text()
    except OSError:
        return
    if _parse_geom(text) is None:
        return
    try:
        GEOM_CACHE.parent.mkdir(parents=True, exist_ok=True)
        GEOM_CACHE.write_text(text)
    except OSError:
        pass


def detect_panel_geometry() -> PanelGeometry:
    """Panel geometry for load_config. Orientation comes from appletsrc, which
    Plasma updates synchronously when the panel moves — NOT from the plasmoid's
    GEOM_FILE, which the widget republishes asynchronously and so can momentarily
    still report the previous edge right after a flip (that stale flag would pick
    the wrong [panel_vertical]/[panel_horizontal] override → columns in a vertical
    panel). GEOM_FILE only adds the measured width/advance for the auto-fit, and
    only when its own orientation flag matches the resolved one — a geom still
    describing the old edge was measured there (its usable_px is the other axis), so
    we keep the orientation but skip its numbers until the widget catches up (the
    daemon reloads on the next geom write)."""
    vertical = _detect_vertical_from_appletsrc()
    geo = _read_geom_file()
    # The tooltip advance is orientation-independent, so keep it even when a stale
    # geom still describes the old edge (whose panel usable_px/glyph_adv we drop).
    tip = geo.tooltip_adv if geo is not None else None
    if geo is not None and geo.vertical == vertical:
        return PanelGeometry(vertical=vertical, usable_px=geo.usable_px,
                             glyph_adv=geo.glyph_adv, tooltip_adv=tip)
    return PanelGeometry(vertical=vertical, tooltip_adv=tip)


def detect_vertical_layout() -> bool:
    """Orientation alone (see detect_panel_geometry) — kept as the narrow
    entry point where only the panel edge matters."""
    return detect_panel_geometry().vertical


def _auto_fit_panel(cfg: "Config", geo: PanelGeometry) -> None:
    """Size the panel visuals to the real panel, in place, from what the plasmoid
    measures live (see PanelGeometry). Both orientations need the glyph advance; the
    vertical branch also needs the usable width. No-op without the glyph advance (the
    plasmoid hasn't published) — the config's own values stand.

    VERTICAL — everything follows from one number, the main-font columns that fit,
    `cols = usable / glyph_adv`:
      • panel_min_width ← cols, so bar-less sections (thermal, batteries) run the
        value out to the true right edge instead of a guessed floor
      • bar width       ← usable / (height·0.6) (floored, 1px reserve), so the bar's
        CSS-px block glyphs fill the usable width without a hinting-rounding wrap
      • panel_font_size ← the divisor that makes traces._bar_layout_width
        (round(width·height/pfs)) land back on exactly `cols`, so the bar's column
        footprint equals the values' right edge — the two share one edge by
        construction. (This is why panel_font_size isn't just the font size: it
        absorbs BOTH the bar's CSS-px advance and the text's pointSize one.)
      • spark/braille panel lengths ← cols: the standalone spanning visuals
        (cpu_usage/mem_usage spark/braille) render at the main font (no small-font
        knob), so one glyph is one column — cols glyphs fill the same width as the
        bar. The history buffer follows automatically (sensors sizes it live off
        these lengths). Tooltip lengths are untouched (separate, fixed-width surface).
      `height` (strip thickness) is the one knob left to the user; changing it
      re-derives width and the divisor, so the fit holds.

    HORIZONTAL — the *_column glyph fills its whole font cell, so at the text font
    size its grey track stands taller than the digits beside it; column_panel.height
    ← main_px · _COLUMN_DIGIT_RATIO shrinks it to the digit height (main_px from the
    advance). usable width is meaningless here (the widget sizes to its content)."""
    if not geo.glyph_adv:
        return
    if cfg.vertical:
        if not geo.usable_px:
            return
        usable = geo.usable_px
        # floor: cols·glyph_adv ≤ usable, so the value column never overflows and wraps.
        cols = max(1, int(usable / geo.glyph_adv))
        h = cfg.bar_panel.height
        # bar glyph advance: CSS px (font-size:{h}px) when height is set, else it inherits
        # the main pointSize font and shares its measured advance.
        bar_adv = h * _CSS_ADVANCE_RATIO if h > 0 else geo.glyph_adv
        # floor (not round) + a 1px reserve: never let hinting rounding push the last
        # glyph past the edge into a wrap (see _BAR_SAFETY_PX).
        cfg.bar_panel.width = max(1, int((usable - _BAR_SAFETY_PX) / bar_adv))
        cfg.display.panel_min_width = cols
        # Standalone spark/braille span the width at the main font → one glyph per
        # column, so cols glyphs fill it like the bar.
        cfg.spark_panel.cpu_spark_length = cols
        cfg.spark_panel.mem_spark_length = cols
        cfg.braille_panel.cpu_braille_length = cols
        cfg.braille_panel.mem_braille_length = cols
        # Only meaningful with a small-font bar (height>0); with height 0
        # _bar_layout_width returns None and the bar's real width (≈ cols) is used.
        if h > 0:
            cfg.display.panel_font_size = max(1, round(cfg.bar_panel.width * h / cols))
    else:
        # main font pixel size from its advance (glyph_adv = main_px · 0.6), then the
        # column font-size that makes the block track as tall as the digits.
        main_px = geo.glyph_adv / _CSS_ADVANCE_RATIO
        cfg.column_panel.height = max(1, round(main_px * _COLUMN_DIGIT_RATIO))


def apply_canonical_width(cfg: "Config", canonical: int) -> None:
    """Resolve the tooltip width the pages render to = max(TOOLTIP_WIDTH_FLOOR,
    canonical), then re-derive the graphs PNG width from it. `canonical` comes from
    PanelFormatter.canonical_width (which needs a formatter + readings, so this
    can't run inside load_config); the daemon/render call it once cfg, hw and a
    readings snapshot exist. 0 = skip (nothing to measure), leaving the default.

    tooltip_width is written fresh from the FLOOR each call (not read back), so a
    shrinking canonical — a disk unmounted, the interface shortened — lowers the
    width again instead of the field ratcheting up against its own previous max."""
    if canonical <= 0:
        return
    d = cfg.display
    d.tooltip_width = max(TOOLTIP_WIDTH_FLOOR, canonical)
    geo = detect_panel_geometry()
    if geo.tooltip_adv:
        cfg.pages.graph_width = round(d.tooltip_width * geo.tooltip_adv)


def machines_path_for(config_path: Path) -> Path:
    """machines.toml next to a given config.toml (an explicit --config keeps its
    own machines sibling, so a self-contained config set — and the tests — stay
    isolated from the user's real hardware)."""
    return config_path.parent / "machines.toml"


def machine_source_paths(config_path: Path | None) -> list[Path]:
    """The machines.toml files that feed load_config(config_path), in load order
    — what the daemon watches for hot-reload. Mirrors _load_machines' choice: the
    default resolution reads the shipped base + the user's XDG override; an
    explicit --config reads only its own sibling."""
    if config_path is None:
        return [SHIPPED_MACHINES, user_machines_path()]
    return [machines_path_for(config_path)]


def _load_machines(config_path: Path | None) -> dict:
    """Machine blocks (one top-level table per machine) merged low→high priority
    from machine_source_paths(config_path) — the shipped base + the user's XDG
    override for the default resolution, or just the sibling of an explicit
    --config. {} if none exist."""
    machines: dict = {}
    for p in machine_source_paths(config_path):
        if not p.exists():
            continue
        try:
            with p.open("rb") as f:
                machines = _deep_merge(machines, tomllib.load(f))
        except (OSError, tomllib.TOMLDecodeError):
            pass
    return machines


def load_config(
    path: Path | None = None,
    *,
    vertical: bool | None = None,
) -> Config:
    """Orientation is auto-detected from the Plasma panel edge; `vertical`, when
    given, forces it instead (used by `render --layout` and by tests) — one
    resolved bool that both picks the [panel_horizontal]/[panel_vertical]
    override and lands on Config.vertical for the formatter's root class, so the
    two never disagree."""
    # Machine files feed off the ORIGINAL path arg: None → the default resolution
    # (shipped base + the user's XDG machines); an explicit --config → only its own
    # sibling, keeping it (and the tests) self-contained. The machine whose
    # [<name>.detect] matches this host merges over the config below — no selector,
    # the hardware picks it.
    machines = _load_machines(path)
    if path is None:
        path = default_config_path()
    if not path.exists():
        return Config(machine=detect_machine(machines) or "")

    with path.open("rb") as f:
        raw = tomllib.load(f)

    machine = detect_machine(machines)
    if machine:
        raw = _deep_merge(raw, machines[machine])

    # Glyphs and labels are external files like style-dark.css, resolved against
    # the asset roots (not the config path): style/icons.toml (theme, XDG-
    # overridable) and lang/<language>.toml (i18n, shipped).
    language  = raw.get("display", {}).get("language", "en")
    icons  = _load_toml_at(resolve_style("icons.toml"))
    labels = _load_toml_at(CODE_ROOT / "lang" / f"{language}.toml")

    geo = detect_panel_geometry()
    is_vertical = vertical if vertical is not None else geo.vertical
    # Orientation override: [panel_vertical]/[panel_horizontal] is merged onto
    # [panel] (same _deep_merge + items_add/items_remove grammar as a machine block)
    # by the resolved orientation, applied after the machine = most specific.
    raw_panel = raw.get("panel", {})
    override = raw.get("panel_vertical" if is_vertical else "panel_horizontal")
    if isinstance(override, dict):
        raw_panel = _deep_merge(raw_panel, override)

    cfg = Config(
        display        = _build_section(DisplayConfig,        raw, "display"),
        vertical       = is_vertical,
        bar_panel      = _build_section(BarConfig,            raw, "bar_panel"),
        column_panel   = _build_section(ColumnConfig,         raw, "column_panel"),
        bar_tooltip    = _build_section(BarConfig,            raw, "bar_tooltip"),
        spark_panel  = _build_section(SparkConfig,    raw, "spark_panel"),
        spark_tooltip = _build_section(SparkConfig,   raw, "spark_tooltip"),
        braille_panel    = _build_section(BrailleConfig,      raw, "braille_panel"),
        braille_tooltip  = _build_section(BrailleConfig,      raw, "braille_tooltip"),
        panel          = _parse_surface(raw_panel),
        tooltip        = _parse_surface(raw.get("tooltip", {})),
        pages          = _build_section(PagesConfig,          raw, "pages"),
        thresholds     = _build_section(ThresholdConfig,      raw, "thresholds"),
        notify_thresholds = _build_section(NotifyThresholds,  raw, "notify_thresholds"),
        notifications  = _build_section(NotificationConfig,   raw, "notifications"),
        icons          = icons,
        labels         = labels,
        sensors        = _build_section(SensorOverrides,      raw, "sensors"),
        disks          = _build_section(DiskConfig,           raw, "disks"),
        battery        = _build_section(BatteryConfig,        raw, "battery"),
        system_updates = _build_section(SystemUpdatesConfig,  raw, "system_updates"),
        server_check   = _build_section(ServerCheckConfig,    raw, "server_check"),
        machine        = machine or "",
    )
    _auto_fit_panel(cfg, geo)
    # graph_width (the graphs PNG width) tracks the resolved tooltip_width, so it's
    # derived by apply_canonical_width once the canonical is known — not here, where
    # tooltip_width is still the bare floor. Its default stands until then.
    _drop_unknown_items(cfg)
    _drop_misplaced_items(cfg)
    return cfg


def _drop_items(surface: Surface, bad: set[str], what: str) -> None:
    """Remove `bad` from every section of `surface` and say so once on stderr:
    the shared tail of the two guardrails below. An emptied section is left in
    place — the render collapses empty ones on its own."""
    if not bad:
        return
    for sec in surface.sections:
        sec.items = [it for it in sec.items if it not in bad]
    import sys
    print(f"[config] {what}, dropped: {', '.join(sorted(bad))}", file=sys.stderr, flush=True)


def _drop_unknown_items(cfg: Config) -> None:
    """Drop items listed in sections but not recognized (a typo in the toml):
    the item registry is the canonical set of names. Separators are section
    entries rather than items, so unknown_item_names spares them. Local import
    so the config schema isn't coupled to the registry at module level."""
    from registry import unknown_item_names
    for surface, where in ((cfg.panel, "panel"), (cfg.tooltip, "tooltip")):
        _drop_items(surface, unknown_item_names(surface.item_set()), f"unknown items in the {where}")


def _drop_misplaced_items(cfg: Config) -> None:
    """Drop items placed on a surface that doesn't admit them: complex forms
    (combo, two-pair, strings) in a [panel.*], or panel-only ones (bar/spark/
    braille — a bare trace with no label) in a [tooltip.*]. The rule lives in
    the metrics (actual surfaces = form ∩ metric, metrics.item_surfaces); this
    enforces it, so a surface renders only what it admits instead of the
    registry happily rendering a misplaced token anyway. Runs before the
    canonical width is derived, so a dropped item never widens the tooltip.
    Local import, as above."""
    from registry import misplaced_items
    bad_panel, bad_tooltip = misplaced_items(cfg.panel.item_set(), cfg.tooltip.item_set())
    _drop_items(cfg.panel, bad_panel, "tooltip-only items placed in the panel")
    _drop_items(cfg.tooltip, bad_tooltip, "panel-only items placed in the tooltip")
