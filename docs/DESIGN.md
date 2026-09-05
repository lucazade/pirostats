# PiroStats — Python Design

## Problem and goal

Rendering panel stats by re-spawning a script every 1.5 s is expensive on a
laptop: process startup, config sourcing, hardware autodetection, and a fork per
sensor (awk, upower, stat, df) add up to ~2 W of extra draw. PiroStats is built
the other way around — a Python daemon always in memory that renders in-process,
so the plasmoid only ever runs `cat`. **Zero forks in the hot path** is the whole
point.

---

## Architecture

```
pirostats/ (src/)      ← Python package
  config.py              ← loads config.toml (+ style/icons.toml, lang/*.toml) → Config dataclass
  sensors.py              ← reads the required sensors → Readings dataclass
  forms.py               ← closed vocabulary of FORMS (how it renders) + surfaces
  metrics.py             ← the METRICS (what: data, hw gate, needs, admitted forms)
  registry.py            ← dispatch (metric × form) → rows; "metric[:form]" token layer
  items.py               ← cell-factories: the row building blocks (row/per/label/value/…)
  traces.py              ← bar/column/spark/braille forms: % encodings → HTML + standalone/combo rows
  render_model.py        ← Cell/Row/Block model + Ident (item-<metric> form-<form>) + threshold→CSS class
  mono_render.py         ← serializes blocks into table-free monospace HTML
  formatter.py           ← orchestrates: token → rows (via registry.render) → string;
                           also renders the whole-page tooltip views (top_process, cpu_cores, graphs)
  chart.py               ← pure-stdlib PNG area charts (zlib) for the graphs page
  pages.py               ← tooltip deep-dive pages: registry, command runners, colorizers, pager
  notifier.py            ← sends D-Bus notifications when thresholds are exceeded
  daemon.py              ← main loop, writes <runtime>/{panel,tooltip}.html
  runtime.py             ← where those files live: the per-user runtime tree ($XDG_RUNTIME_DIR/pirostats)

pirostats              ← entry point: starts daemon.main() (subcommands)
```

The plasmoid (`<runtime>` = `$XDG_RUNTIME_DIR/pirostats`, resolved on both sides
independently — the daemon via `runtime.py`, the applet via QStandardPaths — so the
path is written down in no config file; see runtime.py for the directory layout):
- **Panel**: `cat <runtime>/panel.html`
- **Tooltip**: `cat <runtime>/tooltip.html`
- **Mouse wheel**: `pirostats page prev|next` — steps the tooltip page counter
- **Click**: `pirostats click` — the page's click action (uniform today)
- **Geometry**: writes `<runtime>/state/geom` (`<usable-width-px> <glyph-advance-px>
  <vertical 0|1>`, via `output.width` + `TextMetrics`) — the only plasmoid→daemon
  flow, feeding the panel's auto-fit (see §1 and config._auto_fit_panel). The
  runtime tree is tmpfs, cleared at logout, so the daemon mirrors the last valid geometry to
  `~/.cache/pirostats/geom` and seeds the boot paint from it — the first frame
  is already width-fitted instead of rendering the unfitted defaults and
  reflowing once the plasmoid republishes.

No Python invocation in the hot path. The applet has no clock of its own: it watches
the runtime directory (inotify, via a FolderListModel) and `cat`s only when the daemon
actually writes, so the one rate in the system is `display.poll_interval` and a frame
never ages on disk waiting for a tick to notice it.

### Tooltip pages

The tooltip is paged: index 0 is the full stats view, followed by the deep-dive
pages configured in `pages.order` (see the README). A tiny counter file
(`<runtime>/state/page`) is written by the `page` command and read by the loop
each poll; `<runtime>/state/npages` (published by the daemon) lets `page` wrap
without parsing the config. The active list is `pages.build_pages()` — the full
view plus the configured, known ids. Rendering splits three ways: the full view
via `formatter.format_tooltip`, the formatter-rendered pages (top_process,
cpu_cores, graphs) via dedicated `formatter.format_*` methods (they need
thresholds / braille / readings), and the plain command pages (connections,
fastfetch) via `pages.page_inner`, which runs the command and optionally
colorizes it (`pages._colorize_listening`). The pager is centered on the body's
monospace width via `&nbsp;` padding rather than `align="center"`, so it doesn't
drift while Plasma lazily resizes the popup.

The **graphs page** is the exception to "all visuals are text": it stacks
plasma-systemmonitor-style history charts — CPU, memory, the active GPU (usage
as the filled area, decoder as an overlaid line where the vendor reports one;
discrete preferred over integrated — Nvidia, then AMD, then Intel),
and network (download area + upload line, auto-scaled to the window peak, no
percent labels) — each a PNG rasterized by `chart.py` (grid + y-axis labels +
filled area + antialiased line, pure `zlib`+`struct`, no image lib) and
embedded as a `data:` URI. Qt RichText accepts a raster `<img>` this way (SVG
crashes plasmashell). Only the plot is in the image; the labels and current
values stay HTML, threshold-colored like the stat cells. Chart colors are baked
into the pixels (they can't read the CSS), so `chart.py` carries a small
theme-agnostic palette. The history uses the shared cpu/mem/GPU/network buffers
extended to `pages.graph_history_length` while the page is enabled (GPU and
network sampled by `sensors._sample_gpu_history`/`_sample_net_history`, their
caps requested in `registry.needed_capabilities`). It's drawn only while the
page is shown, like the command pages.

---

## 1. Config — TOML

TOML because: native types (bool, int, float, lists), clear sections, no
dependency (stdlib `tomllib` from Python 3.11, Arch ships 3.11+).

```toml
# [display] holds only what is global: the two cadences and the inspection
# overlay. Anything scoped to a surface, a page or a form lives in its own
# section, so the file descends by scope: display → panel → tooltip → pages →
# forms → thresholds and the rest.
[display]
poll_interval    = 1.5     # seconds between polls (rewrites both HTML files)
history_interval = 1.5     # history buffer cadence, independent of poll: read by
                           # spark, braille AND the graphs page, so it belongs to
                           # none of them and stays global
# panel orientation: no knob, auto-detected from the Plasma panel's edge

# VERTICAL panel visuals (bar, spark, braille) auto-sized to the real width:
# the plasmoid publishes usable width + glyph advance to <runtime>/state/geom,
# the daemon computes everything in config._auto_fit_panel
# (cols = width/advance → panel_min_width, bar width, panel_font_size,
# spark/braille lengths). The tooltip width is auto-derived too — the main
# page's canonical (max) width, config.apply_canonical_width. Only the bar's
# strip thickness (bar_panel.height) and the tooltip spark/braille lengths
# remain in the TOML; the outside-Plasma fallbacks are defaults in config.py.
[spark_tooltip]   # + [braille_tooltip] (2 samples/character)
cpu_spark_length = 10
mem_spark_length = 10
[bar_panel]           # + [bar_tooltip]
height     = 3        # strip thickness in px (0 = inherit); width AUTO in vertical
                      # the █/░ glyphs are the form itself, fixed in traces.py

# ── Sections: each surface is an ordered list of typed sections; each
# section has a title (rendered only in the tooltip) and an ordered item list.
# Listing an item = enabling it; list order = render order. An item shows up
# only if listed AND its hardware is present (gate in the code,
# formatter._available) — so lists can be generous and every machine shows
# only what it has; a section with no visible item collapses entirely.
# Machine blocks override per section key (items / items_add / items_remove,
# and order_add). `pirostats list-items` lists the available names.
#
# A scalar beside `order` is an OPTION of that surface, not a section: it rides
# the machine/orientation overrides exactly like the item lists, and the `order`
# walk in config._parse_surface steps over it. `glyphs` is the one such option
# today — panel-only, since a panel label IS the glyph (dropping it drops the
# cell) while a tooltip label is glyph+word.
[panel]
order  = ["cpumem", "thermal", "batteries"]
glyphs = true
[panel.cpumem]
items = ["cpu_usage:spark", "cpu_usage", "mem_usage"]
[panel.thermal]
items = ["cpu_temp"]

[tooltip]
order = ["cpumem", "drives", "gpu", "thermal", "batteries", "io", "load"]
[tooltip.cpumem]
title = "━━━ Cpu & Mem ━━━"
items = ["cpu_usage:spark_value", "mem_usage:spark_value", "cpu_freq"]
[tooltip.drives]
title = "━━━ Drives ━━━"
items = ["disk_usage", "disk_smart:pair"]
# ...other sections: gpu, thermal, batteries, io, load

# The tooltip's deep-dive pages: `order` is what the wheel cycles through after
# page 0 (the full view, never listed). [] = no pager. Its own knobs live here
# rather than in [display] — graph_history_length is the graphs page's alone.
[pages]
order                = ["graphs", "processes", "cpu_cores", "connections", "fastfetch"]
graph_history_length = 60

[thresholds]
# 3 colors: [mid, high] (below mid = low). Some items are binary (v > threshold)
cpu_usage            = [50, 70]
cpu_temp             = [50, 70]
hd_temp              = [50, 55]
battery_sys          = [20, 80]
gpu_nvidia_dec_usage = 1            # binary: > 1 = active, otherwise no color
load_avg_1           = [0.7, 1.0]   # fraction of cores (value / n_cores), not absolute

[notify_thresholds]      # thresholds that trigger the desktop notification
cpu_temp        = 80
battery_sys     = 10
load_avg_15     = 0.9    # fraction of cores sustained for load_avg_minutes minutes
load_avg_minutes = 10

[notifications]          # which notifications are active
cpu_temp     = true
battery_sys  = true
server_check = false

# Glyphs and labels are NOT in config.toml: they live in external files keyed
# by metric, like style-dark.css. Glyphs (theme) in style/icons.toml, labels
# (i18n, chosen via display.language) in lang/<language>.toml with the delimiter:
#   style/icons.toml   →  cpu_usage = ""
#   lang/en.toml       →  delimiter = ":" ; cpu_usage = "Cpu usage"

[disks]
# "auto" discovers the real mounts under auto_roots (+ "/" always); or an
# explicit list, e.g. mounts = ["/", "/mnt/data"].
mounts             = "auto"
auto_roots         = ["/mnt", "/media", "/run/media"]
smart_interval     = 3600   # SMART TTL for SSD/NVMe (s)
smart_interval_hdd = 21600  # spinning HDDs (slower to query)

[system_updates]
file = ""   # count written by an external checker; empty = disabled

[server_check]
file = ""   # "1"/"0" written by an external checker; empty = disabled
```

> The NVIDIA GPU has no `[gpu]` section/autodetect: it's only enabled explicitly
> in a machine block (`machines.toml`). Visual style (colors, font, spacing)
> is in `style-dark.css`, not here.

---

## 2. Sensors — Readings dataclass

**What "sensor abstraction" means here**: instead of scattered global variables
or methods mixed with output logic, all sensor data lives in a single immutable
object produced on every poll.

```python
@dataclass
class Readings:
    # CPU (history = spark/braille buffer, sampled at history_interval)
    cpu_usage:    Optional[int]   = None
    cpu_temp:     Optional[int]   = None
    cpu_freq:     Optional[float] = None          # MHz, raw
    cpu_turbo:    Optional[bool]  = None
    cpu_history:  list[int]       = field(default_factory=list)
    mem_history:  list[int]       = field(default_factory=list)
    uptime:       Optional[int]   = None          # seconds
    load_avg:     Optional[tuple[float, float, float]] = None
    top_process:  Optional[list[tuple[str, int]]] = None

    # Memory
    mem_usage:    Optional[int]   = None
    swap_usage:   Optional[int]   = None

    # Network (device/ip/wifi detected live → handles interface switching)
    net_up_bps:   Optional[int]   = None
    net_down_bps: Optional[int]   = None
    net_device:   Optional[str]   = None
    ip_address:   Optional[str]   = None
    wifi_ssid:    Optional[str]   = None
    wifi_signal:  Optional[int]   = None          # %, converted from dBm

    # Disk / I/O (disk_smart is per physical disk, paired with hd_temp)
    disk_read_bps:  Optional[int] = None
    disk_write_bps: Optional[int] = None
    disk_usage:   dict[str, Optional[DiskUsage]]  = field(default_factory=dict)  # mount → usage
    disk_smart:   dict[str, Optional[bool]]       = field(default_factory=dict)  # label → healthy?
    hd_temps:     dict[str, Optional[int]]        = field(default_factory=dict)  # label → °C
    fan_speeds:   dict[str, Optional[int]]        = field(default_factory=dict)  # idx → RPM

    # Batteries
    battery_sys:  list[BatterySys]         = field(default_factory=list)
    battery_mouse: Optional[BatteryPeriph] = None
    battery_kbd:   Optional[BatteryPeriph] = None

    # NVIDIA GPU + Intel iGPU
    gpu_temp:     Optional[int]   = None
    gpu_usage:    Optional[int]   = None
    gpu_mem:      Optional[int]   = None
    gpu_dec:      Optional[int]   = None
    gpu_fan:      Optional[int]   = None
    gpu_intel_freq:      Optional[int] = None
    gpu_intel_usage:     Optional[int] = None
    gpu_intel_dec_usage: Optional[int] = None

    # Other
    screen_brightness: Optional[int] = None
    system_updates:    Optional[int] = None
    server_ok:         Optional[bool] = None
```

`sensors.py` exposes `collect` (plus `discover_hardware`/`rescan_peripherals`):

```python
def collect(state: DaemonState, hw: HardwareInfo, cfg: Config,
            timings: dict[str, float] | None = None, skip_slow: bool = False) -> Readings:
    """Fresh snapshot. `state` holds the counters for diff-based sensors (cpu,
    network, disk I/O) and updates them in place; `skip_slow=True` skips the
    sensors whose first cold access blocks (SMART, Bolt HID, nvidia-smi, /proc
    scan, iGPU fdinfo) — used for the immediate first paint at startup.
    Collection is demand-driven: a sensor is read only if a requested
    capability needs it (cpu/mem always)."""
```

`HardwareInfo` is discovered at startup (hwmon paths, UPower devices, network
interface, GPU, presence flags) and doesn't change between polls — the daemon
computes it once (see §7).

---

## 3. Formatter

`PanelFormatter(cfg, hw)` wraps config and hardware; two methods sharing the
same data source produce the HTML (color is a CSS class name, defined only in
`style-dark.css` — this never decides what a color looks like):

```python
class PanelFormatter:
    def format_panel(self, r: Readings, css: str = "", timings=None) -> str:
        """Panel's compact strip (glyph + value, one physical row either
        horizontal or laid out vertically)."""

    def format_tooltip(self, r: Readings, css: str = "", timings=None) -> str:
        """Tooltip: glyph+word labels, sections with a title, colors via CSS class."""
```

Each responsibility has its own module: `sensors` reads, `registry`/`formatter`/`items`
format, `notifier` notifies — no monolithic function.

**Dispatch (metric × form).** There's no `if name == ...` chain: the config
names an item as a `metric[:form]` token (e.g. `cpu_usage:bar`), and the
dispatch table in `registry.py` is keyed by `(metric, form)`. Regular items are
*data* — a list of cells composed with `row(...)`/`per(...)` and reusable
building blocks from `items.py` (`label`/`value`/`spark`/…); irregular ones
(string-join combos, bar+spark, batteries, top_process) are explicit exception
functions in the same table. `render()` computes the `Ident` (metric + form,
with BAR resolved for orientation) and threads it to the cells, which write the
final two-axis class `.item-<metric>.form-<form>`. `_render_item`/`_available`
are thin delegates; the known metrics are the canonical set (`load_config`
warns about unrecognized tokens). Adding a regular item = one row in the dispatch.

Among the irregular ones, the cases that share the *same* layout (not just
different data) are centralized in a single helper instead of being copied: the
two-per-row grid of the `pair` form (`_disk_smart_pair`, `_hd_temp_pair`,
`_fan_speed_pair`) lives in `_pair_grid`, and the two side-by-side bytes/s
metrics of `net_speed`/`disk_io` in `_dual_rate_rows`; the corresponding
exception functions pass only the differences (source, label format, value
cell). The boundary is deliberate: *structural* duplication (identical layout
logic repeated) is centralized, not legitimate variety. The bar/column/spark/
braille forms — the "own-skeleton" percentage encodings — live together in
`src/traces.py` as free `(f, …)` functions (the shape the registry uses),
collapsed onto one combined-row skeleton and one standalone builder; the simple
label+value rows stay as distinct methods because their logic is genuinely
different. The complete layout-plan schema is in `docs/LAYOUT.md`.

**DERIVED placement.** Where an item can go isn't written by hand: it's *derived*
as the intersection of its FORM's surfaces and its METRIC's
(`metrics.item_surfaces`). A complex form (combo, two-pair) lives
only in the tooltip because its form isn't admitted in the panel; a
tooltip-only metric (uptime, load_avg) stays out of the panel because of the
metric; conversely a bare form (bar/spark/braille — a trace with no label at
all) stays out of the tooltip because of the form. `load_config` enforces this
(`_drop_misplaced_items`: a token on a surface its actual surfaces don't admit
is dropped from the section, with a stderr warning), and `pirostats list-items`
lists every item with its placement.

**Demand-driven collection.** Every `Metric` (metrics.py) declares its `needs`
(sensor capabilities); `collect()` reads a sensor only if a capability is
required by a configured item or an enabled notification
(`registry.needed_capabilities`). `cpu_usage`/`mem_usage` are always read
(history sparks + baseline).

---

## 4. Notifier

```python
def check_and_notify(r: Readings, cfg: Config, state: NotifState, hw: HardwareInfo) -> NotifState:
    """
    Compares Readings against the thresholds in cfg.notify_thresholds.
    Sends a D-Bus notification only on the first crossing (edge, not level).
    Returns the new state for the next poll.
    """
```

**Implementation**: `gi.repository.Notify` (GLib, part of `python-gobject`,
already present on any KDE install). No subprocess, no fork, no extra
dependency.

```python
from gi.repository import Notify

Notify.init("pirostats")

def _send(title: str, body: str, icon: str = "dialog-error") -> None:
    n = Notify.Notification.new(title, body, icon)
    n.set_urgency(Notify.Urgency.CRITICAL)
    n.set_timeout(Notify.EXPIRES_NEVER)
    n.show()
```

The daemon runs in the user session with `graphical-session.target` → D-Bus
available, no `DISPLAY` issues.

---

## 5. Daemon loop

`main()` only does argparse with subcommands (`daemon`, `render`, `probe`,
`profiling`, `list-items`) and dispatches them; the real loop lives in `run_daemon`:

```python
def run_daemon(cfg_path):
    cfg   = load_config(cfg_path)             # overlay is read from the config
    hw    = discover_hardware(cfg)            # one-time
    fmt   = PanelFormatter(cfg, hw)
    state = DaemonState()                     # diff counters + history buffer
    notif = NotifState()

    # Non-blocking first paint: skip_slow skips the slow cold sensors,
    # the panel is populated in ~70ms instead of staying blank for ~1-2s.
    r = collect(state, hw, cfg, skip_slow=True)
    _write_atomic(OUT_FILE,     fmt.format_panel(r, css=css))
    _write_atomic(TOOLTIP_FILE, fmt.format_tooltip(r, css=css))

    while True:
        start = time.monotonic()
        # Hot-reload: if config.toml/machines.toml changed, reload
        # (try/except — a malformed TOML doesn't kill the daemon, it keeps the
        # last valid config); the history (cpu+mem) is sampled at
        # history_interval, independent of poll_interval.
        cfg, fmt = _maybe_reload(...)
        r = collect(state, hw, cfg)
        notif = check_and_notify(r, cfg, notif, hw)
        _write_atomic(OUT_FILE,     fmt.format_panel(r, css=css))
        _write_atomic(TOOLTIP_FILE, fmt.format_tooltip(r, css=css))
        time.sleep(max(0, cfg.display.poll_interval - (time.monotonic() - start)))
```

`_write_atomic`: writes to `.tmp` then `os.replace()` — atomic, no race
condition with the plasmoid's `cat`. `PANEL_FILE`/`TOOLTIP_FILE` (runtime.py) are
`<runtime>/{panel,tooltip}.html`. The rename is also what the applet's watcher sees;
renaming over the target swaps the inode, which is why the applet watches the
directory rather than the two files.

The loop also watches, by `mtime`, three files outside the repo, each
triggering a targeted reload: `plasma-org.kde.plasma.desktop-appletsrc` and
`<runtime>/state/geom` (panel orientation / vertical auto-fit), and
`~/.config/kdeglobals` (the desktop color scheme).

**Light/dark stylesheet.** There is no boolean "is dark" flag in Plasma, and
the scheme name (`BreezeDark`) is unreliable for custom schemes, so
`_plasma_is_light` judges by the perceived luminance of the window
`BackgroundNormal` — the same signal KDE's own `KColorScheme` uses. The color is
read with `kreadconfig6` (the official reader, honoring the config cascade),
falling back to parsing `kdeglobals` directly. On a light scheme the daemon
serves `style-light.css` instead of `style-dark.css`; watching `kdeglobals`'
`mtime` makes a Global Theme switch hot-reload the matching sheet with no
restart. `style-light.css` mirrors `style-dark.css`'s selectors 1:1 and differs
only in colors. Plain values (`.val` with no threshold state) are left uncolored
in both, so they inherit the widget's own themed text color (light on dark
desktops, dark on light ones) — nothing to switch there.

---

## 6. Systemd user service

```ini
# service/pirostats.service → ~/.config/systemd/user/pirostats.service
[Unit]
Description=PiroStats stats daemon
Documentation=https://github.com/lucazade/pirostats
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/pirostats daemon
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
```

Start: `systemctl --user enable --now pirostats`

---

## 7. Hardware discovery (one-time)

Discovery is split into two levels: static fields (never change at runtime) and
peripheral ones (retried if `None`), in the same `HardwareInfo`:

```python
@dataclass
class HardwareInfo:
    # ── Static (discovered once at startup) ──
    cpu_temp_path:  Optional[Path]
    cpu_freq_path:  Optional[Path]       # cpu0 scaling_cur_freq (avoids psutil per-core)
    hd_temp_paths:  dict[str, Path]      # disk label → /sys path
    fan_paths:      dict[str, Path]      # "1".."4" → /sys path
    battery_sys_ids: list[str]           # UPower BAT* paths
    has_nvidia:     bool
    intel_gpu_freq_path: Optional[Path]
    intel_gpu_pci:  Optional[str]
    net_device:     Optional[str]        # e.g. "wlan0"
    disk_io_device: Optional[str]        # device backing the "/" mount, e.g. "nvme0n1p2"
    cpu_count:      int                  # normalizes load_avg thresholds (per-core)
    # Presence flags for the formatter's hardware gate (formatter._available):
    cpu_turbo_supported: bool            # the turbo/boost knob exists in sysfs
    has_backlight:  bool
    has_wifi:       bool
    # ── Dynamic (retried every 60s if None) ──
    battery_mouse_id: Optional[str] = None
    battery_kbd_id:   Optional[str] = None
    disk_smart_drives: dict[str, tuple[str, str, bool]] = field(default_factory=dict)
    periph_scan_ts:   float = float("-inf")   # -inf, not 0.0 (see TTL note below)
```

**Static**: hwmon paths, network interface, GPU presence, system batteries,
presence flags — never change at runtime. **Peripheral**: hidpp batteries
(mouse, keyboard) may be missing at startup (PC turned on before the mouse is
connected), so they're retried. UPower/UDisks2 reads and discovery happen via
**GDBus/Gio** (`gi.repository.Gio`), not `upower`/`busctl` subprocesses:

```python
PERIPH_RESCAN_INTERVAL = 60.0  # seconds

if needs_periph_rescan(hw, cfg):
    if time.monotonic() - hw.periph_scan_ts >= PERIPH_RESCAN_INTERVAL:
        hw = rescan_peripherals(hw, cfg)   # one UPower enumerate via Gio
```

Result: at most one scan per minute when the mouse is absent, zero once
everything is found. The user plugs in the mouse 30s after boot: it shows up
in the bar within a minute. (`periph_scan_ts` starts at `-inf`, not `0.0`:
`time.monotonic()` counts from boot, so `0.0` would delay the first scan until
uptime ≥ TTL instead of triggering it immediately.)

---

## 8. One-shot subcommands

Subcommands that don't require the daemon to be running: they perform a single
poll (or not even that) and then exit.
- `probe`: probes the hardware and prints every raw reading, **no render**.
- `render`: renders panel/tooltip. `--component panel|tooltip|both` (default both), `--format text|html` (default text → stdout; html → `/tmp/pirostats_render_{panel,tooltip}.html`). Production CSS (the inspection overlay is a live-widget aid, not a render flag).
- `profiling`: timing report (cold/warm cache, per-section/per-item).
- `list-items`: lists the available items with their placement (panel/tooltip).

There is no `--overlay` flag: the inspection overlay (`style-overlay.css`) is a config key — `overlay = true` under `[display]` in `config.toml`, hot-reloaded like the rest of the file and watched on the live panel/tooltip.

`probe` discovers the hardware, runs the warm-up + one poll, and prints
hardware and readings to stdout in a readable format.

Expected output:
```
── Hardware discovery ──────────────────────────────────────
net_device:     wlan0
cpu_temp_path:  /sys/class/hwmon/hwmon2/temp1_input
hd_temp_paths:  nvme0 → /sys/class/hwmon/hwmon5/temp1_input
battery_sys:    ['/org/freedesktop/UPower/devices/battery_BAT0']
battery_mouse:  None  (not found)
has_nvidia:     False

── Readings (every item, including disabled ones) ──────────
cpu_usage:    42 %
cpu_temp:     61 °C
mem_usage:    58 %
...

── Panel output ─────────────────────────────────────────────
<formatted panel output>

── Tooltip output ───────────────────────────────────────────
<formatted tooltip output>
```

---

## 9. Output fidelity

Renders are pinned so refactors can't change them silently. The suite in `tests/`
(`test_formatter`, `test_mono_render`, `test_render_model`, `test_items`, ...) and
the golden snapshots (`tests/golden/`) assert the generated HTML on edge cases, so
rewrites like the `_pair_grid`/`_dual_rate_rows` helpers stay **byte-for-byte**
identical.

Output traits: Nerd Font glyphs, color as a **CSS class** (never ANSI codes;
the style lives in `style-dark.css`), colored block sparks `▁▂▃▄▅▆▇█` and braille,
bars (`█` filled, configurable `░` empty), sectioned tooltip with title and
color, column alignment via `&nbsp;` padding (see `docs/LAYOUT.md`).

---

## 10. Design principles

- **State in memory, not on disk.** Discovery runs once at startup; sensor state
  and TTL caches live in the process, keyed by `time.monotonic()` — no cache
  files, no `stat` fork just to check freshness.
- **Absent means hidden.** A sensor that reads `None` simply isn't rendered — no
  cooldown files or redetect flags.
- **One immutable `Readings` per poll**, not scattered globals (see §2).
- **Separated responsibilities**: collect / format / notify are distinct steps,
  not one do-everything render function.
- **No forks in the loop.** Sensors read `psutil`, `/proc` and `/sys` directly;
  UPower/UDisks2 go over GDBus/Gio (one-time discovery + a TTL cache), never a
  subprocess per poll.

---

## Dependencies

| Package | Use | Arch package |
|---|---|---|
| `psutil` | CPU, memory, disk, network | `python-psutil` |
| `python-gobject` | Notifications (`gi.repository.Notify`) **and** UPower/UDisks2 via GDBus (`gi.repository.Gio`) | `python-gobject` |
| stdlib `tomllib` | Config TOML parsing | — (Python 3.11+) |
| `python-nvidia-ml-py` | NVIDIA GPU via NVML/pynvml (optional, only if there's an NVIDIA GPU) | `python-nvidia-ml-py` (extra repo) |

UPower/UDisks2 are queried via GDBus/Gio (inside `python-gobject`), **no**
`python-dbus`. The NVIDIA GPU uses pynvml/NVML as the primary path
(`python-nvidia-ml-py` from the *extra* repo, not AUR), with automatic
fallback to an `nvidia-smi` subprocess if the package is missing.

---

## File structure in the repo

```
src/                       ← Python package (config, sensors, formatter, notifier, daemon, ...)
config/
  config.toml              ← default data/behavior
  machines.toml            ← per-machine overrides (detection + tweaks)
style/
  style-dark.css           ← visual style (dark color schemes)
  style-light.css          ← visual style (light color schemes, auto-selected)
  style-overlay.css        ← inspection CSS overlay (theme-agnostic)
  icons.toml               ← Nerd Font glyph per metric (theme)
lang/
  en.toml                  ← label per metric + delimiter (i18n, display.language)
service/
  pirostats.service      ← systemd user unit
pirostats                ← executable entry point (repo root)
```
(current structure; see the README for details)
