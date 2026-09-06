#!/usr/bin/env python3
"""Render PiroStats as if this machine had a Radeon.

The gpu_amd_* rows are gated on amdgpu's sysfs files, so on a machine with any
other GPU they correctly render nowhere — which also means nobody without an AMD
card can look at them. This builds a throwaway amdgpu tree in a temp directory
and points sensors._detect_amd_gpu at it; everything else is untouched, so the
real read path parses the fake files and the Hz->MHz division, the millidegree
temperature, the edge/junction/mem label preference and the used/total VRAM
ratio are all exercised rather than mocked.

The tree carries all three labelled temperatures on purpose: edge is the cool
one, so a render showing junction's or mem's value means the preference in
_amd_hwmon_paths broke.

Usage:
  python3 tools/fake_amd_gpu.py                    # tooltip, stripped to text
  python3 tools/fake_amd_gpu.py --component panel  # the panel row instead
  python3 tools/fake_amd_gpu.py --page graphs      # a deep-dive page
  python3 tools/fake_amd_gpu.py --only-amd         # hide this machine's own GPU
  python3 tools/fake_amd_gpu.py --daemon           # live on the widget:
      systemctl --user stop pirostats              #   the real daemon would
      python3 tools/fake_amd_gpu.py --daemon       #   fight over the same files
      systemctl --user start pirostats             #   when done (Ctrl-C first)

--only-amd matters for the graphs page and for _sample_gpu_history: both pick a
single GPU, discrete first, so on a machine that really has an Nvidia or Intel
one the AMD chart is never the one drawn. Hiding the real card is the only way
to see that path.

The shipped config/config.toml is used, not ~/.config/pirostats/config.toml: a
personal config predates these items and would list none of them, which would
look like the fake card failing.
"""
import argparse
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import sensors        # noqa: E402
import daemon         # noqa: E402

# One idle-ish but non-zero card, in the units amdgpu actually publishes:
# percent for busy, bytes for VRAM, millidegrees for hwmon temps, RPM for the
# fan, Hz for sclk.
CARD = {
    "gpu_busy_percent":     "63",
    "mem_info_vram_used":   "11274289152",   # 10.5 GiB of…
    "mem_info_vram_total":  "17179869184",   # …16 GiB -> 66%
}
HWMON = {
    "name":         "amdgpu",
    "temp1_input":  "61000",  "temp1_label": "edge",
    "temp2_input":  "74000",  "temp2_label": "junction",
    "temp3_input":  "70000",  "temp3_label": "mem",
    "fan1_input":   "1480",                  # RPM, not a duty percent
    "freq1_input":  "2415000000",            # 2415 MHz -> "2.4 GHz"
}


def build_tree(root: Path) -> Path:
    """Write the fake card under `root` and return its device/ directory."""
    device = root / "card9" / "device"
    hwmon = device / "hwmon" / "hwmon7"
    hwmon.mkdir(parents=True)
    for name, value in CARD.items():
        (device / name).write_text(value + "\n")
    for name, value in HWMON.items():
        (hwmon / name).write_text(value + "\n")
    return device


def main() -> None:
    ap = argparse.ArgumentParser(description="Render PiroStats with a fake AMD GPU")
    ap.add_argument("--component", choices=("panel", "tooltip", "both"), default="tooltip")
    ap.add_argument("--page", help="a tooltip deep-dive page (processes, graphs, …)")
    ap.add_argument("--only-amd", action="store_true",
                    help="also hide this machine's real GPU, so AMD is the one the "
                         "graphs page and the history buffers pick")
    ap.add_argument("--daemon", action="store_true",
                    help="run the real poll loop so the fake card shows up on the widget")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="pirostats-fake-amd-") as tmp:
        device = build_tree(Path(tmp))
        # discover_hardware() resolves _detect_amd_gpu through the module at call
        # time, so replacing it here is enough; _amd_hwmon_paths already takes the
        # device directory as an argument, so the hwmon half needs no faking.
        hwmon_paths = sensors._amd_hwmon_paths
        sensors._detect_amd_gpu = lambda: {
            "amd_gpu_busy_path":       device / "gpu_busy_percent",
            "amd_gpu_vram_used_path":  device / "mem_info_vram_used",
            "amd_gpu_vram_total_path": device / "mem_info_vram_total",
            **hwmon_paths(device),
        }
        if args.only_amd:
            sensors._detect_nvidia = lambda: False
            sensors._detect_intel_gpu = lambda: {"intel_gpu_freq_path": None, "intel_gpu_pci": None}

        cfg_path = REPO / "config" / "config.toml"
        if args.daemon:
            daemon.run_daemon(cfg_path)
        else:
            daemon.run_render(cfg_path, component=args.component, fmt="text", page=args.page)


if __name__ == "__main__":
    main()
