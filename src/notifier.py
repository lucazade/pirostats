"""
Desktop notifications via gi.repository.Notify (GLib, part of python-gobject).
Uses edge-triggered logic: notifies once when a threshold is crossed, resets when
clear. Crossings that are noisy at poll granularity (the temperatures, load avg)
go through _sustained, which adds a hold time and hysteresis — see there.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from config import Config
from sensors import HardwareInfo, Readings
from units import TEMP_SCALE

try:
    import gi
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify as _Notify
    _Notify.init("pirostats")
    _GI_AVAILABLE = True
except Exception:
    _GI_AVAILABLE = False


def _send(title: str, body: str, icon: str = "dialog-error") -> None:
    if not _GI_AVAILABLE:
        return
    try:
        n = _Notify.Notification.new(title, body, icon)
        n.set_urgency(_Notify.Urgency.CRITICAL)
        n.set_timeout(_Notify.EXPIRES_NEVER)
        n.show()
    except Exception:
        pass


# ── State ─────────────────────────────────────────────────────────────────────

@dataclass
class Latch:
    """One _sustained alert's edge state: whether it's currently firing, and when
    the value first reached the trip point (None whenever it's below it)."""
    active: bool = False
    since: float | None = None


@dataclass
class NotifState:
    """Tracks which alerts are currently active (to avoid re-sending)."""
    disk: dict[str, bool] = field(default_factory=dict)         # mount → active
    disk_smart: dict[str, bool] = field(default_factory=dict)   # hd_temp label → active (failing)
    battery_sys: dict[str, bool] = field(default_factory=dict)  # battery id → active
    battery_mouse: bool = False
    battery_kbd: bool = False
    server: bool = False
    # Debounced alerts (_sustained): a latch each, keyed by label where per-device.
    cpu_temp: Latch = field(default_factory=Latch)
    gpu_nvidia_temp: Latch = field(default_factory=Latch)
    gpu_amd_temp: Latch = field(default_factory=Latch)
    hd_temp: dict[str, Latch] = field(default_factory=dict)     # label → latch
    load_avg: Latch = field(default_factory=Latch)


def _sustained(latch: Latch, value: float, trip: float, clear: float,
               hold: float, now: float) -> bool:
    """True on the single poll where `value` completes `hold` seconds at or above
    `trip`; False every other poll. Two guards a bare `value >= trip` lacks:

    - hold: a CPU boost burst crosses the trip point for a fraction of a second
      on an otherwise idle machine, so firing on one sample means alerting on
      noise. The value must stay over it continuously — a single reading below
      re-arms the wait from zero.
    - hysteresis (clear < trip): once tripped, the alert stays latched until the
      value falls below `clear`, so a value hovering on the trip point can't
      rattle the notification off and on. Pass clear == trip for none.
    """
    if latch.active:
        if value < clear:
            latch.active = False
            latch.since  = None
        return False
    if value < trip:
        latch.since = None
        return False
    if latch.since is None:
        latch.since = now
    if now - latch.since < hold:
        return False
    latch.active = True
    return True


# ── Main check ────────────────────────────────────────────────────────────────

def check_and_notify(r: Readings, cfg: Config, state: NotifState, hw: HardwareInfo) -> NotifState:
    """Edge-detect threshold crossings and send notifications. Returns updated state."""
    c  = cfg.notifications
    n  = cfg.notify_thresholds
    lb = cfg.labels
    nl = lb.get("notify", {})  # notification-only wording, see lang/<language>.toml [notify]
    now = time.monotonic()     # once per pass, so every latch below times off the same instant

    # Every temperature shares one debounce (a spike is a spike whatever the
    # chip), so they read the same two knobs rather than one pair each.
    hold = n.temp_sustain_seconds
    cool = n.temp_hysteresis

    # CPU temp
    if c.cpu_temp and r.cpu_temp is not None:
        if _sustained(state.cpu_temp, r.cpu_temp, n.cpu_temp, n.cpu_temp - cool, hold, now):
            _send("PiroStats", f"{lb.get('cpu_temp', 'Cpu temp')} {r.cpu_temp}{TEMP_SCALE}")

    # GPU temp
    if c.gpu_nvidia_temp and r.gpu_temp is not None:
        if _sustained(state.gpu_nvidia_temp, r.gpu_temp, n.gpu_nvidia_temp,
                      n.gpu_nvidia_temp - cool, hold, now):
            _send("PiroStats", f"{lb.get('gpu_nvidia_temp', 'Gpu temp')} {r.gpu_temp}{TEMP_SCALE}")

    if c.gpu_amd_temp and r.gpu_amd_temp is not None:
        if _sustained(state.gpu_amd_temp, r.gpu_amd_temp, n.gpu_amd_temp,
                      n.gpu_amd_temp - cool, hold, now):
            _send("PiroStats", f"{lb.get('gpu_amd_temp', 'Radeon temp')} {r.gpu_amd_temp}{TEMP_SCALE}")

    # Disk usage
    if c.disk_usage:
        for mount, du in r.disk_usage.items():
            if du is None or du.percent is None:
                continue
            over = du.percent >= n.disk_usage
            was  = state.disk.get(mount, False)
            if over and not was:
                _send("PiroStats", f"{nl.get('disk_usage', 'Disk')} {mount} {du.percent}%")
            state.disk[mount] = over

    # Disk SMART health (binary, edge-triggered when it turns bad)
    if c.disk_smart:
        for label, healthy in r.disk_smart.items():
            if healthy is None:
                continue
            bad = not healthy
            was = state.disk_smart.get(label, False)
            if bad and not was:
                _send("PiroStats",
                      f"{nl.get('disk_smart', 'Disk')} {label} {nl.get('smart_fail', 'SMART check FAILED')}",
                      icon="dialog-error")
            state.disk_smart[label] = bad

    # HD temp
    if c.hd_temp:
        for label, temp in r.hd_temps.items():
            if temp is None:
                continue
            latch = state.hd_temp.setdefault(label, Latch())
            if _sustained(latch, temp, n.hd_temp, n.hd_temp - cool, hold, now):
                _send("PiroStats", f"{lb.get('hd_temp', 'Disk')} {label} temp {temp}{TEMP_SCALE}",
                      icon="dialog-warning")

    # System battery (notify when *below* threshold, ignore while charging)
    if c.battery_sys:
        for bat in r.battery_sys:
            if not bat.perc:
                continue
            pv   = int(bat.perc.rstrip("%"))
            over = bat.state != "charging" and 0 < pv <= n.battery_sys
            was  = state.battery_sys.get(bat.id, False)
            if over and not was:
                _send("PiroStats", f"{lb.get('battery_sys', 'Battery')} {bat.perc}", icon="battery-caution")
            state.battery_sys[bat.id] = over

    # Peripheral batteries (notify when *at or below* threshold, like battery_sys
    # above: the colour turns red at <= its threshold, so a strict < here left the
    # threshold reading itself red with no alert behind it)
    if c.battery_mouse and r.battery_mouse and r.battery_mouse.perc:
        pv   = int(r.battery_mouse.perc.rstrip("%"))
        over = 0 < pv <= n.battery_mouse   # ignore 0%: device disconnected
        if over and not state.battery_mouse:
            name = r.battery_mouse.name or lb.get("battery_mouse", "Mouse")
            _send("PiroStats", f"{name}: {r.battery_mouse.perc}", icon="battery-caution")
        state.battery_mouse = over

    if c.battery_kbd and r.battery_kbd and r.battery_kbd.perc:
        pv   = int(r.battery_kbd.perc.rstrip("%"))
        over = 0 < pv <= n.battery_kbd    # ignore 0%: device disconnected
        if over and not state.battery_kbd:
            name = r.battery_kbd.name or lb.get("battery_kbd", "Keyboard")
            _send("PiroStats", f"{name}: {r.battery_kbd.perc}", icon="battery-caution")
        state.battery_kbd = over

    # Load avg 15min: the same latch as the temperatures, with no hysteresis and a
    # far longer hold — a load spike is normal, a sustained one is the signal. The
    # threshold is a fraction of cores (v / nproc), so it holds on any machine.
    if c.load_avg and r.load_avg is not None:
        fifteen = r.load_avg[2]
        ratio   = fifteen / hw.cpu_count
        if _sustained(state.load_avg, ratio, n.load_avg_15, n.load_avg_15,
                      n.load_avg_minutes * 60, now):
            _send("PiroStats",
                  f"{lb.get('load_avg', 'Load avg')} 15m {nl.get('load_high_for', 'high for')} "
                  f"{n.load_avg_minutes} min ({fifteen:.2f})",
                  icon="dialog-warning")

    # Server check
    if c.server_check and r.server_ok is not None:
        down = not r.server_ok
        if down and not state.server:
            _send("PiroStats", nl.get("server_down", "Server is not reachable!"), icon="dialog-error")
        state.server = down

    return state
