"""
Hardware discovery (one-time at startup) and sensor reading (every poll).

HardwareInfo  — resolved paths/IDs, never changes between polls
Readings      — snapshot of all sensor values, produced fresh each poll
DaemonState   — mutable inter-poll state (CPU diff, net diff, battery caches)
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import psutil

from config import BRAILLE_LENGTH_MULTIPLIER, Config, SensorOverrides
from registry import needed_capabilities

try:
    from bolt_battery import query as _bolt_query
    _BOLT_AVAILABLE = True
except (ImportError, OSError):
    _BOLT_AVAILABLE = False

try:
    import pynvml   # python-nvidia-ml-py (repo extra), not the AUR pynvml
    _PYNVML_AVAILABLE = True
except Exception:
    _PYNVML_AVAILABLE = False


# ── UPower via GDBus (Gio) ──────────────────────────────────────────────────────
# Replaces the subprocess `upower -e`/`upower -i` calls: a GDBus property read is
# a lightweight call on the system bus, no fork (the old `upower -i` cost ~5-10ms
# per peripheral, every 30s). gi (python-gobject) is already a dependency — the
# notifier uses it for libnotify. If gi, the system bus, or UPower itself is
# unreachable, every helper degrades to None/[] exactly like the old subprocess
# error paths did, so a machine without a working bus simply shows no batteries.

_UPOWER_NAME      = "org.freedesktop.UPower"
_UPOWER_PATH      = "/org/freedesktop/UPower"
_UPOWER_IFACE     = "org.freedesktop.UPower"
_UPOWER_DEV_IFACE = "org.freedesktop.UPower.Device"

# UPower device Type enum (subset we care about)
_UPOWER_TYPE_MOUSE    = 5
_UPOWER_TYPE_KEYBOARD = 6
# UPower device State enum → the string states the rest of the code already uses
_UPOWER_STATE_MAP = {1: "charging", 2: "discharging", 4: "fully-charged"}

try:
    from gi.repository import Gio, GLib
    _GIO_AVAILABLE = True
except Exception:
    _GIO_AVAILABLE = False

_system_bus = None


def _bus():
    """Lazily-opened, cached system bus connection, or None if unavailable."""
    global _system_bus
    if not _GIO_AVAILABLE:
        return None
    if _system_bus is None:
        try:
            _system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except Exception:
            return None
    return _system_bus


def _upower_enumerate() -> list[str]:
    """UPower device object paths (replaces `upower -e`)."""
    bus = _bus()
    if bus is None:
        return []
    try:
        proxy = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.NONE, None,
            _UPOWER_NAME, _UPOWER_PATH, _UPOWER_IFACE, None)
        res = proxy.call_sync("EnumerateDevices", None, Gio.DBusCallFlags.NONE, -1, None)
        return list(res.unpack()[0])
    except Exception:
        return []


def _upower_device_props(path: str, keys: tuple[str, ...]) -> Optional[dict]:
    """Read the given UPower.Device properties for one object path (replaces
    parsing `upower -i <path>`). A fresh proxy is created per call so the cached
    properties are a current snapshot; returns None if the device is unreachable."""
    bus = _bus()
    if bus is None:
        return None
    try:
        proxy = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.NONE, None,
            _UPOWER_NAME, path, _UPOWER_DEV_IFACE, None)
        out: dict = {}
        for k in keys:
            v = proxy.get_cached_property(k)
            out[k] = v.unpack() if v is not None else None
        return out
    except Exception:
        return None


# ── Timing helper (used by the profiling subcommand) ──────────────────────────────────────────

@contextmanager
def timed_section(timings: Optional[dict[str, float]], key: str):
    """No-op when timings is None, otherwise records elapsed ms under key."""
    if timings is None:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        timings[key] = timings.get(key, 0.0) + (time.perf_counter() - start) * 1000


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class BatterySys:
    id: str
    perc: str = ""          # e.g. "85%"
    rate: int = 0           # watts (rounded)
    state: str = ""         # "charging" | "discharging" | "fully-charged"
    limit: Optional[int] = None  # charge_control_end_threshold, e.g. 80


@dataclass
class BatteryPeriph:
    name: str               # device model name
    perc: str = ""          # e.g. "90%"


@dataclass
class DiskUsage:
    percent: Optional[int] = None
    used_gb: Optional[int] = None
    total_gb: Optional[int] = None


@dataclass
class HardwareInfo:
    # ── Static (discovered once at startup, never change) ──────────────────
    cpu_temp_path: Optional[Path]
    cpu_freq_path: Optional[Path]       # cpu0 scaling_cur_freq, avoids psutil per-core overhead
    hd_temp_paths: dict[str, Path]      # device label → /sys path
    fan_paths: dict[str, Path]          # "1".."4" → /sys path
    battery_sys_ids: list[str]          # UPower paths for BAT*
    has_nvidia: bool
    intel_gpu_freq_path: Optional[Path]  # /sys/class/drm/cardN/gt_act_freq_mhz
    intel_gpu_pci: Optional[str]         # PCI address, matches fdinfo's drm-pdev
    net_device: Optional[str]           # e.g. "wlan0"
    disk_io_device: Optional[str]       # physical disk hosting "/", e.g. "nvme0n1" (all its partitions, not just root)
    cpu_count: int                      # to normalize load_avg (per-core thresholds)
    # Presence flags for the formatter's hardware gate (see formatter._available):
    # an item whose hardware isn't here produces no row at all, instead of a "--"
    # placeholder. cpu_turbo_supported = intel_pstate/no_turbo or cpufreq/boost
    # exists; has_backlight = a usable /sys/class/backlight device; has_wifi = at
    # least one wireless interface (regardless of which is the active route).
    cpu_turbo_supported: bool           # turbo/boost knob exists in sysfs
    has_backlight: bool                 # a backlight device exists
    has_wifi: bool                      # a wireless interface exists

    # AMD needs no library or fork (unlike Nvidia): every reading is a sysfs file,
    # resolved once here. Each is independently optional and gates its own metric.
    amd_gpu_busy_path: Optional[Path] = None        # device/gpu_busy_percent
    amd_gpu_vram_used_path: Optional[Path] = None   # device/mem_info_vram_used
    amd_gpu_vram_total_path: Optional[Path] = None  # device/mem_info_vram_total
    amd_gpu_temp_path: Optional[Path] = None        # hwmon tempN_input (edge preferred)
    amd_gpu_fan_path: Optional[Path] = None         # hwmon fan1_input (RPM)
    amd_gpu_freq_path: Optional[Path] = None        # hwmon freq1_input (sclk, Hz)

    # ── Dynamic (retried every 60s if None) ────────────────────────────────
    battery_mouse_id: Optional[str] = None   # UPower path
    battery_kbd_id: Optional[str] = None
    # Disk block device label (e.g. "nvme0n1", "sda") → (UDisks2 drive object
    # path, "ata"|"nvme", rotational), discovered via _detect_disks()
    # independently of hd_temp_paths. `rotational` (a spinning HDD) selects the
    # longer SMART TTL — its SmartUpdate is slow and wakes the disk. Drives with
    # neither SMART interface (SD readers, some USB enclosures) are absent from
    # this dict — no SMART support, disk_smart silently stays None for that disk.
    disk_smart_drives: dict[str, tuple[str, str, bool]] = field(default_factory=dict)
    # -inf, not 0.0: time.monotonic() is seconds-since-boot on Linux, so a 0.0
    # sentinel would make the TTL gate below wait until system uptime reaches
    # the TTL instead of firing on the very first check after daemon startup.
    periph_scan_ts: float = float("-inf")    # time.monotonic() of last scan


@dataclass
class _BatterySysCache:
    perc: str = ""
    rate: int = 0
    state: str = ""
    limit: Optional[int] = None
    ts: float = float("-inf")

@dataclass
class _BatteryPeriphCache:
    name: str = ""
    perc: str = ""
    ts: float = float("-inf")
    path: str = ""       # UPower path these readings came from (unused by Bolt)
    gone: bool = False   # last read found nothing there: the path needs rediscovering

@dataclass
class _NetInfoCache:
    device: str = ""
    ip: str = ""
    ssid: str = ""
    signal_pct: Optional[int] = None
    ts: float = float("-inf")


@dataclass
class _RateState:
    """Prev sample for a bytes/s rate computed from two cumulative counters
    (net rx/tx, disk read/write). See _counter_rate."""
    prev_a: int = 0
    prev_b: int = 0
    ts: float = 0.0


@dataclass
class DaemonState:
    """Mutable state that persists between polls."""
    # CPU usage diff
    cpu_prev_times: list[int] = field(default_factory=list)
    cpu_history: list[int] = field(default_factory=list)
    cpu_history_sample_ts: float = float("-inf")
    mem_history: list[int] = field(default_factory=list)
    mem_history_sample_ts: float = float("-inf")

    # GPU usage + decoder history (graphs page): the active vendor's values,
    # sampled only while the page is on (see _sample_gpu_history).
    gpu_usage_history: list[int] = field(default_factory=list)
    gpu_dec_history: list[int] = field(default_factory=list)
    gpu_history_sample_ts: float = float("-inf")

    # Network up/down byte-rate history (graphs page), sampled only while the
    # page is on (see _sample_net_history).
    net_up_history: list[int] = field(default_factory=list)
    net_down_history: list[int] = field(default_factory=list)
    net_history_sample_ts: float = float("-inf")

    # Per-core CPU (cpu_cores tooltip page): prev jiffies + a history buffer per
    # core, sized to the tooltip braille length. Sampled only when the page is on.
    cpu_core_prev_times: list[list[int]] = field(default_factory=list)
    cpu_core_history: list[list[int]] = field(default_factory=list)
    cpu_core_history_sample_ts: float = float("-inf")

    # Per-process CPU diff (top_process), keyed by pid → utime+stime jiffies.
    # The panel (Top 1/2/3) samples on a 15s TTL; the tooltip page samples every
    # poll off its OWN prev-state (page_proc_*) so the two cadences don't collide.
    proc_prev_times: dict[int, int] = field(default_factory=dict)
    proc_prev_ts: float = 0.0
    top_process_cache: Optional[list[tuple[int, str, int, float]]] = None
    top_process_cache_ts: float = float("-inf")  # see periph_scan_ts comment above
    page_proc_prev_times: dict[int, int] = field(default_factory=dict)
    page_proc_prev_ts: float = 0.0

    # Intel iGPU engine-busy diff (keyed by drm-client-id → {engine: ns})
    intel_gpu_engine_prev: dict[int, dict[str, int]] = field(default_factory=dict)
    intel_gpu_prev_ts: float = 0.0
    intel_gpu_usage_cache: dict[str, int] = field(default_factory=dict)
    intel_gpu_usage_cache_ts: float = float("-inf")

    # Network / disk I/O rate diff (bytes/s from cumulative counters)
    net_rate: _RateState = field(default_factory=_RateState)    # a=tx, b=rx
    disk_rate: _RateState = field(default_factory=_RateState)   # a=read, b=write

    # Battery caches (keyed by UPower path)
    battery_sys_cache: dict[str, _BatterySysCache] = field(default_factory=dict)
    battery_mouse_cache: _BatteryPeriphCache = field(default_factory=_BatteryPeriphCache)
    battery_kbd_cache: _BatteryPeriphCache = field(default_factory=_BatteryPeriphCache)
    net_info_cache: _NetInfoCache = field(default_factory=_NetInfoCache)

    # HD temp cache (keyed by device label, e.g. "nvme0"): controller-side
    # latency on the hwmon read, not a software cost — TTL smooths it out.
    hd_temp_cache: dict[str, tuple[Optional[int], float]] = field(default_factory=dict)
    # hd_temp label → (healthy, ts). Same TTL-cache shape as hd_temp_cache, but
    # the TTL is hours not seconds (see DiskConfig.smart_interval).
    disk_smart_cache: dict[str, tuple[Optional[bool], float]] = field(default_factory=dict)

    # Fan speed cache (keyed by fan index, e.g. "1"): same controller-side
    # latency pattern as hd_temp (EC/Super I/O), RPM doesn't need 1.5s granularity.
    fan_speed_cache: dict[str, tuple[Optional[int], float]] = field(default_factory=dict)

    # GPU cache
    gpu_cache: tuple = ()      # (temp, usage, mem, fan, dec)
    gpu_cache_ts: float = 0.0


@dataclass
class Readings:
    cpu_usage: Optional[int] = None
    cpu_temp: Optional[int] = None
    cpu_freq: Optional[float] = None  # MHz, raw
    cpu_turbo: Optional[bool] = None
    cpu_history: list[int] = field(default_factory=list)
    mem_history: list[int] = field(default_factory=list)
    uptime: Optional[int] = None  # seconds
    load_avg: Optional[tuple[float, float, float]] = None
    top_process: Optional[list[tuple[str, int]]] = None          # panel Top 1/2/3: (comm, cpu%)
    top_process_full: Optional[list[tuple[int, str, int, float]]] = None  # page: (pid, comm, cpu%, mem%)
    cpu_core_usage: Optional[list[int]] = None                    # cpu_cores page: current % per core
    cpu_core_history: Optional[list[list[int]]] = None            # cpu_cores page: braille history per core

    mem_usage: Optional[int] = None
    mem_used_gb: Optional[int] = None    # tooltip mem_usage:value GB column
    mem_total_gb: Optional[int] = None
    swap_usage: Optional[int] = None

    net_up_bps: Optional[int] = None
    net_down_bps: Optional[int] = None

    net_device: Optional[str] = None     # e.g. "wlan0", live-detected (handles interface switches)
    ip_address: Optional[str] = None
    wifi_ssid: Optional[str] = None
    wifi_signal: Optional[int] = None    # %, converted from dBm (see _read_net_info)

    disk_read_bps: Optional[int] = None
    disk_write_bps: Optional[int] = None

    disk_usage: dict[str, Optional[DiskUsage]] = field(default_factory=dict)
    # keyed by hd_temp label (e.g. "nvme0", "sda"), not by mount: SMART is a
    # property of the physical disk, paired with hd_temp instead of disk_usage.
    disk_smart: dict[str, Optional[bool]] = field(default_factory=dict)  # True=healthy, False=failing, None=unknown
    hd_temps: dict[str, Optional[int]] = field(default_factory=dict)
    fan_speeds: dict[str, Optional[int]] = field(default_factory=dict)

    battery_sys: list[BatterySys] = field(default_factory=list)
    battery_mouse: Optional[BatteryPeriph] = None
    battery_kbd: Optional[BatteryPeriph] = None

    gpu_temp: Optional[int] = None
    gpu_usage: Optional[int] = None
    gpu_mem: Optional[int] = None
    gpu_dec: Optional[int] = None
    gpu_fan: Optional[int] = None

    gpu_intel_freq: Optional[int] = None
    gpu_intel_usage: Optional[int] = None
    gpu_intel_dec_usage: Optional[int] = None

    gpu_amd_usage: Optional[int] = None
    gpu_amd_mem_usage: Optional[int] = None   # VRAM occupancy %, not mem_busy
    gpu_amd_temp: Optional[int] = None
    gpu_amd_fan_speed: Optional[int] = None   # RPM (amdgpu reports RPM, not %)
    gpu_amd_freq: Optional[int] = None        # MHz

    # graphs page: the active GPU's usage + decoder history (vendor-resolved)
    gpu_usage_history: list[int] = field(default_factory=list)
    gpu_dec_history: list[int] = field(default_factory=list)
    # graphs page: network up/down byte-rate history
    net_up_history: list[int] = field(default_factory=list)
    net_down_history: list[int] = field(default_factory=list)

    screen_brightness: Optional[int] = None
    system_updates: Optional[int] = None
    server_ok: Optional[bool] = None


# ── Hardware discovery ────────────────────────────────────────────────────────

def discover_hardware(cfg: Config) -> HardwareInfo:
    """Called once at daemon startup. Discovers all static hardware paths."""
    return HardwareInfo(
        cpu_temp_path   = _find_cpu_temp(cfg.sensors),
        cpu_freq_path   = _find_cpu_freq_path(),
        hd_temp_paths   = _find_hd_temps(cfg.sensors),
        fan_paths       = _find_fans(cfg.sensors),
        battery_sys_ids = _find_battery_sys(),
        has_nvidia      = _detect_nvidia(),
        **_detect_intel_gpu(),
        **_detect_amd_gpu(),
        net_device      = _detect_net_device(),
        disk_io_device  = _detect_disk_io_device(),
        cpu_count       = os.cpu_count() or 1,
        cpu_turbo_supported = _detect_cpu_turbo_supported(),
        has_backlight   = _detect_has_backlight(),
        has_wifi        = _detect_has_wifi(),
        # Disk identity (for disk_smart) is discovered independently of
        # hd_temp_paths: UDisks2 sees every disk with an ATA/NVMe SMART
        # interface, even ones hwmon exposes no temperature sensor for.
        disk_smart_drives = _detect_disks() if cfg.disks.smart else {},
        **_find_peripherals(cfg),
    )


def rescan_peripherals(hw: HardwareInfo, cfg: Config) -> HardwareInfo:
    """Retry discovery of hardware that can appear after startup: UPower
    peripherals (mouse/keyboard) and the default-route net device — the latter
    is None when the daemon starts before the network is up. Called at most
    every 60s while something wanted is still missing."""
    found = _find_peripherals(cfg)
    hw.battery_mouse_id = found.get("battery_mouse_id") or hw.battery_mouse_id
    hw.battery_kbd_id   = found.get("battery_kbd_id")   or hw.battery_kbd_id
    if hw.net_device is None:
        hw.net_device = _detect_net_device()
    hw.periph_scan_ts   = time.monotonic()
    return hw


# Items whose gate (_g_net) depends on hw.net_device: as long as one of these
# is configured and the device is None, it's worth retrying discovery.
_NET_GATED_ITEMS = ("net_speed", "net_device_ip", "net_ip", "net_device")


def needs_periph_rescan(hw: HardwareInfo, cfg: Config, state: DaemonState) -> bool:
    # Bolt devices are configured statically — no UPower discovery needed.
    wants_mouse = cfg.panel.has("battery_mouse") or cfg.tooltip.has("battery_mouse")
    wants_kbd   = cfg.panel.has("battery_kbd")   or cfg.tooltip.has("battery_kbd")
    # An id we hold can also be stale, not just missing: UPower drops a
    # peripheral's object path when its receiver is re-enumerated (suspend,
    # replug) and numbers the replacement one higher, so the path resolved at
    # startup answers nothing until it is rediscovered. `gone` is that signal —
    # a device merely switched off keeps answering and doesn't set it.
    if wants_mouse and cfg.battery.mouse_bolt is None and (
            hw.battery_mouse_id is None or state.battery_mouse_cache.gone):
        return True
    if wants_kbd and cfg.battery.kbd_bolt is None and (
            hw.battery_kbd_id is None or state.battery_kbd_cache.gone):
        return True
    # net_device is detected once at startup: if the daemon started before the
    # network was up, it stays None. Retries as long as an item needs it.
    if hw.net_device is None and any(
            cfg.panel.has(n) or cfg.tooltip.has(n) for n in _NET_GATED_ITEMS):
        return True
    return False


# ── Main collect function ─────────────────────────────────────────────────────

def collect(
    state: DaemonState,
    hw: HardwareInfo,
    cfg: Config,
    timings: Optional[dict[str, float]] = None,
    skip_slow: bool = False,
) -> Readings:
    """Produce a fresh Readings snapshot. Updates state in-place for diff sensors.

    When `timings` is passed, records elapsed ms per section/item (used by the profiling subcommand).

    With `skip_slow=True` the sensors whose first (cache-cold) read blocks for a
    long time — disk SMART (ATA SmartUpdate ioctl), Bolt HID batteries, nvidia-smi,
    the /proc top-process scan, and the Intel GPU fdinfo scan — are skipped
    entirely. (system_updates/server_check are now plain file reads, fast, not
    gated here.) Used for the very first
    write at startup so the panel paints immediately instead of staying blank for
    ~1-2s; they fill in on the next (normal) poll. The fast sysfs/psutil sensors
    are always read.
    """
    r = Readings()
    # Demand-driven: a sensor is read only if a capability some configured item
    # (or an enabled notification) needs requires it — see items.needed_
    # capabilities. cpu_usage/mem_usage stay outside the gating (always read:
    # they feed the sparks' history buffers and the baseline).
    caps = needed_capabilities(cfg)

    with timed_section(timings, "cpu_usage"):
        r.cpu_usage   = _read_cpu_usage(state, cfg)
    r.cpu_history = list(state.cpu_history)
    if "cpu_temp" in caps:
        with timed_section(timings, "cpu_temp"):
            r.cpu_temp    = _read_path_millideg(hw.cpu_temp_path)
    if "cpu_freq" in caps:
        with timed_section(timings, "cpu_freq"):
            r.cpu_freq   = _read_cpu_freq(hw.cpu_freq_path)
    if "cpu_turbo" in caps:
        with timed_section(timings, "cpu_turbo"):
            r.cpu_turbo   = _read_cpu_turbo()

    if "uptime" in caps:
        with timed_section(timings, "uptime"):
            r.uptime = _read_uptime()

    if "load_avg" in caps:
        with timed_section(timings, "load_avg"):
            r.load_avg = _read_load_avg()

    if "top_process" in caps and not skip_slow:
        with timed_section(timings, "top_process"):
            full = _read_top_process_cached(state)
            r.top_process_full = full
            r.top_process = ([(comm, pct) for _pid, comm, pct, _mem in full[:TOP_PROCESS_COUNT]]
                             if full else None)

    # Per-core CPU only when the cpu_cores tooltip page is enabled (its history
    # needs continuous sampling; gated so it costs nothing when the page is off).
    if "cpu_cores" in cfg.pages.order and not skip_slow:
        with timed_section(timings, "cpu_cores"):
            r.cpu_core_usage = _read_cpu_cores(state, cfg)
            r.cpu_core_history = state.cpu_core_history or None

    with timed_section(timings, "mem_usage"):
        r.mem_usage, r.mem_used_gb, r.mem_total_gb = _read_mem_usage(state, cfg)
    r.mem_history = list(state.mem_history)
    if "swap_usage" in caps:
        with timed_section(timings, "swap_usage"):
            r.swap_usage = _read_swap_usage()

    if "net_speed" in caps and hw.net_device:
        with timed_section(timings, "net_speed"):
            r.net_up_bps, r.net_down_bps = _read_net_speed(state, hw.net_device)
    _sample_net_history(state, cfg, r)

    # net_device/net_ip/wifi_ssid/wifi_signal/net_device_ip/wifi_ssid_signal share the
    # single net_info read (the "net_info" capability).
    if "net_info" in caps:
        with timed_section(timings, "net_info"):
            info = _read_net_info_cached(state)
            r.net_device   = info.device or None
            r.ip_address   = info.ip or None
            r.wifi_ssid    = info.ssid or None
            r.wifi_signal  = info.signal_pct
            # hw.net_device follows whichever interface is active right now: the
            # live read already knows the current route's device, so we adopt it
            # as soon as it changes (net_device_ip/net_speed's gate turns on right
            # at boot without waiting for the 60s rescan, and the speed follows a
            # wifi<->eth switch instead of staying stuck on the first interface).
            # Only set when the device is present: if the network drops we don't
            # clear it, so the row stays visible with "--" instead of flickering
            # in/out. On an interface change the counters belong to a different
            # NIC: reset the rate state so the first diff doesn't emit a spurious spike.
            if info.device and info.device != hw.net_device:
                hw.net_device = info.device
                state.net_rate = _RateState()

    if "disk_io" in caps and hw.disk_io_device:
        with timed_section(timings, "disk_io"):
            r.disk_read_bps, r.disk_write_bps = _read_disk_io(state, hw.disk_io_device)

    if "disk_usage" in caps:
        for mount in _resolve_mounts(cfg):
            with timed_section(timings, f"disk_usage[{mount}]"):
                r.disk_usage[mount] = _read_disk_usage(mount)

    if "disk_smart" in caps and cfg.disks.smart and not skip_slow:
        for label, (drive_path, kind, rotational) in hw.disk_smart_drives.items():
            interval = cfg.disks.smart_interval_hdd if rotational else cfg.disks.smart_interval
            with timed_section(timings, f"disk_smart[{label}]"):
                r.disk_smart[label] = _read_disk_smart_cached(
                    state, label, drive_path, kind, interval)

    if "hd_temp" in caps:
        for label, path in hw.hd_temp_paths.items():
            with timed_section(timings, f"hd_temp[{label}]"):
                r.hd_temps[label] = _read_hd_temp_cached(state, label, path)

    if "fan_speed" in caps:
        for label, path in hw.fan_paths.items():
            with timed_section(timings, f"fan_speed[{label}]"):
                r.fan_speeds[label] = _read_fan_speed_cached(state, label, path)

    if "battery_sys" in caps:
        with timed_section(timings, "battery_sys"):
            r.battery_sys = _read_battery_sys(state, hw, cfg)

    if "battery_mouse" in caps:
        if hw.battery_mouse_id:
            with timed_section(timings, "battery_mouse"):
                r.battery_mouse = _read_battery_periph(
                    state.battery_mouse_cache, hw.battery_mouse_id, cfg.battery.mouse_name)
        elif cfg.battery.mouse_bolt is not None and not skip_slow:
            with timed_section(timings, "battery_mouse"):
                r.battery_mouse = _read_battery_bolt(
                    state.battery_mouse_cache, cfg.battery.mouse_bolt, cfg.battery.mouse_name)

    if "battery_kbd" in caps:
        if hw.battery_kbd_id:
            with timed_section(timings, "battery_kbd"):
                r.battery_kbd = _read_battery_periph(
                    state.battery_kbd_cache, hw.battery_kbd_id, cfg.battery.kbd_name)
        elif cfg.battery.kbd_bolt is not None and not skip_slow:
            with timed_section(timings, "battery_kbd"):
                r.battery_kbd = _read_battery_bolt(
                    state.battery_kbd_cache, cfg.battery.kbd_bolt, cfg.battery.kbd_name)

    if "gpu_nvidia" in caps and hw.has_nvidia and not skip_slow:
        with timed_section(timings, "gpu_nvidia"):
            r.gpu_temp, r.gpu_usage, r.gpu_mem, r.gpu_dec, r.gpu_fan = _read_nvidia(state)

    if "gpu_intel_freq" in caps and hw.intel_gpu_freq_path:
        with timed_section(timings, "gpu_intel_freq"):
            r.gpu_intel_freq = _read_path_int(hw.intel_gpu_freq_path)

    wants_intel_usage = "gpu_intel_usage" in caps
    wants_intel_dec   = "gpu_intel_dec" in caps
    if hw.intel_gpu_pci and (wants_intel_usage or wants_intel_dec) and not skip_slow:
        with timed_section(timings, "gpu_intel_usage"):
            metrics = _read_intel_gpu_metrics_cached(state, hw.intel_gpu_pci)
        if wants_intel_usage:
            r.gpu_intel_usage = metrics.get("render")
        if wants_intel_dec:
            r.gpu_intel_dec_usage = metrics.get("video")

    # No TTL cache and no skip_slow guard: the whole block is a few small reads.
    if "gpu_amd" in caps:
        with timed_section(timings, "gpu_amd"):
            r.gpu_amd_usage     = _pct_cap(_read_path_int(hw.amd_gpu_busy_path))
            r.gpu_amd_temp      = _read_path_millideg(hw.amd_gpu_temp_path)
            r.gpu_amd_fan_speed = _read_path_int(hw.amd_gpu_fan_path)
            r.gpu_amd_mem_usage = _read_amd_vram_percent(hw)
            # amdgpu reports sclk in Hz; the freq cell renders MHz like cpu_freq.
            # `is not None`, not truthiness: an idle card really does report 0.
            freq = _read_path_int(hw.amd_gpu_freq_path)
            r.gpu_amd_freq = freq // 1_000_000 if freq is not None else None

    _sample_gpu_history(state, cfg, hw, r)

    if "screen_brightness" in caps:
        with timed_section(timings, "screen_brightness"):
            r.screen_brightness = _read_brightness()

    # Both read a file written by an external checker (see config) — plain file
    # reads, no subprocess, so no TTL cache or skip_slow gating is needed: they
    # can't block the poll loop the way the old ping/pacman subprocesses did.
    if "system_updates" in caps and cfg.system_updates.file:
        with timed_section(timings, "system_updates"):
            r.system_updates = _read_count_file(cfg.system_updates.file)

    if "server_check" in caps and cfg.server_check.file:
        with timed_section(timings, "server_check"):
            r.server_ok = _read_server_file(cfg.server_check.file)

    return r


# ── hwmon helpers ─────────────────────────────────────────────────────────────

def _cached_by_label(cache: dict, label: str, ttl: float, read_fn: Callable[[], object]):
    """TTL cache keyed by label, shared by the per-device sysfs reads whose cost
    is controller-side latency rather than CPU (hd_temp, fan_speed, disk_smart).
    Holds (value, ts) and re-reads via read_fn only once `ttl` seconds passed.
    The -inf default ts forces a read on first access: time.monotonic() is
    seconds-since-boot, so a 0.0 default would delay it until uptime >= ttl."""
    value, ts = cache.get(label, (None, float("-inf")))
    if time.monotonic() - ts >= ttl:
        value = read_fn()
        cache[label] = (value, time.monotonic())
    return value


HD_TEMP_CACHE_TTL = 30.0   # seconds — same TTL as batteries, to limit APST wakeups


def _read_hd_temp_cached(state: DaemonState, label: str, path: Path) -> Optional[int]:
    """Cache hd_temp: the hwmon read itself costs 5-15ms (NVMe controller
    latency, e.g. APST power-state wakeup, not a software cost), and the
    temperature doesn't move meaningfully within a few seconds."""
    return _cached_by_label(state.hd_temp_cache, label, HD_TEMP_CACHE_TTL,
                            lambda: _read_path_millideg(path))


FAN_CACHE_TTL = 30.0   # seconds — same pattern as hd_temp: the EC/Super I/O read costs as much as the NVMe one


def _read_fan_speed_cached(state: DaemonState, label: str, path: Path) -> Optional[int]:
    """Cache fan_speed: fan RPM doesn't change meaningfully within a few
    seconds, and the sysfs read has the same cost order as the NVMe wakeup."""
    return _cached_by_label(state.fan_speed_cache, label, FAN_CACHE_TTL,
                            lambda: _read_path_int(path))


def _hwmon_find(chip_substr: str) -> list[Path]:
    """Return all hwmon dirs whose 'name' file contains chip_substr."""
    found = []
    for p in sorted(Path("/sys/class/hwmon").iterdir()):
        name_file = p / "name"
        if not name_file.exists():
            continue
        try:
            name = name_file.read_text().strip().lower()
        except OSError:
            continue
        if chip_substr.lower() in name:
            found.append(p)
    return found


def _resolve_sensor(spec: str) -> Optional[Path]:
    """Resolve 'chip|file' spec to a /sys hwmon path."""
    chip, filename = spec.split("|", 1)
    for hwmon in _hwmon_find(chip):
        p = hwmon / filename
        if p.exists():
            return p
    return None


def _read_path_millideg(path: Optional[Path]) -> Optional[int]:
    """Read a hwmon millidegree file, return integer °C."""
    if path is None:
        return None
    try:
        return int(path.read_text()) // 1000
    except (OSError, ValueError):
        return None


def _read_path_int(path: Optional[Path]) -> Optional[int]:
    if path is None:
        return None
    try:
        return int(path.read_text())
    except (OSError, ValueError):
        return None


# ── Discovery internals ───────────────────────────────────────────────────────

def _find_cpu_temp(ovr: SensorOverrides) -> Optional[Path]:
    if ovr.cpu_temp:
        return _resolve_sensor(ovr.cpu_temp)
    for chip in ("coretemp", "k10temp", "zenpower"):
        for hwmon in _hwmon_find(chip):
            p = hwmon / "temp1_input"
            if p.exists():
                return p
    return None


def _find_cpu_freq_path() -> Optional[Path]:
    """cpu0's scaling_cur_freq: one file instead of the one-per-core scan psutil.cpu_freq() does."""
    p = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    return p if p.exists() else None


def _find_hd_temps(ovr: SensorOverrides) -> dict[str, Path]:
    result: dict[str, Path] = {}

    # Manual overrides take full precedence
    for i in range(1, 5):
        spec = getattr(ovr, f"hd{i}_temp", None)
        if not spec:
            continue
        p = _resolve_sensor(spec)
        if p:
            label = _hwmon_device_label(p.parent)
            result[label] = p

    if result:
        return result

    # Autodetect nvme + drivetemp
    for chip in ("nvme", "drivetemp"):
        for hwmon in _hwmon_find(chip):
            p = hwmon / "temp1_input"
            if p.exists():
                label = _hwmon_device_label(hwmon)
                result[label] = p
    return result


def _resolve_nvme_namespace(ctrl: str) -> str:
    """'nvme0' (controller) -> 'nvme0n1' (its first namespace block device) —
    falls back to the controller name if no namespace is found. This is the
    same label _detect_disks() derives from UDisks2 (which sees the block
    device, not the controller), so hd_temp and disk_smart key off the same
    string for the same physical disk."""
    path = Path("/sys/class/nvme") / ctrl
    try:
        namespaces = sorted(p.name for p in path.iterdir() if p.name.startswith(f"{ctrl}n"))
    except OSError:
        return ctrl
    return namespaces[0] if namespaces else ctrl


def _hwmon_device_label(hwmon: Path) -> str:
    """Produce a short device label from the hwmon sysfs path (e.g. 'nvme0n1', 'sda')."""
    real = hwmon.resolve()
    real_str = str(real)

    # nvme: part of path is 'nvme0' etc. — resolved to its namespace block device.
    for part in reversed(real.parts):
        if part.startswith("nvme"):
            return _resolve_nvme_namespace(part)
        if part.startswith("sd") or part.startswith("hd"):
            return part

    # SATA/drivetemp: find SCSI address (H:B:T:L) and map to block device
    m = re.search(r'(\d+:\d+:\d+:\d+)', real_str)
    if m:
        scsi = m.group(1)
        for blk in sorted(Path("/sys/class/block").iterdir()):
            try:
                if scsi in str(blk.resolve()):
                    return blk.name
            except OSError:
                continue

    return hwmon.name


def _find_fans(ovr: SensorOverrides) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for i in range(1, 5):
        spec = getattr(ovr, f"fan{i}_speed", None)
        if not spec:
            break
        p = _resolve_sensor(spec)
        if p:
            result[str(i)] = p
    return result


_SUBPROCESS_ERRORS = (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError)


def _token_after(tokens: list[str], key: str) -> Optional[str]:
    """Token immediately following `key` in an `ip route` token list (e.g.
    the device after 'dev', the address after 'src'), or None if absent."""
    if key in tokens:
        idx = tokens.index(key)
        if idx + 1 < len(tokens):
            return tokens[idx + 1]
    return None


def _detect_net_device() -> Optional[str]:
    for args in (["ip", "route", "get", "8.8.8.8"], ["ip", "route", "show", "default"]):
        try:
            out = subprocess.check_output(args, text=True, timeout=3)
        except _SUBPROCESS_ERRORS:
            continue
        dev = _token_after(out.split(), "dev")
        if dev:
            return dev
    return None


NET_INFO_TTL = 10.0   # seconds — 'ip route get' + 'iw dev link' cost ~5ms combined,
                       # not worth running every poll for data that barely changes


def _is_wireless(device: str) -> bool:
    return Path(f"/sys/class/net/{device}/wireless").exists()


def _dbm_to_pct(dbm: int) -> int:
    """Rough dBm->quality% approximation used by several Linux wireless tools:
    -50dBm or better -> 100%, -100dBm or worse -> 0%, linear in between."""
    return max(0, min(100, 2 * (dbm + 100)))


def _read_net_info() -> tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
    """Returns (device, ip, ssid, signal_pct) for the currently active route.
    device/ip come from the same 'ip route get' call (one subprocess, not
    two); ssid/signal only get queried when that device is wireless."""
    device = ip = None
    try:
        out = subprocess.check_output(
            ["ip", "route", "get", "8.8.8.8"], text=True, timeout=3)
        tokens = out.split()
        device = _token_after(tokens, "dev")
        ip = _token_after(tokens, "src")
    except _SUBPROCESS_ERRORS:
        pass

    ssid = None
    signal_pct = None
    if device and _is_wireless(device):
        try:
            out = subprocess.check_output(["iw", "dev", device, "link"], text=True, timeout=3)
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("SSID:"):
                    ssid = line.removeprefix("SSID:").strip()
                elif line.startswith("signal:"):
                    try:
                        signal_pct = _dbm_to_pct(int(line.split()[1]))
                    except (ValueError, IndexError):
                        pass
        except _SUBPROCESS_ERRORS:
            pass

    return device, ip, ssid, signal_pct


def _read_net_info_cached(state: DaemonState) -> _NetInfoCache:
    c = state.net_info_cache
    if time.monotonic() - c.ts >= NET_INFO_TTL:
        device, ip, ssid, signal_pct = _read_net_info()
        c.device, c.ip, c.ssid, c.signal_pct = device or "", ip or "", ssid or "", signal_pct
        c.ts = time.monotonic()
    return c


def _resolve_mount_device(mount: str) -> Optional[str]:
    """Resolve a mountpoint to its device basename, e.g. '/' -> 'nvme0n1p2'."""
    try:
        for part in psutil.disk_partitions():
            if part.mountpoint == mount:
                return Path(part.device).name
    except OSError:
        pass
    return None


def _whole_disk_of(device: str) -> str:
    """Walks up from a partition to the physical disk that contains it (e.g.
    nvme0n1p5 -> nvme0n1, sda3 -> sda) by reading the kernel tree: the parent
    in the partition's real sysfs path is the disk. This way I/O counts every
    partition on the disk, not just the root one. If `device` isn't a
    partition (already a whole disk, or dm/LVM/LUKS with no single parent
    disk) it's returned unchanged — falls back to per-device behavior."""
    try:
        node = Path(f"/sys/class/block/{device}")
        if not (node / "partition").exists():
            return device
        parent = node.resolve().parent.name
        return parent or device
    except OSError:
        return device


def _detect_disk_io_device(mount: str = "/") -> Optional[str]:
    """Resolve mount '/' to the whole physical disk backing it (e.g. 'nvme0n1'),
    the key used by psutil.disk_io_counters(perdisk=True). Walking up from the
    root partition to its disk makes the I/O rate cover every partition on that
    disk, not just root (see _whole_disk_of)."""
    device = _resolve_mount_device(mount)
    return _whole_disk_of(device) if device else None


# ── UDisks2 via GDBus (Gio) ─────────────────────────────────────────────────────
# Same migration as UPower above: replaces the `busctl tree`/`introspect`/
# `get-property`/`call` subprocesses (the disk discovery alone cost ~80ms of fork
# overhead at startup, one introspect per block device). GetManagedObjects pulls
# the whole object tree in a single D-Bus call; SmartUpdate is invoked directly.
_UDISKS_NAME      = "org.freedesktop.UDisks2"
_UDISKS_PATH      = "/org/freedesktop/UDisks2"
_UDISKS_BLOCK     = "org.freedesktop.UDisks2.Block"
_UDISKS_PARTITION = "org.freedesktop.UDisks2.Partition"
_UDISKS_NVME      = "org.freedesktop.UDisks2.NVMe.Controller"
_UDISKS_ATA       = "org.freedesktop.UDisks2.Drive.Ata"


def _is_rotational(label: str) -> bool:
    """True for spinning HDDs. Reads the kernel's block queue flag rather than
    UDisks2's Rotational property, which proved unreliable (a real HDD here is
    reported as non-rotational by UDisks2 but rotational=1 by the kernel)."""
    try:
        return Path(f"/sys/block/{label}/queue/rotational").read_text().strip() == "1"
    except OSError:
        return False


def _udisks_prop(path: str, iface: str, prop: str):
    """Read one UDisks2 property fresh (Properties.Get, not a cached proxy
    snapshot — SmartUpdate's new value isn't reflected in a proxy cache without
    a running main loop). Returns None on any failure."""
    bus = _bus()
    if bus is None:
        return None
    try:
        res = bus.call_sync(
            _UDISKS_NAME, path, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (iface, prop)), None, Gio.DBusCallFlags.NONE, -1, None)
        return res.unpack()[0]
    except Exception:
        return None


def _detect_disks() -> dict[str, tuple[str, str]]:
    """Disk identity source of truth: enumerate whole-disk block devices via
    UDisks2 (skips partitions) and resolve each to its drive object and SMART
    interface kind ('ata' or 'nvme'). Labels are block device basenames (e.g.
    'nvme0n1', 'sda') — independent of mount config and of whether hwmon
    happens to expose a temperature sensor for that disk. Drives exposing
    neither SMART interface (SD card readers, some USB enclosures) are
    skipped."""
    result: dict[str, tuple[str, str]] = {}
    bus = _bus()
    if bus is None:
        return result
    try:
        proxy = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.NONE, None,
            _UDISKS_NAME, _UDISKS_PATH, "org.freedesktop.DBus.ObjectManager", None)
        objects = proxy.call_sync(
            "GetManagedObjects", None, Gio.DBusCallFlags.NONE, -1, None).unpack()[0]
    except Exception:
        return result

    for path, ifaces in objects.items():
        if "/block_devices/" not in path:
            continue
        block = ifaces.get(_UDISKS_BLOCK)
        if block is None or _UDISKS_PARTITION in ifaces:
            continue  # whole disks only, not partitions
        drive_path = block.get("Drive")
        if not drive_path or drive_path == "/":
            continue
        drive = objects.get(drive_path)
        if drive is None:
            continue
        label = path.rsplit("/", 1)[-1]
        if label.startswith("sr"):
            continue  # optical drives (sr0…): no meaningful SMART/temp to monitor
        rotational = _is_rotational(label)
        if _UDISKS_NVME in drive:
            result[label] = (drive_path, "nvme", rotational)
        elif _UDISKS_ATA in drive:
            result[label] = (drive_path, "ata", rotational)
    return result


def _read_disk_smart(drive_path: str, kind: str) -> Optional[bool]:
    """True = healthy, False = failing, None = D-Bus call failed/unsupported."""
    bus = _bus()
    if bus is None:
        return None
    iface = _UDISKS_NVME if kind == "nvme" else _UDISKS_ATA
    try:
        # SmartUpdate(options a{sv}) — empty options. This is a real ioctl on the
        # drive (slow on ATA), hence the long TTL upstream.
        bus.call_sync(
            _UDISKS_NAME, drive_path, iface, "SmartUpdate",
            GLib.Variant("(a{sv})", ({},)), None, Gio.DBusCallFlags.NONE, 15000, None)
    except Exception:
        return None

    if kind == "nvme":
        warning = _udisks_prop(drive_path, iface, "SmartCriticalWarning")
        if warning is None:
            return None
        return len(warning) == 0

    failing = _udisks_prop(drive_path, iface, "SmartFailing")
    if failing is None:
        return None
    return not failing


def _read_disk_smart_cached(
    state: DaemonState, label: str, drive_path: str, kind: str, ttl: float,
) -> Optional[bool]:
    return _cached_by_label(state.disk_smart_cache, label, ttl,
                            lambda: _read_disk_smart(drive_path, kind))


def _find_battery_sys() -> list[str]:
    return sorted(p for p in _upower_enumerate() if "/battery_BAT" in p)


def _find_peripherals(cfg: Config) -> dict:
    """Discover hidpp battery UPower paths. Returns dict with battery_mouse_id, battery_kbd_id."""
    result: dict = {"battery_mouse_id": None, "battery_kbd_id": None}

    # Manual overrides
    if cfg.battery.mouse_unifying:
        result["battery_mouse_id"] = cfg.battery.mouse_unifying
    if cfg.battery.kbd_unifying:
        result["battery_kbd_id"] = cfg.battery.kbd_unifying
    if result["battery_mouse_id"] and result["battery_kbd_id"]:
        return result

    hidpp = [p for p in _upower_enumerate() if "/battery_hidpp" in p]
    for path in hidpp:
        if result["battery_mouse_id"] and result["battery_kbd_id"]:
            break
        props = _upower_device_props(path, ("Model", "Type"))
        if props is None:
            continue
        model = (props.get("Model") or "").lower()
        dtype = props.get("Type")

        # UPower's Type is the reliable signal; the model-name heuristics stay as
        # a fallback for devices that report Type=0/Unknown.
        is_kbd = dtype == _UPOWER_TYPE_KEYBOARD or \
                 any(w in model for w in ("keyboard", "keys", "ergo")) or \
                 any(model.startswith(p) for p in ("k4", "k8", "mx keys"))
        is_mouse = dtype == _UPOWER_TYPE_MOUSE or \
                   "mouse" in model or "master" in model or \
                   "mx m" in model or "trackball" in model

        if is_kbd and not result["battery_kbd_id"]:
            result["battery_kbd_id"] = path
        elif is_mouse and not result["battery_mouse_id"]:
            result["battery_mouse_id"] = path

    return result


def _detect_cpu_turbo_supported() -> bool:
    """Mirror of _read_cpu_turbo's path probing, at the hardware-discovery level:
    the turbo/boost knob exists iff one of these sysfs files is present (VMs have
    neither, so cpu_turbo is gated off there)."""
    return (Path("/sys/devices/system/cpu/intel_pstate/no_turbo").exists()
            or Path("/sys/devices/system/cpu/cpufreq/boost").exists())


def _detect_has_backlight() -> bool:
    """A backlight device with both brightness/max_brightness (same files
    _read_brightness reads). Desktops and VMs have none → screen_brightness off."""
    try:
        for bl in Path("/sys/class/backlight").iterdir():
            if (bl / "brightness").exists() and (bl / "max_brightness").exists():
                return True
    except OSError:
        pass
    return False


def _detect_has_wifi() -> bool:
    """At least one wireless interface exists (any /sys/class/net/*/wireless),
    independent of which interface is the current default route — gates the
    wifi_* items off on machines with no wireless hardware at all."""
    try:
        return any(_is_wireless(n.name) for n in Path("/sys/class/net").iterdir())
    except OSError:
        return False


def _detect_nvidia() -> bool:
    for dev in Path("/sys/bus/pci/devices").iterdir():
        try:
            if (dev / "vendor").read_text().strip() == "0x10de":
                if (dev / "class").read_text().strip().startswith("0x03"):
                    return True
        except OSError:
            continue
    return False


def _detect_intel_gpu() -> dict:
    """Find an Intel iGPU DRM card (vendor 0x8086, display class). Returns the
    gt_act_freq_mhz sysfs path and the PCI address (matches fdinfo's drm-pdev,
    used to attribute /proc/*/fdinfo drm-engine-* counters to this GPU)."""
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        device = card / "device"
        try:
            if (device / "vendor").read_text().strip() != "0x8086":
                continue
            if not (device / "class").read_text().strip().startswith("0x03"):
                continue
            freq_path = card / "gt_act_freq_mhz"
            pci = device.resolve().name   # e.g. "0000:00:02.0"
            return {
                "intel_gpu_freq_path": freq_path if freq_path.exists() else None,
                "intel_gpu_pci": pci,
            }
        except OSError:
            continue
    return {"intel_gpu_freq_path": None, "intel_gpu_pci": None}


# amdgpu labels several temperatures. "edge" is the one other tools report as
# "the" GPU temperature; junction (hotspot) and mem run 5-15°C hotter, so picking
# one of those would fire the shipped thresholds on a healthy card.
_AMD_TEMP_PREFERENCE = ("edge", "junction", "mem")

_AMD_KEYS = ("amd_gpu_busy_path", "amd_gpu_vram_used_path", "amd_gpu_vram_total_path",
             "amd_gpu_temp_path", "amd_gpu_fan_path", "amd_gpu_freq_path")


def _amd_hwmon_paths(device: Path) -> dict:
    """Resolve the amdgpu hwmon readings (temp/fan/sclk) for one card. The hwmon
    index isn't stable across boots, so it's found under the card's own device/
    rather than by a global /sys/class/hwmon scan — that also keeps two AMD GPUs
    from resolving to each other's sensors."""
    paths = {"amd_gpu_temp_path": None, "amd_gpu_fan_path": None, "amd_gpu_freq_path": None}
    try:
        hwmons = sorted((device / "hwmon").iterdir())
    except OSError:
        return paths
    for hw in hwmons:
        try:
            if (hw / "name").read_text().strip() != "amdgpu":
                continue
        except OSError:
            continue
        labelled: dict[str, Path] = {}
        for temp in sorted(hw.glob("temp[0-9]*_input")):
            label_file = temp.parent / temp.name.replace("_input", "_label")
            try:
                labelled[label_file.read_text().strip().lower()] = temp
            except OSError:
                labelled.setdefault("", temp)   # unlabelled: last-resort candidate
        for want in _AMD_TEMP_PREFERENCE:
            if want in labelled:
                paths["amd_gpu_temp_path"] = labelled[want]
                break
        else:
            # No recognized label: take the lowest-numbered sensor that exists.
            first = next(iter(sorted(hw.glob("temp[0-9]*_input"))), None)
            paths["amd_gpu_temp_path"] = first
        for key, name in (("amd_gpu_fan_path", "fan1_input"),
                          ("amd_gpu_freq_path", "freq1_input")):
            candidate = hw / name
            if candidate.exists():
                paths[key] = candidate
        return paths
    return paths


def _detect_amd_gpu() -> dict:
    """Find an AMD DRM card (vendor 0x1002, display class) and resolve its sysfs
    readings. On a hybrid AMD APU + AMD dGPU machine both cards match, so the one
    with the most VRAM wins — that's the discrete card, the one whose load and
    temperature a stats panel is about. Every path is independently optional."""
    best: Optional[tuple[int, dict]] = None
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        device = card / "device"
        try:
            if (device / "vendor").read_text().strip() != "0x1002":
                continue
            if not (device / "class").read_text().strip().startswith("0x03"):
                continue
        except OSError:
            continue
        busy = device / "gpu_busy_percent"
        if not busy.exists():
            continue          # not an amdgpu-driven card (radeon legacy): nothing to read
        vram_total = device / "mem_info_vram_total"
        vram_used  = device / "mem_info_vram_used"
        found = {
            "amd_gpu_busy_path": busy,
            "amd_gpu_vram_used_path":  vram_used  if vram_used.exists()  else None,
            "amd_gpu_vram_total_path": vram_total if vram_total.exists() else None,
            **_amd_hwmon_paths(device),
        }
        size = _read_path_int(vram_total) or 0
        if best is None or size > best[0]:
            best = (size, found)
    if best is None:
        return dict.fromkeys(_AMD_KEYS, None)
    return best[1]


# ── Sensor reads ──────────────────────────────────────────────────────────────

def _read_cpu_usage(state: DaemonState, cfg: Config) -> int:
    """CPU usage % via /proc/stat diff. Updates state.cpu_history in-place."""
    try:
        with Path("/proc/stat").open() as f:
            line = f.readline()
        vals = [int(x) for x in line.split()[1:]]
    except (OSError, ValueError):
        return 0

    usage = 0
    if state.cpu_prev_times:
        prev = state.cpu_prev_times
        if len(vals) == len(prev):
            idle_now  = vals[3] + vals[4]   # idle + iowait
            idle_prev = prev[3] + prev[4]
            total_now  = sum(vals)
            total_prev = sum(prev)
            dt = total_now - total_prev
            if dt > 0:
                usage = min(99, 100 - (idle_now - idle_prev) * 100 // dt)

    state.cpu_prev_times = vals

    now = time.monotonic()
    history_interval = cfg.display.history_interval
    if now - state.cpu_history_sample_ts >= history_interval:
        state.cpu_history_sample_ts = now
        # buffer sized to the widest consumer: spark (length chars), braille
        # (length chars * 2 samples/char), or the graphs page's history chart
        # (only when that page is enabled), across panel and tooltip
        max_len = max(cfg.spark_panel.cpu_spark_length,
                      cfg.spark_tooltip.cpu_spark_length,
                      cfg.braille_panel.cpu_braille_length * BRAILLE_LENGTH_MULTIPLIER,
                      cfg.braille_tooltip.cpu_braille_length * BRAILLE_LENGTH_MULTIPLIER,
                      cfg.pages.graph_history_length if "graphs" in cfg.pages.order else 0)
        state.cpu_history.append(usage)
        if len(state.cpu_history) > max_len:
            state.cpu_history = state.cpu_history[-max_len:]

    return usage


def _read_cpu_cores(state: DaemonState, cfg: Config) -> Optional[list[int]]:
    """Per-core CPU % via the /proc/stat per-cpu lines (cpu0, cpu1, …), same
    idle-diff as _read_cpu_usage. Maintains a history buffer per core (sampled at
    display.history_interval) for the cpu_cores page's braille. None on read error."""
    try:
        with Path("/proc/stat").open() as f:
            rows = [ln.split() for ln in f if ln.startswith("cpu") and ln[3:4].isdigit()]
        cores = [[int(x) for x in row[1:]] for row in rows]
    except (OSError, ValueError):
        return None
    if not cores:
        return None

    n = len(cores)
    if len(state.cpu_core_prev_times) != n:      # first sample or core count changed
        state.cpu_core_prev_times = [[] for _ in range(n)]
        state.cpu_core_history    = [[] for _ in range(n)]

    usage: list[int] = []
    for i, vals in enumerate(cores):
        prev = state.cpu_core_prev_times[i]
        u = 0
        if prev and len(vals) == len(prev):
            dt = sum(vals) - sum(prev)
            if dt > 0:
                idle = (vals[3] + vals[4]) - (prev[3] + prev[4])
                u = min(99, 100 - idle * 100 // dt)
        state.cpu_core_prev_times[i] = vals
        usage.append(u)

    now = time.monotonic()
    if now - state.cpu_core_history_sample_ts >= cfg.display.history_interval:
        state.cpu_core_history_sample_ts = now
        # The cpu_cores page stretches its braille to tooltip_width chars, so
        # size the buffer for the wider of that and the configured spark length.
        max_len = max(cfg.braille_tooltip.cpu_braille_length,
                      cfg.display.tooltip_width) * BRAILLE_LENGTH_MULTIPLIER
        for i in range(n):
            h = state.cpu_core_history[i]
            h.append(usage[i])
            if len(h) > max_len:
                del h[:-max_len]
    return usage


def _read_uptime() -> Optional[int]:
    try:
        with Path("/proc/uptime").open() as f:
            return int(float(f.readline().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def _read_load_avg() -> Optional[tuple[float, float, float]]:
    try:
        return os.getloadavg()
    except OSError:
        return None


_CLK_TCK   = os.sysconf("SC_CLK_TCK")
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

_total_mem_bytes: Optional[int] = None


def _mem_total_bytes() -> int:
    """Total RAM in bytes, cached (constant for the machine)."""
    global _total_mem_bytes
    if _total_mem_bytes is None:
        _total_mem_bytes = psutil.virtual_memory().total
    return _total_mem_bytes


def _read_proc_stat_times() -> dict[int, tuple[str, int, int]]:
    """Returns {pid: (comm, utime+stime jiffies, rss pages)} for all running
    processes. 300+ processes are scanned every TOP_PROCESS_TTL, so the per-file
    cost dominates: raw os.open/os.read/os.close into a fixed buffer (no buffered
    file object), parsing on the bytes, is ~37% faster than the buffered
    open()+read()+decode it replaced (bench: ~4.0ms → ~2.5ms for ~310 procs).
    A 1024-byte read always covers the fields we need — comm is kernel-capped
    at 16 chars, utime/stime sit right after it and rss (field 24) a bit further
    — and a maxsplit stops splitting after field 24 instead of walking all ~50.
    int() accepts the bytes tokens directly; comm is read positionally between
    the parens (rindex, since comm itself may contain ')'), latin-1 never
    raising on odd names."""
    result: dict[int, tuple[str, int, int]] = {}
    with os.scandir("/proc") as it:
        for entry in it:
            name = entry.name
            if not name.isdigit():
                continue
            try:
                fd = os.open(entry.path + "/stat", os.O_RDONLY)
                try:
                    buf = os.read(fd, 1024)
                finally:
                    os.close(fd)
                rparen = buf.rindex(b")")
                comm = buf[buf.index(b"(") + 1:rparen].decode("latin-1", "replace")
                # Post-comm fields, 0-indexed: state=0 … utime=11, stime=12, rss=21.
                # maxsplit=22 so rss (index 21) is isolated, not glued to the tail.
                fields = buf[rparen + 2:].split(None, 22)
                utime, stime, rss = int(fields[11]), int(fields[12]), int(fields[21])
            except (OSError, ValueError, IndexError):
                continue
            result[int(name)] = (comm, utime + stime, rss)
    return result


TOP_PROCESS_TTL   = 15.0   # seconds — scanning /proc/*/stat costs ~30-40ms, too much to do every poll
TOP_PROCESS_COUNT = 3      # rows the panel's Top 1/2/3 shows; the tooltip page takes more
TOP_PROCESS_PAGE_ROWS = 15 # rows the tooltip processes page shows (also the formatter's slice)

_CMDLINE_READ = 512        # bytes read from /proc/[pid]/cmdline — enough for argv[0] + the first args
_CMDLINE_MAX  = 64         # cap the resolved name; the formatter truncates further to the live column


def _cmdline_name(pid: int, fallback: str) -> str:
    """A fuller process name than the 15-char, kernel-capped stat comm: from
    /proc/[pid]/cmdline (NUL-separated argv), argv[0] reduced to its basename with
    the remaining args appended — '/usr/lib/firefox/firefox -contentproc …' ->
    'firefox -contentproc …'. Capped to _CMDLINE_MAX (the processes page truncates
    it further to the elastic COMMAND column). Falls back to `comm` for kernel
    threads and zombies, whose cmdline is empty."""
    try:
        fd = os.open(f"/proc/{pid}/cmdline", os.O_RDONLY)
        try:
            raw = os.read(fd, _CMDLINE_READ)
        finally:
            os.close(fd)
    except OSError:
        return fallback
    parts = [p for p in raw.split(b"\x00") if p]
    if not parts:
        return fallback
    argv0 = parts[0].rsplit(b"/", 1)[-1].decode("utf-8", "replace")
    rest  = [p.decode("utf-8", "replace") for p in parts[1:]]
    name  = " ".join([argv0, *rest]).strip()
    return name[:_CMDLINE_MAX] or fallback


def _read_top_process_cached(state: DaemonState) -> Optional[list[tuple[int, str, int, float]]]:
    now = time.monotonic()
    # The TTL only applies once we have a real value to hold onto: right after
    # startup (or any time the proc-stat diff has no previous sample yet),
    # _read_top_process() returns None, and caching that None for a full TTL
    # would mean waiting up to TOP_PROCESS_TTL seconds longer than necessary
    # for the first real reading (same retry-immediately logic as periph_scan).
    if state.top_process_cache is not None and now - state.top_process_cache_ts < TOP_PROCESS_TTL:
        return state.top_process_cache
    state.top_process_cache = _read_top_process(state)
    state.top_process_cache_ts = now
    return state.top_process_cache


def _diff_top_process(
    current: dict[int, tuple[str, int, int]], prev: dict[int, int], dt: float,
    keep_idle: bool = False,
) -> list[tuple[int, str, int, float]]:
    """(pid, comm, cpu%, mem%) sorted by cpu then mem, desc. CPU is the
    /proc/[pid]/stat jiffies diff — same pattern as _read_cpu_usage, normalized
    to one core (like top), so it's instantaneous, not ps's lifetime average.
    mem% is RSS over total RAM. keep_idle keeps 0% processes too — the page uses
    it to always fill a fixed row count (stable tooltip height); the panel drops
    them so Top 1/2/3 never shows an idle process."""
    total_mem = _mem_total_bytes()
    candidates: list[tuple[int, float, int, str]] = []  # (cpu%, mem%, pid, comm)
    if prev and dt > 0:
        for pid, (comm, total, rss) in current.items():
            prev_total = prev.get(pid)
            if prev_total is None or total < prev_total:
                continue
            pct = int((total - prev_total) / _CLK_TCK / dt * 100)
            if pct > 0 or keep_idle:
                mem = rss * _PAGE_SIZE / total_mem * 100 if total_mem else 0.0
                candidates.append((pct, mem, pid, comm))
    candidates.sort(reverse=True)
    return [(pid, comm, pct, mem) for pct, mem, pid, comm in candidates]


def _read_top_process(state: DaemonState) -> Optional[list[tuple[int, str, int, float]]]:
    """Panel path (Top 1/2/3), sampled on the 15s TTL via _read_top_process_cached.
    The full sorted list; the panel takes the first TOP_PROCESS_COUNT."""
    now = time.monotonic()
    current = _read_proc_stat_times()
    result = _diff_top_process(current, state.proc_prev_times, now - state.proc_prev_ts)
    state.proc_prev_times = {pid: total for pid, (_, total, _) in current.items()}
    state.proc_prev_ts = now
    return result or None


def read_top_process_page(state: DaemonState) -> Optional[list[tuple[int, str, int, float]]]:
    """Tooltip top-processes page: a fresh sample every call, off its own
    prev-state so it updates each poll instead of every TOP_PROCESS_TTL like the
    panel. Called by the daemon only while that page is the active one."""
    now = time.monotonic()
    current = _read_proc_stat_times()
    # First open: warm-start from the panel's already-sampled prev (up to
    # TOP_PROCESS_TTL old) so the very first render is real, full-height data
    # instead of the stale cached list — no "old then resize" flash. After that
    # the page's own prev drives short, instantaneous windows.
    prev    = state.page_proc_prev_times or state.proc_prev_times
    prev_ts = state.page_proc_prev_ts or state.proc_prev_ts
    result = _diff_top_process(current, prev, now - prev_ts, keep_idle=True)
    state.page_proc_prev_times = {pid: total for pid, (_, total, _) in current.items()}
    state.page_proc_prev_ts = now
    if not result:
        return None
    # Resolve the fuller cmdline name only for the rows the page actually shows —
    # reading /proc/[pid]/cmdline for all ~300 processes would waste the scan.
    return [(pid, _cmdline_name(pid, comm), pct, mem)
            for pid, comm, pct, mem in result[:TOP_PROCESS_PAGE_ROWS]]


def _read_mem_usage(state: DaemonState, cfg: Config) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """RAM usage % plus used/total GB. Updates state.mem_history in-place. Used
    GB is total - available, so it matches psutil's percent (not m.used)."""
    m = psutil.virtual_memory()
    usage = int(m.percent)
    gib = 1024 ** 3
    used_gb = round((m.total - m.available) / gib)
    total_gb = round(m.total / gib)

    now = time.monotonic()
    history_interval = cfg.display.history_interval
    if now - state.mem_history_sample_ts >= history_interval:
        state.mem_history_sample_ts = now
        max_len = max(cfg.spark_panel.mem_spark_length,
                      cfg.spark_tooltip.mem_spark_length,
                      cfg.braille_panel.mem_braille_length * BRAILLE_LENGTH_MULTIPLIER,
                      cfg.braille_tooltip.mem_braille_length * BRAILLE_LENGTH_MULTIPLIER,
                      cfg.pages.graph_history_length if "graphs" in cfg.pages.order else 0)
        state.mem_history.append(usage)
        if len(state.mem_history) > max_len:
            state.mem_history = state.mem_history[-max_len:]

    return usage, used_gb, total_gb


def _sample_gpu_history(state: DaemonState, cfg: Config, hw, r: Readings) -> None:
    """Sample the active GPU's usage + decoder into the shared history buffers
    for the graphs page (discrete before integrated on hybrids: Nvidia, then AMD,
    then Intel). Gated on the page being enabled, throttled to history_interval,
    trimmed to graph_history_length. Writes r.gpu_usage_history /
    r.gpu_dec_history; on a poll where the GPU wasn't read (skipped/None) it just
    re-exposes the buffer so a gap doesn't blank the chart."""
    if "graphs" not in cfg.pages.order:
        return
    if hw.has_nvidia:
        usage, dec = r.gpu_usage, r.gpu_dec
    elif hw.amd_gpu_busy_path:
        # amdgpu exposes no per-engine decoder utilization in sysfs, so the
        # graphs page draws the usage area with no overlay line for AMD.
        usage, dec = r.gpu_amd_usage, None
    elif hw.intel_gpu_pci:
        usage, dec = r.gpu_intel_usage, r.gpu_intel_dec_usage
    else:
        return
    if usage is not None:
        now = time.monotonic()
        if now - state.gpu_history_sample_ts >= cfg.display.history_interval:
            state.gpu_history_sample_ts = now
            max_len = cfg.pages.graph_history_length
            state.gpu_usage_history.append(usage)
            state.gpu_dec_history.append(dec or 0)
            if len(state.gpu_usage_history) > max_len:
                state.gpu_usage_history = state.gpu_usage_history[-max_len:]
            if len(state.gpu_dec_history) > max_len:
                state.gpu_dec_history = state.gpu_dec_history[-max_len:]
    r.gpu_usage_history = list(state.gpu_usage_history)
    r.gpu_dec_history = list(state.gpu_dec_history)


def _sample_net_history(state: DaemonState, cfg: Config, r: Readings) -> None:
    """Sample network up/down byte-rates into the shared history buffers for the
    graphs page. Gated on the page being enabled, throttled to history_interval,
    trimmed to graph_history_length. Writes r.net_up_history / r.net_down_history;
    on a poll where the rate wasn't read it just re-exposes the buffer."""
    if "graphs" not in cfg.pages.order:
        return
    up, down = r.net_up_bps, r.net_down_bps
    if up is not None or down is not None:
        now = time.monotonic()
        if now - state.net_history_sample_ts >= cfg.display.history_interval:
            state.net_history_sample_ts = now
            max_len = cfg.pages.graph_history_length
            state.net_up_history.append(up or 0)
            state.net_down_history.append(down or 0)
            if len(state.net_up_history) > max_len:
                state.net_up_history = state.net_up_history[-max_len:]
            if len(state.net_down_history) > max_len:
                state.net_down_history = state.net_down_history[-max_len:]
    r.net_up_history = list(state.net_up_history)
    r.net_down_history = list(state.net_down_history)


def _read_swap_usage() -> Optional[int]:
    s = psutil.swap_memory()
    if s.total == 0:
        return None
    return int(s.percent)


def _counter_rate(
    rate: _RateState, cur_a: int, cur_b: int,
) -> tuple[Optional[int], Optional[int]]:
    """Bytes/s for two cumulative counters from the diff against the previous
    poll. Returns (None, None) until there's a prior sample to diff against (and
    when dt <= 0). Updates `rate` in place."""
    now = time.monotonic()
    a_bps = b_bps = None
    if rate.ts > 0:
        dt = now - rate.ts
        if dt > 0:
            a_bps = int((cur_a - rate.prev_a) / dt)
            b_bps = int((cur_b - rate.prev_b) / dt)
    rate.prev_a, rate.prev_b, rate.ts = cur_a, cur_b, now
    return a_bps, b_bps


def _read_net_speed(state: DaemonState, device: str) -> tuple[Optional[int], Optional[int]]:
    try:
        counters = psutil.net_io_counters(pernic=True).get(device)
    except OSError:
        return None, None
    if counters is None:
        return None, None
    # up = tx (bytes_sent), down = rx (bytes_recv)
    return _counter_rate(state.net_rate, counters.bytes_sent, counters.bytes_recv)


def _resolve_mounts(cfg: Config) -> list[str]:
    """Ordered list of mountpoints whose disk usage to show. An explicit list in
    [disks].mounts is used as-is; "auto" (the default) discovers the filesystems
    currently mounted under [disks].auto_roots (/mnt, /media, /run/media, ...)
    via psutil.disk_partitions(). Only real mounts are returned — empty leftover
    folders from old mounts in /mnt or /media are skipped, and plugging/unplugging
    a drive is reflected on the next poll. "/" is always first."""
    mounts = cfg.disks.mounts
    if isinstance(mounts, list):
        return mounts
    roots = tuple(r.rstrip("/") + "/" for r in cfg.disks.auto_roots)
    found: list[str] = []
    try:
        for part in psutil.disk_partitions():
            mp = part.mountpoint
            if mp != "/" and mp.startswith(roots):
                found.append(mp)
    except OSError:
        pass
    return ["/"] + sorted(found)


def _read_disk_usage(mount: str) -> Optional[DiskUsage]:
    try:
        du = psutil.disk_usage(mount)
    except OSError:
        return None
    gib = 1024 ** 3
    return DiskUsage(
        percent=int(du.percent), used_gb=round(du.used / gib), total_gb=round(du.total / gib))


def _read_disk_io(state: DaemonState, device: str) -> tuple[Optional[int], Optional[int]]:
    try:
        counters = psutil.disk_io_counters(perdisk=True).get(device)
    except OSError:
        return None, None
    if counters is None:
        return None, None
    return _counter_rate(state.disk_rate, counters.read_bytes, counters.write_bytes)


def _read_cpu_freq(path: Optional[Path]) -> Optional[float]:
    if path is not None:
        try:
            return int(path.read_text()) / 1000   # kHz → MHz
        except (OSError, ValueError):
            pass
    freq = psutil.cpu_freq()   # fallback: driver without scaling_cur_freq on cpu0
    return freq.current if freq is not None else None


def _read_cpu_turbo() -> Optional[bool]:
    p = Path("/sys/devices/system/cpu/intel_pstate/no_turbo")
    if p.exists():
        return p.read_text().strip() == "0"
    p = Path("/sys/devices/system/cpu/cpufreq/boost")
    if p.exists():
        return p.read_text().strip() == "1"
    return None


def _read_brightness() -> Optional[int]:
    for bl in Path("/sys/class/backlight").iterdir():
        cur_f = bl / "brightness"
        max_f = bl / "max_brightness"
        if not (cur_f.exists() and max_f.exists()):
            continue
        try:
            cur = int(cur_f.read_text())
            mx  = int(max_f.read_text())
            if mx > 0:
                return cur * 100 // mx
        except (OSError, ValueError):
            continue
    return None


def _sysfs_bat_rate(bat_id: str) -> int:
    """Fallback: reads power_now from sysfs (microwatts → rounded watts)."""
    name = bat_id.rsplit("battery_", 1)[-1]   # /org/.../battery_BAT0 → BAT0
    try:
        uw = int(Path(f"/sys/class/power_supply/{name}/power_now").read_text())
        return round(uw / 1_000_000)
    except OSError:
        return 0


def _sysfs_bat_charge_limit(bat_id: str) -> Optional[int]:
    """Reads charge_control_end_threshold from sysfs (configured charge limit, e.g. 80)."""
    name = bat_id.rsplit("battery_", 1)[-1]   # /org/.../battery_BAT0 → BAT0
    try:
        limit = int(Path(f"/sys/class/power_supply/{name}/charge_control_end_threshold").read_text())
        return limit if limit < 100 else None
    except OSError:
        return None


BAT_CACHE_TTL = 30.0   # seconds

_SYSFS_BAT_STATUS_MAP = {
    "Full":         "fully-charged",
    "Charging":     "charging",
    "Discharging":  "discharging",
}   # anything else (e.g. "Not charging" at a charge limit) maps to "" — the
    # charge-limit/100% check in the formatter already covers that case


def _sysfs_bat_read(bat_id: str) -> tuple[str, int, str]:
    """Reads percentage/state/rate directly from sysfs (capacity/status/power_now),
    without an upower subprocess. Raises OSError if sysfs is unavailable."""
    name = bat_id.rsplit("battery_", 1)[-1]   # /org/.../battery_BAT0 → BAT0
    base = Path(f"/sys/class/power_supply/{name}")
    capacity = int((base / "capacity").read_text())
    status   = (base / "status").read_text().strip()
    state    = _SYSFS_BAT_STATUS_MAP.get(status, "")
    rate     = _sysfs_bat_rate(bat_id) if state in ("charging", "discharging") else 0
    return f"{capacity}%", rate, state


def _read_battery_sys(state: DaemonState, hw: HardwareInfo, cfg: Config) -> list[BatterySys]:
    result = []
    for bat_id in hw.battery_sys_ids:
        cache = state.battery_sys_cache.setdefault(bat_id, _BatterySysCache())
        if time.monotonic() - cache.ts >= BAT_CACHE_TTL:
            try:
                cache.perc, cache.rate, cache.state = _sysfs_bat_read(bat_id)
                cache.limit = _sysfs_bat_charge_limit(bat_id)
                cache.ts = time.monotonic()
            except OSError:
                # sysfs unavailable: fall back to UPower over GDBus.
                props = _upower_device_props(bat_id, ("Percentage", "State", "EnergyRate"))
                if props and props.get("Percentage") is not None:
                    cache.perc  = f"{int(props['Percentage'])}%"
                    cache.state = _UPOWER_STATE_MAP.get(props.get("State"), "")
                    cache.rate  = round(props.get("EnergyRate") or 0)
                    if cache.rate == 0 and cache.state in ("charging", "discharging"):
                        cache.rate = _sysfs_bat_rate(bat_id)
                    cache.limit = _sysfs_bat_charge_limit(bat_id)
                    cache.ts = time.monotonic()
        if cache.perc:
            result.append(BatterySys(
                id=bat_id, perc=cache.perc, rate=cache.rate, state=cache.state,
                limit=cache.limit))
    return result


PERIPH_CACHE_TTL = 30.0
# 1h: a keyboard's charge changes over days, and every Bolt query wakes the
# device from deep sleep — the first round-trip after idle costs ~900ms (the
# wakeup, not the data) and needlessly drains the keyboard's own battery, so
# polling it often buys nothing. The wake cost is unavoidable per refresh;
# spacing refreshes out is what keeps it rare.
BOLT_CACHE_TTL   = 3600.0


def _read_battery_periph(
    cache: _BatteryPeriphCache,
    upower_path: str,
    name_override: Optional[str],
) -> Optional[BatteryPeriph]:
    # A rescan moves the device to a fresh object path: the name and level
    # cached from the old one describe a device we no longer read. Drop them so
    # the next read repopulates both, name included.
    if upower_path != cache.path:
        cache.path, cache.name, cache.perc, cache.ts = upower_path, "", "", float("-inf")
    if time.monotonic() - cache.ts >= PERIPH_CACHE_TTL:
        props = _upower_device_props(upower_path, ("Percentage", "Model"))
        pct = props.get("Percentage") if props else None
        # A vanished object path is not an error on the bus: the proxy still
        # builds and every property reads back None. That — not `props is None`
        # — is what tells us the path died and needs rediscovering. A device
        # merely switched off keeps answering, with Percentage 0.
        cache.gone = pct is None
        if props and not cache.name and props.get("Model"):
            cache.name = props["Model"]
        # 0% (or missing) = device disconnected: leave perc empty so it
        # disappears from the tooltip.
        cache.perc = f"{int(pct)}%" if pct else ""
        cache.ts = time.monotonic()
    if not cache.perc:
        return None
    return BatteryPeriph(
        name=name_override or cache.name,
        perc=cache.perc,
    )


def _read_battery_bolt(
    cache: _BatteryPeriphCache,
    dev_idx: int,
    name_override: Optional[str],
) -> Optional[BatteryPeriph]:
    if not _BOLT_AVAILABLE:
        return None
    if time.monotonic() - cache.ts >= BOLT_CACHE_TTL:
        try:
            # The HID++ device-name read costs ~10x the battery read (~930ms vs
            # ~95ms on an MX Keys S — an extra, much slower round-trip). The name
            # never changes, so fetch it only until we have it cached; every
            # refresh after that re-reads just the level.
            want_name = not name_override and not cache.name
            name, level = _bolt_query(dev_idx, want_name=want_name)
            if level is None:
                cache.ts = time.monotonic()
                return None
            cache.name = name_override or name or cache.name
            cache.perc = f"{level}%"
            cache.ts = time.monotonic()
        except OSError:
            return None
    if not cache.perc:
        return None
    return BatteryPeriph(name=cache.name, perc=cache.perc)


INTEL_GPU_ENGINES = ("render", "copy", "video", "video-enhance")
INTEL_GPU_USAGE_TTL = 30.0   # seconds — scanning /proc/*/fd/* (readlink per fd) costs ~20-40ms,
                              # more than top_process; same TTL as hd_temp/fan_speed


def _read_intel_gpu_engine_times(pci_addr: str) -> dict[int, dict[str, int]]:
    """Scans /proc/*/fd/* for DRM client fds and reads their fdinfo drm-engine-*
    counters (ns), same mechanism intel_gpu_top uses without root. Keyed by
    drm-client-id to dedupe fds that share the same underlying DRM file.
    os.scandir + os.readlink + raw open() instead of pathlib: this walks every
    fd of every process, so per-entry overhead dominates (same reason as
    _read_proc_stat_times)."""
    result: dict[int, dict[str, int]] = {}
    needle = f"drm-pdev:\t{pci_addr}"
    try:
        proc_it = os.scandir("/proc")
    except OSError:
        return result
    with proc_it:
        for pid_dir in proc_it:
            pid = pid_dir.name
            if not pid.isdigit():
                continue
            try:
                fd_it = os.scandir(f"/proc/{pid}/fd")
            except OSError:
                continue
            with fd_it:
                for fd in fd_it:
                    try:
                        link = os.readlink(fd.path)
                    except OSError:
                        continue
                    if "/dri/" not in link:
                        continue
                    try:
                        with open(f"/proc/{pid}/fdinfo/{fd.name}", "rb") as f:
                            text = f.read().decode("latin-1", "replace")
                    except OSError:
                        continue
                    if needle not in text:
                        continue
                    client_id = None
                    engines: dict[str, int] = {}
                    for line in text.splitlines():
                        if line.startswith("drm-client-id:"):
                            client_id = int(line.split(":", 1)[1].strip())
                        elif line.startswith("drm-engine-"):
                            name, val = line.split(":", 1)
                            engines[name[len("drm-engine-"):].strip()] = int(val.strip().split()[0])
                    if client_id is not None:
                        result[client_id] = engines
    return result


def _read_intel_gpu_metrics(state: DaemonState, pci_addr: str) -> dict[str, int]:
    """Per-engine utilization % since the previous sample, summed across clients
    per engine and capped at 100 since true wall-clock overlap can't exceed it.
    'render' is the 3D/compute engine (general GPU load); 'video' is the VDBOX
    decode/encode engine — the one VAAPI hardware video decode actually uses."""
    now = time.monotonic()
    current = _read_intel_gpu_engine_times(pci_addr)
    prev = state.intel_gpu_engine_prev
    dt = now - state.intel_gpu_prev_ts

    pct = {engine: 0 for engine in INTEL_GPU_ENGINES}
    if prev and dt > 0:
        sums = {engine: 0 for engine in INTEL_GPU_ENGINES}
        for client_id, engines in current.items():
            prev_engines = prev.get(client_id)
            if prev_engines is None:
                continue
            for engine, ns in engines.items():
                delta = ns - prev_engines.get(engine, ns)
                if delta > 0:
                    sums[engine] = sums.get(engine, 0) + delta
        dt_ns = dt * 1_000_000_000
        pct = {engine: min(99, int(ns_sum / dt_ns * 100)) for engine, ns_sum in sums.items()}

    state.intel_gpu_engine_prev = current
    state.intel_gpu_prev_ts = now
    return pct


def _read_intel_gpu_metrics_cached(state: DaemonState, pci_addr: str) -> dict[str, int]:
    now = time.monotonic()
    if now - state.intel_gpu_usage_cache_ts < INTEL_GPU_USAGE_TTL:
        return state.intel_gpu_usage_cache
    state.intel_gpu_usage_cache = _read_intel_gpu_metrics(state, pci_addr)
    state.intel_gpu_usage_cache_ts = now
    return state.intel_gpu_usage_cache


GPU_CACHE_TTL        = 3.0     # nvidia-smi fallback: it forks, worth caching
GPU_CACHE_TTL_NVML   = 0.0     # pynvml is ~0.3ms: re-read every poll, more responsive


def _gpu_cache_ttl() -> float:
    """0 (read every poll) when pynvml is usable — an NVML read is ~0.3ms, so the
    cache buys nothing and a per-poll read makes temp/usage twice as responsive —
    otherwise the nvidia-smi TTL, since that path forks."""
    return GPU_CACHE_TTL_NVML if (_PYNVML_AVAILABLE and not _pynvml_init_failed) else GPU_CACHE_TTL


def _pct_cap(v: Optional[int]) -> Optional[int]:
    """Cap a metric at 99 (panel/tooltip render % as two digits), passing None through."""
    return min(v, 99) if v is not None else None


_pynvml_handle = None
_pynvml_init_failed = False


def _pynvml_handle_get():
    """Lazily nvmlInit() and cache GPU 0's handle. Returns None (once and for
    all) if NVML init or the handle lookup fails, so _read_nvidia falls back to
    nvidia-smi. Init is attempted a single time, not retried every poll."""
    global _pynvml_handle, _pynvml_init_failed
    if _pynvml_init_failed:
        return None
    if _pynvml_handle is None:
        try:
            pynvml.nvmlInit()
            _pynvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception:
            _pynvml_init_failed = True
            return None
    return _pynvml_handle


def _read_nvidia_pynvml() -> Optional[tuple[Optional[int], ...]]:
    """(temp, usage, mem, dec, fan) via NVML, or None on failure so the caller
    can fall back to nvidia-smi. fan/decoder are optional (some GPUs/drivers
    don't expose them) and degrade to None individually."""
    h = _pynvml_handle_get()
    if h is None:
        return None
    try:
        temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
        util = pynvml.nvmlDeviceGetUtilizationRates(h)
    except Exception:
        return None
    try:
        fan = pynvml.nvmlDeviceGetFanSpeed(h)
    except Exception:
        fan = None
    try:
        dec = pynvml.nvmlDeviceGetDecoderUtilization(h)[0]
    except Exception:
        dec = None
    return (_pct_cap(temp), _pct_cap(util.gpu), _pct_cap(util.memory),
            _pct_cap(dec), _pct_cap(fan))


def _read_nvidia_smi() -> tuple[Optional[int], ...]:
    """Fallback for when pynvml (python-nvidia-ml-py) isn't installed or NVML
    init failed. Same (temp, usage, mem, dec, fan) tuple as _read_nvidia_pynvml."""
    try:
        out = subprocess.check_output([
            "nvidia-smi",
            "--query-gpu=temperature.gpu,utilization.gpu,utilization.memory,"
            "fan.speed,utilization.decoder",
            "--format=csv,noheader,nounits",
        ], text=True, timeout=5)
        parts = [p.strip() for p in out.split(",")]
        def _i(v: str) -> Optional[int]:
            try:
                return _pct_cap(int(v))
            except ValueError:
                return None
        # Caller unpacks (temp, usage, mem, dec, fan). The query lists fan.speed
        # (parts[3]) before utilization.decoder (parts[4]), so dec is parts[4]
        # and fan is parts[3] — not parts[3], parts[4] in order.
        return _i(parts[0]), _i(parts[1]), _i(parts[2]), _i(parts[4]), _i(parts[3])
    except (*_SUBPROCESS_ERRORS, IndexError):
        return None, None, None, None, None


def _read_nvidia(state: DaemonState) -> tuple[Optional[int], ...]:
    if state.gpu_cache and time.monotonic() - state.gpu_cache_ts < _gpu_cache_ttl():
        return state.gpu_cache  # type: ignore[return-value]
    result = _read_nvidia_pynvml() if _PYNVML_AVAILABLE else None
    if result is None:   # pynvml absent, NVML init failed, or a read raised
        result = _read_nvidia_smi()
    state.gpu_cache    = result
    state.gpu_cache_ts = time.monotonic()
    return result


def _read_amd_vram_percent(hw) -> Optional[int]:
    """VRAM occupancy % from mem_info_vram_used/total. amdgpu's mem_busy_percent
    is a bandwidth-utilization figure, not occupancy, so it isn't the analog of
    Nvidia's memory number the mem_usage thresholds are written against — the
    used/total ratio is."""
    used  = _read_path_int(hw.amd_gpu_vram_used_path)
    total = _read_path_int(hw.amd_gpu_vram_total_path)
    if used is None or not total:
        return None
    return _pct_cap(round(used * 100 / total))


def _read_count_file(path: str) -> Optional[int]:
    """Reads an integer (e.g. the pacman updates count) from a file written by
    an external checker. Plain file read — no subprocess, never blocks the poll
    loop. None if the file is missing/unreadable/empty/not an int."""
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _read_server_file(path: str) -> Optional[bool]:
    """Reads a server-reachability flag from a file written by an external ping
    checker: '1' = reachable, '0' = not. Plain file read — no ping subprocess in
    the poll loop. None if missing/unreadable/unrecognized (renders as empty)."""
    try:
        with open(path) as f:
            val = f.read().strip()
    except OSError:
        return None
    if val == "1":
        return True
    if val == "0":
        return False
    return None
