"""The item registry, keyed by (metric × form) instead of a flat name. Two
things live here:

  1. The DISPATCH `_RENDER[(metric, form)] -> GroupFn`: how a row is composed.
     Regular ones with items.py's declarative library (row/per + cell-factory),
     irregular ones (combos, string joins, batteries, adaptive, own skeletons)
     as explicit exception functions. `render()` computes the Ident (metric +
     form) and threads it to the cells, which write the final two-axis class
     `.item-<metric>.form-<form>` (role `aux`) — this is also where the two
     [panel_horizontal]/[panel_vertical] sections merge: the BAR form picks
     bar (vertical) or column (horizontal) on its own via `_form_token`.

  2. The TOKEN LAYER that formatter/config/sensors consume: `parse` of
     "metric[:form]", from which `render_item`, `item_gate`,
     `needed_capabilities` and the validations (unknown/misplaced) derive.

Surfaces (placement), gate and needs come from metrics.py; the row building
blocks from items.py: only "how to render" and token resolution live here.
"""
from __future__ import annotations

from forms import Form, Surface, form_from_token
from metrics import METRICS, item_surfaces, supports
from render_model import Ident, SEPARATOR_ITEMS
import traces
from items import (  # the cell-factory library: the row building blocks
    row, per, label, value, spark, braille, freq_value, turbo_icon, turbo_value,
    fan_value, gpu_fan_value, hd_temp_value, disk_label, disk_value, disk_space,
    mem_space, _thr, _TEMP, _NONE,
)

# form → `form-<...>` class token (the form part of the Ident). BAR depends on
# orientation (bar in vertical, column in horizontal); the others match the
# Form's value. None = own skeletons (net_speed/disk_io/top_process): their
# cells label themselves (DUO parts with no form, top_process as value), so
# there's no single form here.
def _form_token(form: Form | None, vertical: bool) -> str | None:
    if form is None:
        return None
    if form is Form.BAR:
        return "bar" if vertical else "column"
    return form.value


# ── dispatch (metric, form) → GroupFn ─────────────────────────────────────────
# A single table. REGULAR entries are composed with the declarative library
# (row/per + cell-factory); IRREGULAR ones (combos, string joins, batteries,
# adaptive, own skeletons) are explicit exception functions `(f, ident, r, t)
# -> rows`. The `ident` (metric + form) passed in is what the cells use to
# write the final two-axis class; exceptions that compose cells of a different
# form (bar+history combos, DUO parts) build their own per-cell Idents.

GroupFn = object  # (f, ident, r, tooltip) -> list[Row]


def _historied(attr: str, thrf: str, hist: str, sparkname: str, bpref: str) -> dict:
    """The 8 forms of the historied metrics (cpu_usage/mem_usage), parametrized."""
    thr = _thr(thrf)

    def bar(f, ident, r, t):  # :bar = bar (vertical) or column (horizontal)
        v, th = getattr(r, attr), getattr(f._cfg.thresholds, thrf)
        return traces.bar_row(f, v, th, t, ident) if f._vertical else traces.column_row(f, v, th, ident)

    return {
        Form.VALUE:         row(label(), value(attr, "%", thr)),
        Form.BAR:           bar,
        Form.SPARK:         (lambda f, ident, r, t: traces.spark_row(f, getattr(r, hist), sparkname, t, ident)),
        Form.BRAILLE:       (lambda f, ident, r, t: traces.braille_row(f, getattr(r, hist), bpref, t, ident)),
        Form.SPARK_VALUE:   row(label(), spark(hist, sparkname), value(attr, "%", thr)),
        Form.BRAILLE_VALUE: row(label(), braille(hist, bpref), value(attr, "%", thr)),
        Form.BAR_SPARK:     (lambda f, ident, r, t: traces.bar_spark_row(
            f, bpref, getattr(r, attr), getattr(f._cfg.thresholds, thrf), getattr(r, hist), sparkname, t)),
        Form.BAR_BRAILLE:   (lambda f, ident, r, t: traces.bar_braille_row(
            f, bpref, getattr(r, attr), getattr(f._cfg.thresholds, thrf), getattr(r, hist), t)),
    }


_RENDER: dict[tuple[str, Form | None], GroupFn] = {}
for _pref, _attr, _hist, _spark in [
    ("cpu", "cpu_usage", "cpu_history", "cpu_spark"),
    ("mem", "mem_usage", "mem_history", "mem_spark"),
]:
    for _form, _fn in _historied(_attr, _attr, _hist, _spark, _pref).items():
        _RENDER[(_attr, _form)] = _fn

# mem_usage:value gains a GB used/total middle column (tooltip-only, like disks);
# cpu has no GB, so this overrides only the mem VALUE form from the loop above.
_RENDER[("mem_usage", Form.VALUE)] = row(
    label(), mem_space(_thr("mem_usage")), value("mem_usage", "%", _thr("mem_usage")))

_RENDER.update({
    # ── single-value metrics (_std family) ──
    ("swap_usage", Form.VALUE): row(label(), value("swap_usage", "%", _thr("swap_usage"))),
    ("cpu_temp", Form.VALUE): row(label(), value("cpu_temp", _TEMP, _thr("cpu_temp"))),
    ("cpu_freq", Form.VALUE): row(label(), turbo_icon(), freq_value("cpu_freq")),  # intrinsic turbo
    ("cpu_turbo", Form.VALUE): row(label(), turbo_value()),
    ("gpu_nvidia_temp", Form.VALUE): row(label(), value("gpu_temp", _TEMP, _thr("gpu_nvidia_temp"))),
    ("gpu_nvidia_usage", Form.VALUE): row(label(), value("gpu_usage", "%", _thr("gpu_nvidia_usage"))),
    ("gpu_nvidia_mem_usage", Form.VALUE): row(label(), value("gpu_mem", "%", _thr("gpu_nvidia_mem_usage"))),
    ("gpu_nvidia_dec_usage", Form.VALUE): row(label(), value("gpu_dec", "%", _thr("gpu_nvidia_dec_usage"))),
    ("gpu_nvidia_fan_speed", Form.VALUE): row(label(), gpu_fan_value()),
    ("gpu_intel_freq", Form.VALUE): row(label(), freq_value("gpu_intel_freq")),
    ("gpu_intel_usage", Form.VALUE): row(label(), value("gpu_intel_usage", "%", _thr("gpu_intel_usage"))),
    ("gpu_intel_dec_usage", Form.VALUE): row(label(), value("gpu_intel_dec_usage", "%", _thr("gpu_intel_dec_usage"))),
    ("gpu_amd_usage", Form.VALUE): row(label(), value("gpu_amd_usage", "%", _thr("gpu_amd_usage"))),
    ("gpu_amd_mem_usage", Form.VALUE): row(label(), value("gpu_amd_mem_usage", "%", _thr("gpu_amd_mem_usage"))),
    ("gpu_amd_temp", Form.VALUE): row(label(), value("gpu_amd_temp", _TEMP, _thr("gpu_amd_temp"))),
    ("gpu_amd_fan_speed", Form.VALUE): row(label(), fan_value("gpu_amd_fan_speed")),
    ("gpu_amd_freq", Form.VALUE): row(label(), freq_value("gpu_amd_freq")),
    ("screen_brightness", Form.VALUE): row(label(), value("screen_brightness", "%", _NONE)),

    # ── multi-instance (disks, fans): value, and pair where provided ──
    ("hd_temp", Form.VALUE): per(
        lambda f, r: [l for l in f._hw.hd_temp_paths if r.hd_temps.get(l) is not None],
        label(lambda f, r, key: f"{f._cfg.labels.get('hd_temp', '')} {f._hd_label(key)}"),
        hd_temp_value(_thr("hd_temp"))),
    ("hd_temp", Form.PAIR): (lambda f, ident, r, t: f._hd_temp_pair(r, t)),
    ("disk_usage", Form.VALUE): per(
        lambda f, r: r.disk_usage, disk_label(),
        disk_space(_thr("disk_usage")), disk_value(_thr("disk_usage"))),
    ("disk_smart", Form.PAIR): (lambda f, ident, r, t: f._disk_smart_pair(r, t)),
    ("fan_speed", Form.VALUE): per(
        lambda f, r: f._hw.fan_paths, label(lambda f, r, key: f"Fan{key}"), fan_value()),
    ("fan_speed", Form.PAIR): (lambda f, ident, r, t: f._fan_speed_pair(r, t)),

    # ── batteries ──
    ("battery_sys", Form.VALUE): (lambda f, ident, r, t: [
        f._battery_sys(bat, t, idx=i) for i, bat in enumerate(r.battery_sys)]),
    ("battery_mouse", Form.VALUE): (lambda f, ident, r, t: [f._battery_periph(r.battery_mouse, "battery_mouse", t)]),
    ("battery_kbd", Form.VALUE): (lambda f, ident, r, t: [f._battery_periph(r.battery_kbd, "battery_kbd", t)]),

    # ── network / wifi (composed values, tooltip-only) ──
    ("net_device", Form.VALUE): (lambda f, ident, r, t: f._string_row("net_device", r.net_device, t)),
    ("net_ip", Form.VALUE): (lambda f, ident, r, t: f._string_row("net_ip", r.ip_address, t)),
    ("net_device_ip", Form.VALUE): (lambda f, ident, r, t: f._net_device_ip(r, t)),
    ("wifi_ssid", Form.VALUE): (lambda f, ident, r, t: f._string_row("wifi_ssid", r.wifi_ssid, t)),
    ("wifi_signal", Form.VALUE): (lambda f, ident, r, t: f._wifi_signal(r, t)),
    ("wifi_ssid_signal", Form.VALUE): (lambda f, ident, r, t: f._wifi_ssid_signal(r, t)),

    # ── system ──
    ("uptime", Form.VALUE): (lambda f, ident, r, t: [f._uptime(r, t)]),
    ("load_avg", Form.VALUE): (lambda f, ident, r, t: [f._load_avg(r, t)]),
    ("system_updates", Form.VALUE): (lambda f, ident, r, t: [f._system_updates(r, t)]),
    ("server_check", Form.VALUE): (lambda f, ident, r, t: [f._server_check(r, t)]),

    # ── own skeleton (intrinsic form, not from the menu) ──
    ("net_speed", None): (lambda f, ident, r, t: f._net_speed(r, t)),
    ("disk_io", None): (lambda f, ident, r, t: f._disk_io(r, t)),
    ("top_process", None): (lambda f, ident, r, t: f._top_process(r, t)),
})


def render(f, metric: str, form: Form | None, r, tooltip: bool) -> list:
    """Composes the rows of `metric:form` from the dispatch table. Cells
    receive the Ident (metric + form, BAR form resolved for orientation) and
    write the two-axis class `item-<metric> form-<form>` directly — no
    intermediate flat name or retagging."""
    ident = Ident(metric, _form_token(form, f._vertical))
    fn = _RENDER.get((metric, form))
    return fn(f, ident, r, tooltip) if fn else []


# ── token layer: the boundary formatter/config/sensors consume ───────────────
# An item in the config is a "metric[:form]" token (or a separator). This is
# where it's resolved, and render, gate, needs and the validations all derive
# from it — the same API items.py used to expose, now keyed by metric instead
# of a flat name.

def parse(token: str) -> tuple[str, Form | None] | None:
    """`"cpu_usage:braille_value"` → `("cpu_usage", Form.BRAILLE_VALUE)`.
    `"net_speed"` → `("net_speed", None)` (own skeleton). None = invalid token
    (unknown metric, unsupported form, form on an intrinsic metric)."""
    metric, _, ftok = token.partition(":")
    m = METRICS.get(metric)
    if m is None:
        return None
    if m.intrinsic_shape is not None:
        return (metric, None) if not ftok else None
    try:
        form = form_from_token(ftok)
    except ValueError:
        return None
    return (metric, form) if supports(metric, form) else None


def render_item(f, token: str, r, tooltip: bool) -> list:
    """The formatter's entry point: renders one token. Unknown → no row."""
    parsed = parse(token)
    if parsed is None:
        return []
    return render(f, parsed[0], parsed[1], r, tooltip)


def item_gate(f, token: str, r) -> bool:
    """Hardware gate of the token, from its metric. Unknown token → True (the
    gate doesn't hide it, the empty render does; unknown_item_names flags it)."""
    parsed = parse(token)
    return METRICS[parsed[0]].gate(f, r) if parsed else True


# Capabilities enabled notifications consume even without the item on screen
# (key = NotificationConfig field, value = capability).
_NOTIFY_CAPS = {
    "cpu_temp": "cpu_temp", "gpu_nvidia_temp": "gpu_nvidia", "gpu_amd_temp": "gpu_amd",
    "disk_usage": "disk_usage",
    "disk_smart": "disk_smart", "hd_temp": "hd_temp", "battery_sys": "battery_sys",
    "battery_mouse": "battery_mouse", "battery_kbd": "battery_kbd",
    "load_avg": "load_avg", "server_check": "server_check",
}


def needed_capabilities(cfg) -> set[str]:
    """Sensor capabilities to read this poll: the union of the configured
    metrics' `needs` plus the ones enabled notifications consume. cpu_usage/
    mem_usage have empty needs (always collected)."""
    caps: set[str] = set()
    for token in cfg.panel.item_set() | cfg.tooltip.item_set():
        parsed = parse(token)
        if parsed:
            caps |= METRICS[parsed[0]].needs
    n = cfg.notifications
    for flag, cap in _NOTIFY_CAPS.items():
        if getattr(n, flag, False):
            caps.add(cap)
    # The graphs page charts GPU usage + decoder and network up/down even with
    # no such item on a surface, so request their caps; the hardware gate in
    # collect narrows this to the GPU / interface actually present.
    if "graphs" in cfg.pages.order:
        caps |= {"gpu_nvidia", "gpu_amd", "gpu_intel_usage", "gpu_intel_dec", "net_speed"}
    return caps


def unknown_item_names(names) -> set[str]:
    """Tokens that don't resolve to a valid item (a typo in the toml).
    Separators are valid entries, not items, so they're excluded."""
    return {n for n in names if n not in SEPARATOR_ITEMS and parse(n) is None}


def misplaced_items(panel_names, tooltip_names) -> tuple[set[str], set[str]]:
    """Tokens placed on a surface their actual surfaces (form ∩ metric) don't
    admit: `cpu_usage:bar` in a [tooltip.*], `uptime` in a [panel.*]. Unknown
    tokens are ignored here (unknown_item_names flags those)."""
    def bad(names, surface: Surface) -> set[str]:
        out: set[str] = set()
        for n in names:
            if n in SEPARATOR_ITEMS:
                continue
            parsed = parse(n)
            if parsed and not (item_surfaces(parsed[0], parsed[1]) & surface):
                out.add(n)
        return out
    return bad(panel_names, Surface.PANEL), bad(tooltip_names, Surface.TOOLTIP)
