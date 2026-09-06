"""Golden/snapshot test: the regression safety net for the metric×form model.

Renders the panel (vertical + horizontal) and the tooltip with a COMPLETE
hardware/readings fixture and compares them byte-for-byte against the
snapshots in tests/golden/. Any change to the rendered HTML — a render port,
a CSS migration, table collapsing — must leave these unchanged. If a change is
INTENDED, regenerate the snapshots:

    UPDATE_GOLDEN=1 python3 -m pytest tests/test_golden_render.py

The fixture turns on every piece of hardware and populates every reading, so
every configured item (in both orientations) actually renders.
"""
import os
from pathlib import Path

import pytest

import config
from config import load_config, apply_canonical_width, PanelGeometry
from formatter import PanelFormatter
from sensors import HardwareInfo, Readings, DiskUsage, BatterySys, BatteryPeriph

GOLDEN = Path(__file__).parent / "golden"


def _full_hw() -> HardwareInfo:
    return HardwareInfo(
        cpu_temp_path=Path("/x"), cpu_freq_path=Path("/x"),
        hd_temp_paths={"nvme0": Path("/x"), "sda": Path("/x")},
        fan_paths={"1": Path("/x"), "2": Path("/x")},
        battery_sys_ids=["/org/freedesktop/UPower/devices/battery_BAT0"],
        has_nvidia=True, intel_gpu_freq_path=Path("/x"), intel_gpu_pci="0000:00:02.0",
        net_device="wlan0", disk_io_device="nvme0n1", cpu_count=8,
        cpu_turbo_supported=True, has_backlight=True, has_wifi=True,
        battery_mouse_id="/m", battery_kbd_id="/k",
        disk_smart_drives={"nvme0": ("/d0", "nvme", False), "sda": ("/d1", "ata", True)},
    )


def _full_readings() -> Readings:
    return Readings(
        cpu_usage=73, cpu_temp=55, cpu_freq=3200.0, cpu_turbo=True,
        cpu_history=[10, 20, 30, 40, 50, 60, 70, 80, 73, 65] * 2,
        mem_history=[15, 25, 35, 45, 55, 42, 38, 44, 50, 42] * 2,
        uptime=123456, load_avg=(1.2, 0.9, 0.7),
        top_process=[("plasmashell", 12), ("firefox", 8)],
        mem_usage=42, mem_used_gb=13, mem_total_gb=32, swap_usage=10,
        net_up_bps=500000, net_down_bps=2000000,
        net_device="wlan0", ip_address="192.168.1.5", wifi_ssid="MyWifi", wifi_signal=80,
        disk_read_bps=1500000, disk_write_bps=800000,
        disk_usage={"/": DiskUsage(50, 100, 200), "/mnt/data": DiskUsage(70, 700, 1000)},
        disk_smart={"nvme0": True, "sda": True},
        hd_temps={"nvme0": 45, "sda": 50},
        fan_speeds={"1": 1200, "2": 0},
        battery_sys=[BatterySys(id="/BAT0", perc="80%", rate=15, state="discharging", limit=80)],
        battery_mouse=BatteryPeriph("Logi Mouse", "90%"),
        battery_kbd=BatteryPeriph("Logi Kbd", "85%"),
        gpu_temp=60, gpu_usage=30, gpu_mem=40, gpu_dec=5, gpu_fan=25,
        gpu_intel_freq=900, gpu_intel_usage=20, gpu_intel_dec_usage=2,
        screen_brightness=75, system_updates=3, server_ok=True,
    )


def _render(vertical: bool, kind: str) -> str:
    cfg = load_config(vertical=vertical)
    fmt = PanelFormatter(cfg, _full_hw())
    r = _full_readings()
    if kind != "panel":
        # Match the runtime: the tooltip width is the derived canonical, not the
        # raw config value (now 0/auto), so the golden reflects the real width.
        apply_canonical_width(cfg, fmt.canonical_width(r))
    return fmt.format_panel(r) if kind == "panel" else fmt.format_tooltip(r)


CASES = [
    ("panel_v", True, "panel"),
    ("panel_h", False, "panel"),
    ("tooltip", True, "tooltip"),
]


@pytest.mark.parametrize("name,vertical,kind", CASES)
def test_golden_render(name, vertical, kind, monkeypatch):
    # FIXED geometry: without usable_px/glyph_adv the vertical panel's auto-fit
    # doesn't trigger and the width is config's own defaults — so the snapshot
    # doesn't depend on /tmp/pirostats_geom, which a live daemon writes and rewrites.
    monkeypatch.setattr(config, "detect_panel_geometry", lambda: PanelGeometry(vertical=True))
    # NO machine block: load_config() would otherwise merge whichever one matches
    # the machine running the suite (~/.config/pirostats/machines.toml included),
    # so the snapshot would encode the contributor's hardware instead of the
    # shipped defaults.
    monkeypatch.setattr(config, "detect_machine", lambda machines: None)
    # FIXED time: battery_sys in the panel alternates percentage/watts based on
    # time.time() // interval — freezing it pins the phase, otherwise panel_v
    # would change every few seconds.
    monkeypatch.setattr("time.time", lambda: 1_000_000.0)
    out = _render(vertical, kind)
    path = GOLDEN / f"{name}.html"
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(out)
        pytest.skip(f"golden {name} regenerated")
    assert out == path.read_text(), (
        f"Rendered HTML differs from snapshot {name}.html — if intended, regenerate with "
        f"UPDATE_GOLDEN=1"
    )
