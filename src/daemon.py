"""
Main daemon loop: polls sensors, formats output, writes atomic tmp files.
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import pages
from pagestate import (PAGE_FILE, NPAGES_FILE, read_page as _read_page,
                       set_page as _set_page, step_page)
from runtime import GEOM_FILE, PANEL_FILE, TOOLTIP_FILE, ensure_dirs
from config import (Config, apply_canonical_width, cache_live_geom, default_config_path,
                    load_config, machine_source_paths, resolve_style)
from formatter import PanelFormatter
from notifier import NotifState, check_and_notify
from sensors import (
    _gpu_cache_ttl, BAT_CACHE_TTL, DaemonState, FAN_CACHE_TTL, HardwareInfo, HD_TEMP_CACHE_TTL,
    NET_INFO_TTL, PERIPH_CACHE_TTL, Readings, collect,
    discover_hardware, needs_periph_rescan, read_top_process_page, rescan_peripherals,
)

# The runtime tree — PANEL_FILE / TOOLTIP_FILE (what the applet cats) and GEOM_FILE
# (what it publishes) — is runtime.py's; see there for why the two HTML files sit
# alone in a directory. Writing either one is what drives a repaint: the applet
# watches that directory, it doesn't poll.
# One-shot render files (inspection): outside the runtime dir, both because they are
# human-facing output a user opens by hand and because `pirostats render` must never
# touch the files a running daemon is writing — nor wake its watcher.
RENDER_PANEL_FILE   = Path("/tmp/pirostats_render_panel.html")
RENDER_TOOLTIP_FILE = Path("/tmp/pirostats_render_tooltip.html")
PLASMA_CFG   = Path.home() / ".config/plasma-org.kde.plasma.desktop-appletsrc"
# Color-scheme source: which stylesheet to serve (dark vs light) is read from
# here and watched by mtime, so switching the Global Theme hot-reloads the CSS.
KDEGLOBALS   = Path.home() / ".config/kdeglobals"
# GEOM_FILE carries the plasmoid's real text-area width and glyph advance; watched by
# mtime so the vertical panel re-fits when the panel is resized or the widget font
# changes (both move those numbers).
# PAGE_FILE / NPAGES_FILE (the tooltip page counter and its wrap bound) and the
# read/set/step helpers live in the stdlib-only `pagestate` module, so the wheel
# command stays cheap and its read-modify-write is flock-serialized. Imported
# above as PAGE_FILE, NPAGES_FILE, _read_page, _set_page, step_page.
_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_BLOCK_RE = re.compile(r"<style>.*?</style>", re.S)
_BR_RE = re.compile(r"<br\s*/?>", re.I)


def _css_path_for(light: bool = False) -> Path:
    """The stylesheet served to the widget: style-dark.css for dark desktops,
    style-light.css for light ones (chosen by _plasma_is_light). Resolved via
    config.resolve_style (XDG override, else the shipped style/) so it's correct
    regardless of where config.toml itself came from. The inspection overlay
    (style-overlay.css, shared by both) is _overlay_css_path's job."""
    name = "style-light.css" if light else "style-dark.css"
    return resolve_style(name)


def _parse_rgb(text: str) -> tuple[int, int, int] | None:
    """Parse an "r,g,b" triple (kdeglobals' color format) into ints, or None."""
    try:
        r, g, b = (int(x) for x in text.split(",")[:3])
    except (ValueError, TypeError):
        return None
    return r, g, b


def _window_bg() -> tuple[int, int, int] | None:
    """The desktop's window BackgroundNormal color. Read with kreadconfig6 —
    KDE's official config reader, which honors the whole config cascade (system
    defaults + /etc + user) — and, if that binary is absent, by parsing
    ~/.config/kdeglobals directly (the file we watch by mtime anyway)."""
    try:
        out = subprocess.run(
            ["kreadconfig6", "--file", "kdeglobals",
             "--group", "Colors:Window", "--key", "BackgroundNormal"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        if out:
            return _parse_rgb(out)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        in_window = False
        for line in KDEGLOBALS.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("["):
                in_window = s == "[Colors:Window]"
            elif in_window and s.startswith("BackgroundNormal="):
                return _parse_rgb(s.split("=", 1)[1])
    except OSError:
        pass
    return None


def _plasma_is_light() -> bool:
    """Whether Plasma's active color scheme is light. KDE exposes no boolean
    "is dark" flag, and the scheme NAME (e.g. "BreezeDark") is unreliable for
    custom schemes — so we judge by the perceived luminance of the window
    background, exactly how KDE's own frameworks (KColorScheme) decide
    dark-ness. Selects the served stylesheet (style-light.css vs style-dark.css) and
    is watched by KDEGLOBALS' mtime so a Global Theme switch hot-reloads the
    CSS. Defaults to dark (False) when the color can't be read, keeping the
    original look."""
    rgb = _window_bg()
    if rgb is None:
        return False
    r, g, b = rgb
    # Rec. 601 luma on 0..255; brighter than mid-grey reads as a light desktop.
    return (0.299 * r + 0.587 * g + 0.114 * b) > 127.5


_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def _read_css_file(css_path: Path) -> str:
    """Strip /* */ comments and collapse to a single line: Qt's RichText CSS
    parser breaks on both (the whole embedded <style> block silently fails
    to apply) — the plasmoid's own \\n -> <br> conversion (applied to ALL
    output, not just plain text) turns every newline in our CSS into a
    literal <br> tag sitting inside <style>, which is enough to corrupt the
    whole block. Comments/formatting stay readable in style-dark.css on
    disk, just not in the HTML actually fed to Qt."""
    try:
        text = css_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    text = _CSS_COMMENT_RE.sub("", text)
    return " ".join(text.split())


def _overlay_css_path(overlay: bool) -> Path | None:
    """style-overlay.css (the inspection overlay's per-cell backgrounds) when the
    overlay is on, else None. Resolved via resolve_style like the base sheet (XDG
    override, else shipped). Theme-agnostic on purpose: ONE file read on top of
    both bases (style-dark.css / style-light.css) — it only paints backgrounds, no
    text colour. Shared by _read_css and the daemon's mtime watch so both agree."""
    return resolve_style("style-overlay.css") if overlay else None


def _read_css(css_path: Path, overlay: bool = False) -> str:
    """Like _read_css_file, but also appends style-overlay.css when the inspection
    overlay is on and that file exists — the CSS half of the overlay (per-cell
    backgrounds)."""
    css = _read_css_file(css_path)
    overlay_path = _overlay_css_path(overlay)
    if overlay_path is not None:
        extra = _read_css_file(overlay_path)
        if extra:
            css += " " + extra
    return css


def _strip_html(html: str) -> str:
    """Plain-text rendering of the generated HTML, for terminal diagnostic output
    (no color: that's a CSS-only concept now, meaningless on a tty). mono_render
    emits one row per <div>, with &nbsp; padding doing the column alignment, so
    a newline at every </div> turns it back into one line per row and
    &nbsp; -> space keeps the columns lined up on a monospace terminal. (The
    horizontal panel is a single <span> line, so it just collapses to one
    line.) Deep-dive pages (processes/connections/…) break rows with <br>
    inside one <div>, so those become newlines too."""
    html = _STYLE_BLOCK_RE.sub("", html)
    html = html.replace("</div>", "</div>\n")
    html = _BR_RE.sub("\n", html)
    text = _TAG_RE.sub("", html)
    return text.replace("&nbsp;", " ")

PERIPH_RESCAN_INTERVAL = 60.0

# How long after daemon startup to watch for "first value" transitions on the
# TTL-cached sensors below (see BOOT_WATCH) and log them. Bounded and
# self-terminating: once every watched sensor has reported once, or this
# window has elapsed, the check is skipped for the rest of the process's
# life — zero steady-state cost. The log line in `journalctl --user -u
# pirostats.service -b` makes it easy to see how long each cached sensor
# took to report its first real value after boot/login, on this machine or
# any other hardware.
BOOT_WATCH_WINDOW = 90.0
BOOT_WATCH: list[tuple[str, Callable[[Readings], bool]]] = [
    ("battery_sys",    lambda r: bool(r.battery_sys)),
    ("battery_mouse",  lambda r: r.battery_mouse is not None),
    ("battery_kbd",    lambda r: r.battery_kbd is not None),
    ("hd_temps",       lambda r: any(v is not None for v in r.hd_temps.values())),
    ("fan_speeds",     lambda r: any(v is not None for v in r.fan_speeds.values())),
    ("gpu_nvidia",     lambda r: r.gpu_temp is not None),
    ("gpu_intel",      lambda r: r.gpu_intel_freq is not None),
    ("system_updates", lambda r: r.system_updates is not None),
    ("server_check",   lambda r: r.server_ok is not None),
    ("top_process",    lambda r: r.top_process is not None),
]


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _write_atomic(path: Path, content: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _render_page(fmt: PanelFormatter, r: Readings, css: str, active: list, idx: int) -> str:
    """Tooltip HTML for page `idx` of `active`, with the pager row. Index 0 is
    the full formatter view; the rest wrap a command's/table's output in the same
    shell. The pager is centered on the body's monospace width (page 0 gets that
    width from the formatter, the deep-dive pages from their text)."""
    n = len(active)
    if idx == 0:
        # Page 0 already opens with its own section headers — no page title there.
        return fmt.format_tooltip(r, css=css, pager_fn=lambda w: pages.pager_html(0, w, n))
    page = active[idx]
    title = pages.title_html(page)
    pager_fn = lambda w: pages.pager_html(idx, w, n)   # noqa: E731
    # top_process and cpu_cores are rendered by the formatter (colored / braille);
    # the rest are plain command-output text bodies.
    if page.render == "cpu_cores":
        return fmt.format_cpu_cores(r, css=css, header=title, pager_fn=pager_fn)
    if page.render == "top_process":
        return fmt.format_top_process(r, css=css, header=title, pager_fn=pager_fn)
    if page.render == "graphs":
        return fmt.format_graphs(r, css=css, header=title, pager_fn=pager_fn)
    return fmt.format_page(pages.page_inner(page, idx, n, fmt._cfg.display.tooltip_width),
                           css=css, header=title)


def _render_tooltip(fmt: PanelFormatter, r: Readings, css: str, active: list) -> str:
    """Tooltip HTML for the currently selected page. A deep-dive page's body is
    built only while it's selected (the loop reads the page each poll), so it
    costs nothing while on the full view."""
    return _render_page(fmt, r, css, active, _read_page() % len(active))


def _publish_pages(cfg: Config) -> list:
    """The active page list from config, publishing its length to NPAGES_FILE so
    the lightweight `page` command can wrap the counter without parsing config."""
    active = pages.build_pages(cfg.pages.order)
    _write_atomic(NPAGES_FILE, str(len(active)))
    return active


def _cleanup(signum, frame):
    try:
        PANEL_FILE.unlink(missing_ok=True)
        TOOLTIP_FILE.unlink(missing_ok=True)
        PAGE_FILE.unlink(missing_ok=True)
        NPAGES_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    sys.exit(0)


def _warmed_readings(cfg, hw) -> tuple[Readings, DaemonState]:
    """A "warm" poll good for a one-shot: diff-based sensors (cpu_usage,
    net_speed, top_process, gpu_intel_usage) need a reference sample before
    they report real values, so this does a warm-up collect, forces the TTL
    caches to expire, waits a moment, then resamples. Shared by `probe` (raw
    readings) and `render` (formatter). Returns the warmed state too, so a
    caller can drive the page-only diff sensors (read_top_process_page)."""
    dstate = DaemonState()
    collect(dstate, hw, cfg)
    dstate.top_process_cache_ts = 0.0       # force re-eval past its TTL on the next collect
    dstate.intel_gpu_usage_cache_ts = 0.0
    time.sleep(1.0)
    return collect(dstate, hw, cfg), dstate


def run_probe(cfg_path: Path | None) -> None:
    """One-shot hardware probe: hardware discovery + every raw reading, then exits.
    No panel/tooltip render — that's `render`'s job. The Readings section prints
    every field of the Readings dataclass, so it's independent of which items the
    active config enables."""
    import dataclasses

    cfg = load_config(cfg_path)
    hw  = discover_hardware(cfg)

    print("── Hardware discovery ──────────────────────────────────────")
    print(f"machine:         {cfg.machine or '(none)'}")
    print(f"net_device:      {hw.net_device or '(not found)'}")
    print(f"cpu_temp_path:   {hw.cpu_temp_path or '(not found)'}")
    for label, path in hw.hd_temp_paths.items():
        print(f"hd_temp [{label}]:  {path}")
    for idx, path in hw.fan_paths.items():
        print(f"fan [{idx}]:         {path}")
    for bat_id in hw.battery_sys_ids:
        print(f"battery_sys:     {bat_id}")
    print(f"battery_mouse:   {hw.battery_mouse_id or '(not found)'}")
    print(f"battery_kbd:     {hw.battery_kbd_id or '(not found)'}")
    print(f"intel_gpu:       {hw.intel_gpu_pci or '(not found)'}")
    print(f"has_nvidia:      {hw.has_nvidia}")
    print()

    r, _ = _warmed_readings(cfg, hw)

    print("── Readings ────────────────────────────────────────────────")
    for f in dataclasses.fields(r):
        print(f"  {f.name:<22} {getattr(r, f.name)!r}")
    print()


def _tooltip_html_for_render(fobj: PanelFormatter, r: Readings, css: str, page: str | None) -> str:
    """The tooltip markup run_render prints. `page` None → the full view; a
    deep-dive page id (any REGISTRY page, regardless of pages.order) → that
    page alone, wrapped in a two-page pager so the deep-dive dispatch and the
    pager row render exactly as in the daemon. The graphs page still emits its
    PNGs — legible in 'html' format, stripped to just the legends in 'text'."""
    if page is None or page == "full":
        return _render_page(fobj, r, css, [pages.FULL_PAGE], 0)
    active = [pages.FULL_PAGE, pages.REGISTRY[page]]
    return _render_page(fobj, r, css, active, 1)


def run_render(cfg_path: Path | None, component: str = "both",
               fmt: str = "text", vertical: bool | None = None,
               page: str | None = None) -> None:
    """One-shot render of panel and/or tooltip, then exits. `component` picks
    what (panel/tooltip/both), `fmt` how: 'text' = HTML stripped to stdout
    (quick terminal read), 'html' = real markup in /tmp/pirostats_render_*
    files (inspection in a viewer/qml). Production rendering (machine overrides,
    real colors) — the inspection overlay is a live-widget aid, watched via the
    config, not this one-shot.
    `vertical` (True/False) forces the panel orientation (column vs bar),
    ignoring auto-detection, so both layouts can be inspected on one machine;
    None = auto-detection, same as the daemon.
    `page` picks a tooltip deep-dive page to render (any REGISTRY id, even one
    not in pages.order) instead of the full view; None = full view."""
    cfg = load_config(cfg_path, vertical=vertical)
    hw  = discover_hardware(cfg)
    # A page's per-page sensors (cpu_cores, the graphs GPU/net histories) are
    # gated on the page being in pages.order, so enable the requested page
    # before warming or collect() skips its data. Harmless for a one-shot.
    if page and page not in ("full",) and page not in cfg.pages.order:
        cfg.pages.order = [*cfg.pages.order, page]
    r, state = _warmed_readings(cfg, hw)
    fobj = PanelFormatter(cfg, hw)
    apply_canonical_width(cfg, fobj.canonical_width(r))

    # The processes page's diff sensor runs off its own prev-state, only while
    # that page is shown — the daemon drives it, collect() doesn't. collect never
    # sampled it here (no top_process item requests the cap), so prime a
    # reference, wait, then resample; otherwise the page renders "no data yet".
    if page and pages.REGISTRY[page].render == "top_process":
        read_top_process_page(state)
        time.sleep(0.5)
        r.top_process_full = read_top_process_page(state) or r.top_process_full

    css = _read_css(_css_path_for(_plasma_is_light()), cfg.display.overlay)
    # A deep-dive page is a tooltip surface: --page implies tooltip-only,
    # overriding --component (there's no such page on the panel).
    want_panel   = page is None and component in ("panel", "both")
    want_tooltip = page is not None or component in ("tooltip", "both")

    if fmt == "html":
        # Extra styling only for these standalone inspection files: a Nerd Font
        # mono so the PUA glyphs (icons) don't turn to tofu, and a dark
        # background + white text fallback because there's no Plasma panel
        # underneath here and light text would vanish on a white background.
        # In production none of this applies: font and background come from
        # the widget's QML.
        css += (' body { background: #000; color: #fff; }'
                ' .panel, .tooltip { font-family: "NotoSansM Nerd Font Mono", monospace; }')
        print("── HTML written ──────────────────────────────────────────────")
        if want_panel:
            RENDER_PANEL_FILE.write_text(fobj.format_panel(r, css=css), encoding="utf-8")
            print(f"  panel:   {RENDER_PANEL_FILE}")
        if want_tooltip:
            RENDER_TOOLTIP_FILE.write_text(_tooltip_html_for_render(fobj, r, css, page), encoding="utf-8")
            print(f"  tooltip: {RENDER_TOOLTIP_FILE}")
        return

    if want_panel:
        print("── Panel output ────────────────────────────────────────────")
        print(_strip_html(fobj.format_panel(r, css=css)))
        if want_tooltip:
            print()
    if want_tooltip:
        label = page if page else "full"
        print(f"── Tooltip output ({label}) ──────────────────────────────────")
        print(_strip_html(_tooltip_html_for_render(fobj, r, css, page)))


TIMING_THRESHOLD_MS = 0.5   # items below this are folded into "other negligible"


def _print_timings(title: str, timings: dict[str, float], total_ms: float) -> None:
    items  = sorted(timings.items(), key=lambda kv: -kv[1])
    shown  = [(k, v) for k, v in items if v >= TIMING_THRESHOLD_MS]
    hidden = [(k, v) for k, v in items if v < TIMING_THRESHOLD_MS]

    print(f"  {title} — total {total_ms:.2f}ms")
    if not shown:
        print(f"    (no item above {TIMING_THRESHOLD_MS:.1f}ms)")
    for key, ms in shown:
        bar = "█" * min(40, round(ms))
        print(f"    {key:<26} {ms:7.2f}ms {bar}")
    if hidden:
        hidden_ms = sum(v for _, v in hidden)
        print(f"    … {len(hidden)} others < {TIMING_THRESHOLD_MS:.1f}ms (total {hidden_ms:.2f}ms)")
    print()


def _print_cache_state(state: DaemonState, hw: HardwareInfo, cfg: Config) -> None:
    now = time.monotonic()
    print("═" * 70)
    print("  CACHE STATE")
    print("═" * 70)
    for label, (_, ts) in state.hd_temp_cache.items():
        age = now - ts
        status = "STALE → refresh on next poll" if age >= HD_TEMP_CACHE_TTL else "fresh"
        print(f"  hd_temp[{label}]             age={age:6.2f}s  ttl={HD_TEMP_CACHE_TTL:.0f}s  {status}")
    for label, (_, ts) in state.fan_speed_cache.items():
        age = now - ts
        status = "STALE → refresh on next poll" if age >= FAN_CACHE_TTL else "fresh"
        print(f"  fan_speed[{label}]           age={age:6.2f}s  ttl={FAN_CACHE_TTL:.0f}s  {status}")
    for bat_id, cache in state.battery_sys_cache.items():
        age = now - cache.ts
        status = "STALE → refresh on next poll" if age >= BAT_CACHE_TTL else "fresh"
        print(f"  battery_sys[{bat_id}]  age={age:6.2f}s  ttl={BAT_CACHE_TTL:.0f}s  {status}")
    if hw.battery_mouse_id:
        age = now - state.battery_mouse_cache.ts
        status = "STALE" if age >= PERIPH_CACHE_TTL else "fresh"
        print(f"  battery_mouse                age={age:6.2f}s  ttl={PERIPH_CACHE_TTL:.0f}s  {status}")
    if hw.battery_kbd_id:
        age = now - state.battery_kbd_cache.ts
        status = "STALE" if age >= PERIPH_CACHE_TTL else "fresh"
        print(f"  battery_kbd                  age={age:6.2f}s  ttl={PERIPH_CACHE_TTL:.0f}s  {status}")
    if hw.has_nvidia:
        age = now - state.gpu_cache_ts
        gpu_ttl = _gpu_cache_ttl()
        status = "STALE" if age >= gpu_ttl else "fresh"
        print(f"  gpu_nvidia                   age={age:6.2f}s  ttl={gpu_ttl:.0f}s  {status}")
    if state.net_info_cache.ts != float("-inf"):
        age = now - state.net_info_cache.ts
        status = "STALE → refresh on next poll" if age >= NET_INFO_TTL else "fresh"
        print(f"  net_info                     age={age:6.2f}s  ttl={NET_INFO_TTL:.0f}s  {status}")
    for label, (_, ts) in state.disk_smart_cache.items():
        age = now - ts
        rotational = hw.disk_smart_drives.get(label, ("", "", False))[2]
        ttl = cfg.disks.smart_interval_hdd if rotational else cfg.disks.smart_interval
        status = "STALE → refresh on next poll" if age >= ttl else "fresh"
        print(f"  disk_smart[{label}]          age={age:6.2f}s  ttl={ttl:.0f}s  {status}")
    print()


PROFILE_OUT_FILE     = Path("/tmp/pirostats_profile_out")
PROFILE_TOOLTIP_FILE = Path("/tmp/pirostats_profile_tooltip")


def run_profile(cfg_path: Path | None) -> None:
    """One-shot timing report covering every phase of a real loop iteration:
    config load, hardware discovery, formatter init, collect() (cold vs warm
    cache), rendering, the per-poll bookkeeping (mtime checks, periph rescan
    check, notifier, atomic file writes) and the cache TTL state. Writes to
    separate /tmp/pirostats_profile_* files so it never touches the files
    a real running daemon may be writing to."""
    t = time.perf_counter()
    cfg = load_config(cfg_path)
    config_ms = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    hw = discover_hardware(cfg)
    discovery_ms = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    fmt = PanelFormatter(cfg, hw)
    formatter_init_ms = (time.perf_counter() - t) * 1000

    state = DaemonState()
    notif = NotifState()
    watch_path = cfg_path or default_config_path()

    print("═" * 70)
    print("  STARTUP")
    print("═" * 70)
    print(f"  load_config()                {config_ms:7.2f}ms")
    print(f"  discover_hardware()          {discovery_ms:7.2f}ms")
    print(f"  PanelFormatter()             {formatter_init_ms:7.2f}ms")
    print()

    runs = (("cold", "Cold poll (empty cache)"), ("warm", "Warm poll (valid cache)"))
    summary: dict[str, dict[str, float]] = {}
    last_r = last_panel = last_tooltip = None

    for key, label in runs:
        sensor_t: dict[str, float] = {}
        panel_t:  dict[str, float] = {}
        tooltip_t: dict[str, float] = {}

        t = time.perf_counter()
        r = collect(state, hw, cfg, timings=sensor_t)
        collect_ms = (time.perf_counter() - t) * 1000
        apply_canonical_width(cfg, fmt.canonical_width(r))

        t = time.perf_counter()
        panel = fmt.format_panel(r, timings=panel_t)
        panel_ms = (time.perf_counter() - t) * 1000

        t = time.perf_counter()
        tooltip = fmt.format_tooltip(r, timings=tooltip_t)
        tooltip_ms = (time.perf_counter() - t) * 1000

        summary[key] = {"collect": collect_ms, "panel": panel_ms, "tooltip": tooltip_ms}
        last_r, last_panel, last_tooltip = r, panel, tooltip

        print("═" * 70)
        print(f"  {label.upper()}")
        print("═" * 70)
        _print_timings("collect() by section", sensor_t, collect_ms)
        _print_timings("format_panel() by item", panel_t, panel_ms)
        _print_timings("format_tooltip() by item", tooltip_t, tooltip_ms)

    # ── Deep-dive tooltip pages (mouse-wheel): built only while that page is
    #    shown, NOT every poll, and fastfetch is TTL-cached — so this is a
    #    per-page worst case, not steady-state cost. Page 0 is format_tooltip()
    #    above. Cache cleared first so command pages actually run. ─────────────
    page_t: dict[str, float] = {}
    pages._cache.clear()
    active = pages.build_pages(cfg.pages.order)
    for i, page in enumerate(active):
        if i == 0:
            continue
        t = time.perf_counter()
        if page.render == "cpu_cores":
            fmt.format_cpu_cores(last_r)
        elif page.render == "top_process":
            fmt.format_top_process(last_r)
        elif page.render == "graphs":
            fmt.format_graphs(last_r)
        else:
            pages.page_inner(page, i, len(active), cfg.display.tooltip_width)
        page_t[page.label] = (time.perf_counter() - t) * 1000

    print("═" * 70)
    print("  TOOLTIP PAGES (deep-dive body, only while that page is shown)")
    print("═" * 70)
    _print_timings("page body build (cold, uncached)", page_t, sum(page_t.values()))

    # ── Loop bookkeeping: everything else run_daemon() does each poll ──────────
    overhead: dict[str, float] = {}

    t = time.perf_counter()
    _mtime(watch_path)
    _mtime(PLASMA_CFG)
    overhead["mtime checks"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    needs_periph_rescan(hw, cfg, state)
    overhead["needs_periph_rescan"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    check_and_notify(last_r, cfg, notif, hw)
    overhead["check_and_notify"] = (time.perf_counter() - t) * 1000

    try:
        t = time.perf_counter()
        _write_atomic(PROFILE_OUT_FILE, last_panel)
        _write_atomic(PROFILE_TOOLTIP_FILE, last_tooltip)
        overhead["write_atomic x2"] = (time.perf_counter() - t) * 1000
    finally:
        PROFILE_OUT_FILE.unlink(missing_ok=True)
        PROFILE_TOOLTIP_FILE.unlink(missing_ok=True)
        PROFILE_OUT_FILE.with_suffix(".tmp").unlink(missing_ok=True)
        PROFILE_TOOLTIP_FILE.with_suffix(".tmp").unlink(missing_ok=True)

    overhead_ms = sum(overhead.values())

    print("═" * 70)
    print("  LOOP OVERHEAD (per-poll, beyond collect/render)")
    print("═" * 70)
    _print_timings("bookkeeping", overhead, overhead_ms)

    print("═" * 70)
    print("  SUMMARY")
    print("═" * 70)
    print(f"  {'phase':<24}{'cold':>12}{'warm':>12}{'saved':>14}")
    rows = [
        ("load_config()", config_ms, None),
        ("discover_hardware()", discovery_ms, None),
        ("PanelFormatter()", formatter_init_ms, None),
        ("collect()", summary["cold"]["collect"], summary["warm"]["collect"]),
        ("format_panel()", summary["cold"]["panel"], summary["warm"]["panel"]),
        ("format_tooltip()", summary["cold"]["tooltip"], summary["warm"]["tooltip"]),
        ("loop overhead", overhead_ms, None),
    ]
    for name, cold, warm in rows:
        if warm is None:
            print(f"  {name:<24}{cold:>10.2f}ms{'':>12}{'':>14}")
        else:
            saved = cold - warm
            print(f"  {name:<24}{cold:>10.2f}ms{warm:>10.2f}ms{saved:>12.2f}ms")
    steady_state = summary["warm"]["collect"] + summary["warm"]["panel"] + \
        summary["warm"]["tooltip"] + overhead_ms
    print(f"  {'steady-state loop total':<24}{steady_state:>22.2f}ms")
    print()

    _print_cache_state(state, hw, cfg)


def _log_boot_ready(r: Readings, boot_pending: dict, boot_t0: float) -> None:
    """Log each watched sensor the first time it reports a value, timestamped
    from boot_t0. Called both right after the first paint and on every poll, so
    fast sensors present in the first paint are timed to it (~tens of ms) rather
    than to the first loop turn (~1.5s). No-op once boot_pending is empty or the
    watch window has elapsed (see BOOT_WATCH_WINDOW)."""
    if not boot_pending:
        return
    elapsed = time.monotonic() - boot_t0
    if elapsed > BOOT_WATCH_WINDOW:
        boot_pending.clear()
        return
    for name, is_ready in list(boot_pending.items()):
        if is_ready(r):
            print(f"[boot] {name} ready at +{elapsed:.2f}s", flush=True)
            del boot_pending[name]


def run_daemon(cfg_path: Path | None) -> None:
    boot_t0 = time.monotonic()   # reference for all [boot] timings, incl. discover

    ensure_dirs()
    PANEL_FILE.unlink(missing_ok=True)
    TOOLTIP_FILE.unlink(missing_ok=True)
    _set_page(0)   # a fresh daemon always starts on the full view

    cfg = load_config(cfg_path)
    hw  = discover_hardware(cfg)
    fmt = PanelFormatter(cfg, hw)
    active = _publish_pages(cfg)   # tooltip page list, rebuilt on every config reload

    # The inspection overlay is config-driven ([display] overlay): load_config
    # flags cfg.display.overlay, and here we key style-overlay.css off it. It's
    # re-read after every reload below, so toggling the config hot-swaps it live.
    overlay = cfg.display.overlay

    watch_path  = cfg_path or default_config_path()
    machine_paths = machine_source_paths(cfg_path)
    cfg_mtime   = _mtime(watch_path)
    machine_mtimes = [_mtime(p) for p in machine_paths]
    plasma_mtime = _mtime(PLASMA_CFG)
    geom_mtime = _mtime(GEOM_FILE)
    kdeglobals_mtime = _mtime(KDEGLOBALS)

    light         = _plasma_is_light()
    css_path      = _css_path_for(light)
    overlay_path  = _overlay_css_path(overlay)
    css_mtime     = _mtime(css_path)
    overlay_mtime = _mtime(overlay_path) if overlay_path is not None else 0.0
    css           = _read_css(css_path, overlay)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT,  _cleanup)

    state = DaemonState()
    notif = NotifState()

    boot_pending = dict(BOOT_WATCH)

    # First paint: write immediately with only the fast sensors, so the panel
    # isn't blank for the ~1-2s the first cache-cold read of the slow sensors
    # (ATA SMART ioctl, Bolt HID, nvidia-smi, /proc scan) would otherwise block.
    # They fill in on the first normal poll below.
    r = collect(state, hw, cfg, skip_slow=True)
    apply_canonical_width(cfg, fmt.canonical_width(r))
    _write_atomic(PANEL_FILE,     fmt.format_panel(r, css=css))
    _write_atomic(TOOLTIP_FILE, _render_tooltip(fmt, r, css, active))
    print(f"[boot] first paint at +{(time.monotonic() - boot_t0) * 1000:.0f}ms", flush=True)
    _log_boot_ready(r, boot_pending, boot_t0)   # fast sensors: timed to the first paint

    # Seed the geometry cache now if a live geom is already present (a plain
    # daemon restart doesn't wipe /tmp, so the geom-change watch below would
    # never fire and never persist it). This is what makes the NEXT cold boot
    # (tmpfs wiped) start already width-fitted. No-op when there's no live geom.
    cache_live_geom()

    while True:
        start = time.monotonic()

        # Reload config.toml/machines.toml when either changes on disk. The
        # parse is guarded: a malformed TOML saved while the daemon is running
        # must not kill it (with Restart=on-failure that would crash-loop until
        # the file is fixed). On failure we log and keep the last good
        # cfg/hw/fmt; the mtimes are still advanced so we don't retry every poll
        # — only when a file changes again (i.e. the user saves a corrected one).
        new_cfg_mtime  = _mtime(watch_path)
        new_machine_mtimes = [_mtime(p) for p in machine_paths]
        if new_cfg_mtime != cfg_mtime or new_machine_mtimes != machine_mtimes:
            cfg_mtime, machine_mtimes = new_cfg_mtime, new_machine_mtimes
            try:
                new_cfg = load_config(cfg_path)
                new_hw  = discover_hardware(new_cfg)
                fmt = PanelFormatter(new_cfg, new_hw)
                cfg, hw = new_cfg, new_hw
                active = _publish_pages(cfg)   # pages.order may have changed
                plasma_mtime = _mtime(PLASMA_CFG)
                geom_mtime = _mtime(GEOM_FILE)
            except Exception as e:
                print(f"[reload] config reload failed, keeping previous: {e}", flush=True)

        # Reload config + rebuild the formatter when the KDE panel is
        # moved/resized: moving it to another edge can flip the orientation,
        # which changes BOTH the formatter's root class/render path AND the item
        # set ([panel_horizontal] vs [panel_vertical]). The item set lives in
        # cfg.panel, resolved by load_config from the orientation — so
        # rebuilding the formatter alone would flip the layout but keep the
        # previous orientation's items.
        # The plasmoid's geometry file is watched alongside appletsrc: the panel's
        # usable width and glyph advance drive the vertical bar's auto-fit, so a
        # resize or a widget-font change (which rewrite that file) must re-run
        # load_config too.
        # Guarded like the config reload above: a transient read must not crash.
        new_plasma_mtime = _mtime(PLASMA_CFG)
        new_geom_mtime = _mtime(GEOM_FILE)
        if new_plasma_mtime != plasma_mtime or new_geom_mtime != geom_mtime:
            plasma_mtime = new_plasma_mtime
            geom_mtime = new_geom_mtime
            # The plasmoid just (re)published its geometry: persist it so the next
            # cold start (tmpfs GEOM_FILE wiped) seeds a width-fitted first paint
            # from it instead of the unfitted defaults (see config.GEOM_CACHE).
            cache_live_geom()
            try:
                cfg = load_config(cfg_path)
                cfg_mtime, machine_mtimes = _mtime(watch_path), [_mtime(p) for p in machine_paths]
                fmt = PanelFormatter(cfg, hw)
                active = _publish_pages(cfg)
            except Exception as e:
                print(f"[reload] plasma-triggered reload failed, keeping previous: {e}", flush=True)

        # Overlay is config-driven ([display] overlay): when a reload above flips
        # it, cfg.display.overlay carries the new state — here we re-sync the CSS
        # side, swapping style-overlay.css in or out and re-reading. No-op on the
        # common poll where it's unchanged.
        if cfg.display.overlay != overlay:
            overlay = cfg.display.overlay
            overlay_path = _overlay_css_path(overlay)
            overlay_mtime = _mtime(overlay_path) if overlay_path is not None else 0.0
            css = _read_css(css_path, overlay)

        # Re-detect the light/dark color scheme when kdeglobals changes (the
        # user switched Global Theme). On a flip, swap the served stylesheet
        # (style-dark.css <-> style-light.css) and its overlay, then reload
        # so the next paint matches. Watched by mtime like appletsrc/geom above;
        # the css_mtime/overlay_mtime are re-baselined to the NEW file so the
        # edit-on-disk reload below keeps tracking the right one.
        new_kdeglobals_mtime = _mtime(KDEGLOBALS)
        if new_kdeglobals_mtime != kdeglobals_mtime:
            kdeglobals_mtime = new_kdeglobals_mtime
            new_light = _plasma_is_light()
            if new_light != light:
                light = new_light
                css_path = _css_path_for(light)
                overlay_path = _overlay_css_path(overlay)
                css_mtime = _mtime(css_path)
                overlay_mtime = _mtime(overlay_path) if overlay_path is not None else 0.0
                css = _read_css(css_path, overlay)

        # Reload style-dark.css — and the active style-overlay.css — when either
        # changes on disk. The overlay is watched even before it exists: _mtime
        # returns 0.0 for a missing file, so creating it later trips the check too.
        new_css_mtime = _mtime(css_path)
        new_overlay_mtime = _mtime(overlay_path) if overlay_path is not None else 0.0
        if new_css_mtime != css_mtime or new_overlay_mtime != overlay_mtime:
            css_mtime, overlay_mtime = new_css_mtime, new_overlay_mtime
            css = _read_css(css_path, overlay)

        # Retry peripherals periodically when not found, or found at a path
        # that has since died under them
        if needs_periph_rescan(hw, cfg, state):
            if time.monotonic() - hw.periph_scan_ts >= PERIPH_RESCAN_INTERVAL:
                hw = rescan_peripherals(hw, cfg)
                fmt = PanelFormatter(cfg, hw)  # rebuild if hw changed

        r     = collect(state, hw, cfg)
        # Re-fit the tooltip floor to the main page's canonical (max) width each
        # poll: cheap (one synthetic build), and it picks up config reloads, a
        # disk hotplug or a geom change without a dedicated trigger. Stable across
        # polls since _maxed_readings ignores the live values.
        apply_canonical_width(cfg, fmt.canonical_width(r))
        notif = check_and_notify(r, cfg, notif, hw)

        # Keep the top-processes page responsive: while it's the active page,
        # resample every poll off its own prev-state instead of the panel's
        # 15s-TTL cache, so it refreshes each poll instead of every ~15s.
        if active[_read_page() % len(active)].render == "top_process":
            r.top_process_full = read_top_process_page(state) or r.top_process_full

        # See BOOT_WATCH_WINDOW comment above. No-op after the first 90s of
        # this process's life, or once every watched sensor has reported once.
        _log_boot_ready(r, boot_pending, boot_t0)

        _write_atomic(PANEL_FILE,     fmt.format_panel(r, css=css))
        _write_atomic(TOOLTIP_FILE, _render_tooltip(fmt, r, css, active))
        last_page = _read_page()

        # Sleep until the next poll in small steps, re-rendering the tooltip at
        # once if the page changed under us (mouse-wheel) so switching feels
        # instant instead of lagging up to a full poll_interval. Command pages
        # re-run their command; page 0 reuses this poll's readings.
        elapsed = time.monotonic() - start
        remaining = cfg.display.poll_interval - elapsed
        while remaining > 0.0:
            time.sleep(min(0.1, remaining))
            remaining -= 0.1
            page = _read_page()
            if page != last_page:
                last_page = page
                # Freshen the top-processes list on switch too, so the instant
                # re-render already shows the full fixed-size table instead of the
                # previous poll's stale panel list (which would resize a beat later).
                if active[page % len(active)].render == "top_process":
                    r.top_process_full = read_top_process_page(state) or r.top_process_full
                _write_atomic(TOOLTIP_FILE, _render_tooltip(fmt, r, css, active))


def run_list_items() -> None:
    """List `metric[:form]` tokens with where they can go (panel/tooltip), then
    exits. Placement is DERIVED (item_surfaces): the intersection of the
    form's surfaces and the metric's — the panel accepts the visuals and
    "glyph label: val", the tooltip also the wide forms (combo, pairs, strings)."""
    from metrics import METRICS, item_surfaces
    from forms import Form, Surface

    def where(metric: str, form) -> str:
        s = item_surfaces(metric, form)
        panel, tip = bool(s & Surface.PANEL), bool(s & Surface.TOOLTIP)
        return ("panel + tooltip" if panel and tip else
                "panel only" if panel else "tooltip only" if tip else "-")

    rows: list[tuple[str, str]] = []
    for metric, m in METRICS.items():
        if m.intrinsic_shape is not None:
            rows.append((metric, where(metric, None)))
        else:
            for form in m.forms:
                token = metric if form is Form.VALUE else f"{metric}:{form.value}"
                rows.append((token, where(metric, form)))
    width = max(len(t) for t, _ in rows)
    print("Available items (metric[:form] → where it can go):\n")
    # Order: first by placement (a→z), then by token (a→z).
    for token, w in sorted(rows, key=lambda tw: (tw[1], tw[0])):
        print(f"  {token:<{width}}  {w}")


def run_page(step: str) -> None:
    """Mouse-wheel command: step the tooltip page ±1 with wrap-around. Delegates
    to pagestate.step_page, which wraps against the active page count the daemon
    publishes to NPAGES_FILE (so it needn't parse config), is a no-op when no
    deep-dive pages are configured, and flock-serializes the read-modify-write so
    a fast scroll never drops a notch. The daemon renders the new page on its
    next poll (the tick the widget re-reads). Normally the `pirostats` entrypoint
    short-circuits to step_page before importing this heavy module; this stays as
    the argparse-dispatch path."""
    step_page(step)


def run_click() -> None:
    """Click command: run the click action, detached so pirostats returns at
    once (the widget's executable engine waits on it). Uniform today
    (plasma-systemmonitor); per-page routing would build the active list here."""
    argv = pages.default_click()
    try:
        subprocess.Popen(argv, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[click] failed to launch {argv!r}: {e}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(prog="pirostats")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # --config is common to almost every mode: a shared parent, inherited by
    # the subparsers that need it (all but list-items).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=None, metavar="PATH",
                        help="Path to the TOML (default: ~/.config/pirostats/config.toml, else the shipped config)")

    # No --overlay flag anywhere: the inspection overlay is a config key
    # ([display] overlay, hot-reloaded), watched on the live panel/tooltip.
    sub.add_parser("daemon", parents=[common],
                   help="Production loop: renders continuously and writes the files the widget reads")

    p_render = sub.add_parser("render", parents=[common],
                              help="One-shot render of panel/tooltip, then exits")
    p_render.add_argument("--component", choices=("panel", "tooltip", "both"), default="both",
                          help="What to render (default: both)")
    p_render.add_argument("--format", choices=("text", "html"), default="text", dest="fmt",
                          help="text = stripped to stdout; html = /tmp/pirostats_render_* files (default: text)")
    p_render.add_argument("--layout", choices=("auto", "horizontal", "vertical"), default="auto",
                          help="Forces the panel orientation (horizontal = column, "
                               "vertical = inline bar); auto = detection like the daemon (default)")
    p_render.add_argument("--page", choices=("full", *pages.REGISTRY), default=None,
                          help="Render a tooltip deep-dive page (any page, even one not in "
                               "pages.order) instead of the full view; implies --component tooltip. "
                               "Image pages (graphs) show only their legends in text format")

    sub.add_parser("probe", parents=[common],
                   help="One-shot: probe the hardware and print the raw readings (no render)")
    sub.add_parser("profiling", parents=[common],
                   help="One-shot timing report (cold/warm cache, per-section/item)")
    sub.add_parser("list-items",
                   help="Lists the available items and where they can go, then exits")

    p_page = sub.add_parser("page",
                            help="Switch the tooltip page (bind to the widget's mouse-wheel commands)")
    p_page.add_argument("step", choices=("next", "prev"),
                        help="Move to the next/previous page (wraps around)")
    sub.add_parser("click",
                   help="Run the current page's click action (bind to the widget's click command)")

    args = parser.parse_args()

    if args.command == "daemon":
        run_daemon(args.config)
    elif args.command == "render":
        vertical = {"horizontal": False, "vertical": True}.get(args.layout)
        run_render(args.config, args.component, args.fmt, vertical, args.page)
    elif args.command == "probe":
        run_probe(args.config)
    elif args.command == "profiling":
        run_profile(args.config)
    elif args.command == "list-items":
        run_list_items()
    elif args.command == "page":
        run_page(args.step)
    elif args.command == "click":
        run_click()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
