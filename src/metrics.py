"""The "what" axis of an item: the METRICS, separate from the forms (forms.py).

A metric declares only what's intrinsic to it and independent of how you draw
it: what data it needs (`needs`, the hook into the one helper that reads it),
when the hardware is present (`gate`), on which surfaces it makes sense
regardless of form (`surfaces` — an IP doesn't belong in the compact panel),
and WHICH generic forms it supports (`forms` — cpu_usage supports them all
because it has a history; cpu_temp only the value).

Glyph, label and thresholds are NOT here: they live in external files keyed by
metric name — glyphs in style/icons.toml (theme), labels in lang/<language>.toml
(i18n), thresholds in [thresholds]. This is the structure; the rendering (how
the number is formatted) is in the formatter.

Placement is DERIVED, not declared: an item's actual surfaces are the
intersection of the form's (FORM_SURFACES) and the metric's.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from forms import Form, Shape, Surface, FORM_SURFACES

# (f, r) -> bool — `f` is the PanelFormatter (accesses f._hw / f._cfg), `r` the Readings.
GateFn = Callable[..., bool]


def _ALWAYS(f, r) -> bool:
    return True


# ── hardware gates (a metric "is present" if its sensor is present) ───────────
# Properties of the DATA, so they live with the metric.
_g_cpu_temp      = lambda f, r: f._hw.cpu_temp_path is not None
_g_cpu_turbo     = lambda f, r: f._hw.cpu_turbo_supported
_g_net           = lambda f, r: f._hw.net_device is not None
_g_disk_io       = lambda f, r: f._hw.disk_io_device is not None
_g_wifi          = lambda f, r: f._hw.has_wifi
_g_fan           = lambda f, r: bool(f._hw.fan_paths)
_g_nvidia        = lambda f, r: f._hw.has_nvidia
_g_intel_freq    = lambda f, r: f._hw.intel_gpu_freq_path is not None
_g_intel_pci     = lambda f, r: f._hw.intel_gpu_pci is not None
# One gate per path, not one per card: a card exposes only some of these
# (an APU has no fan), so each row appears exactly where its sysfs file does.
_g_amd           = lambda f, r: f._hw.amd_gpu_busy_path is not None
_g_amd_temp      = lambda f, r: f._hw.amd_gpu_temp_path is not None
_g_amd_fan       = lambda f, r: f._hw.amd_gpu_fan_path is not None
_g_amd_freq      = lambda f, r: f._hw.amd_gpu_freq_path is not None
_g_amd_vram      = lambda f, r: f._hw.amd_gpu_vram_total_path is not None
_g_battery_sys   = lambda f, r: bool(f._hw.battery_sys_ids)
_g_battery_mouse = lambda f, r: f._hw.battery_mouse_id is not None or f._cfg.battery.mouse_bolt is not None
_g_battery_kbd   = lambda f, r: f._hw.battery_kbd_id is not None or f._cfg.battery.kbd_bolt is not None
_g_backlight     = lambda f, r: f._hw.has_backlight
_g_swap          = lambda f, r: r.swap_usage is not None
_g_updates       = lambda f, r: bool(f._cfg.system_updates.file)
_g_server        = lambda f, r: bool(f._cfg.server_check.file)


# ── supported-form sets (the "how" axis a metric admits) ──────────────────────
# HISTORIED: metrics with a history buffer (cpu/mem usage) → the whole visual
# menu EXCEPT `pair`, which only makes sense for multi-instance metrics (disks, fans).
_HISTORIED  = frozenset(Form) - {Form.PAIR}
_VALUE      = frozenset({Form.VALUE})               # number only
_VALUE_PAIR = frozenset({Form.VALUE, Form.PAIR})    # number, or paired (multi-instance)
_PAIR       = frozenset({Form.PAIR})                # paired only


@dataclass(frozen=True)
class Metric:
    name: str
    needs: frozenset = frozenset()
    gate: GateFn = _ALWAYS
    forms: frozenset = field(default_factory=lambda: _VALUE)
    # Surfaces the METRIC admits regardless of form. Default: everywhere. Some
    # are tooltip-only by nature (an IP, uptime): it's not about the form, it's
    # content that doesn't fit the compact panel.
    surfaces: Surface = Surface.ALL
    # Metrics with their OWN skeleton (net_speed/disk_io = adaptive DUO,
    # top_process = TRIPLE_L): the form is intrinsic, not picked from the menu.
    # `forms` is ignored.
    intrinsic_shape: Optional[Shape] = None


def _m(name, **kw) -> tuple[str, Metric]:
    return name, Metric(name, **kw)


METRICS: dict[str, Metric] = dict([
    # ── cpu/mem usage: the only ones with a history → full visual menu ──
    _m("cpu_usage",  forms=_HISTORIED),
    _m("mem_usage",  forms=_HISTORIED),
    _m("swap_usage", needs={"swap_usage"}, gate=_g_swap),

    # ── frequency / turbo / cpu temp ──
    # cpu_freq shows the turbo glyph INTRINSICALLY when the data is there:
    # TRIPLE_L rendering with turbo, PAIR without. Hence it also needs the
    # cpu_turbo capability.
    _m("cpu_freq",  needs={"cpu_freq", "cpu_turbo"}),
    _m("cpu_turbo", needs={"cpu_turbo"}, gate=_g_cpu_turbo),
    _m("cpu_temp",  needs={"cpu_temp"},  gate=_g_cpu_temp),

    # ── disks (multi-instance) ──
    _m("hd_temp",    needs={"hd_temp"},    forms=_VALUE_PAIR),
    _m("disk_usage", needs={"disk_usage"}),                   # GB is intrinsic in the tooltip
    _m("disk_smart", needs={"disk_smart"}, forms=_PAIR,       surfaces=Surface.TOOLTIP),

    # ── gpu ──
    _m("gpu_nvidia_temp",      needs={"gpu_nvidia"}, gate=_g_nvidia),
    _m("gpu_nvidia_usage",     needs={"gpu_nvidia"}, gate=_g_nvidia),
    _m("gpu_nvidia_mem_usage", needs={"gpu_nvidia"}, gate=_g_nvidia),
    _m("gpu_nvidia_dec_usage", needs={"gpu_nvidia"}, gate=_g_nvidia),
    _m("gpu_nvidia_fan_speed", needs={"gpu_nvidia"}, gate=_g_nvidia),
    _m("gpu_intel_freq",       needs={"gpu_intel_freq"}, gate=_g_intel_freq),
    _m("gpu_intel_usage",      needs={"gpu_intel_usage"}, gate=_g_intel_pci),
    _m("gpu_intel_dec_usage",  needs={"gpu_intel_dec"},   gate=_g_intel_pci),
    _m("gpu_amd_usage",        needs={"gpu_amd"}, gate=_g_amd),
    _m("gpu_amd_mem_usage",    needs={"gpu_amd"}, gate=_g_amd_vram),
    _m("gpu_amd_temp",         needs={"gpu_amd"}, gate=_g_amd_temp),
    _m("gpu_amd_fan_speed",    needs={"gpu_amd"}, gate=_g_amd_fan),
    _m("gpu_amd_freq",         needs={"gpu_amd"}, gate=_g_amd_freq),
    _m("screen_brightness",    needs={"screen_brightness"}, gate=_g_backlight),

    # ── fans / batteries (multi-instance for the first three) ──
    _m("fan_speed",   needs={"fan_speed"},   gate=_g_fan, forms=_VALUE_PAIR),
    _m("battery_sys", needs={"battery_sys"}, gate=_g_battery_sys),  # rate/limit is intrinsic in the tooltip
    _m("battery_mouse", needs={"battery_mouse"}, gate=_g_battery_mouse),
    _m("battery_kbd",   needs={"battery_kbd"},   gate=_g_battery_kbd),

    # ── network: speed (adaptive DUO) and identity (tooltip-only, composed value) ──
    _m("net_speed", needs={"net_speed"}, gate=_g_net, intrinsic_shape=Shape.DUO),
    _m("disk_io",   needs={"disk_io"},   gate=_g_disk_io, intrinsic_shape=Shape.DUO),
    _m("net_device",    needs={"net_info"}, gate=_g_net, surfaces=Surface.TOOLTIP),
    _m("net_ip",        needs={"net_info"}, gate=_g_net, surfaces=Surface.TOOLTIP),
    _m("net_device_ip", needs={"net_info"}, gate=_g_net, surfaces=Surface.TOOLTIP),
    _m("wifi_ssid",        needs={"net_info"}, gate=_g_wifi, surfaces=Surface.TOOLTIP),
    _m("wifi_signal",      needs={"net_info"}, gate=_g_wifi),
    _m("wifi_ssid_signal", needs={"net_info"}, gate=_g_wifi, surfaces=Surface.TOOLTIP),

    # ── system (tooltip-only) ──
    _m("uptime",      needs={"uptime"},   surfaces=Surface.TOOLTIP),
    _m("load_avg",    needs={"load_avg"}, surfaces=Surface.TOOLTIP),
    _m("top_process", needs={"top_process"}, surfaces=Surface.TOOLTIP, intrinsic_shape=Shape.TRIPLE_L),

    # ── misc ──
    _m("system_updates", needs={"system_updates"}, gate=_g_updates),
    _m("server_check",   needs={"server_check"},   gate=_g_server),
])


# ── validity and derivation ────────────────────────────────────────────────────

def supports(metric: str, form: Form) -> bool:
    """True if the metric admits that generic form. Metrics with their own
    skeleton (intrinsic_shape) don't take forms from the menu."""
    m = METRICS.get(metric)
    if m is None or m.intrinsic_shape is not None:
        return False
    return form in m.forms


def item_surfaces(metric: str, form: Form) -> Surface:
    """The actual surfaces of `metric:form`: the intersection of the form's and
    the metric's. Surface(0) = a meaningless combination."""
    m = METRICS[metric]
    if m.intrinsic_shape is not None:
        return m.surfaces
    return FORM_SURFACES[form] & m.surfaces
