# Items reference

Every item you can put in `config.toml`, what it shows, and an example of the
row it renders. This is the plain-language companion to the token naming: you
should never need to decode a name — find the thing you want here and copy the
token.

The authoritative, always-current list of tokens (and where each one is allowed)
is `pirostats list-items`. This file adds the descriptions and examples.

**How to read the examples.** Values are illustrative, not live. In the
**tooltip** a row is `label: value` (the label carries a glyph in front of the
word); in the **panel** the same item is drawn as the glyph alone followed by
its value, to stay compact. Sparklines/bars are shown here with unicode blocks
(`▁▂▃▅▇█`, `⣀⣤⣶⣿`) standing in for the real Nerd Font drawing.

A token is **bare** unless the metric offers more than one rendering; only then
does it take a `:suffix`. See [TOKEN_GRAMMAR.md](TOKEN_GRAMMAR.md) for the rule.

---

## CPU & memory

`cpu_usage` and `mem_usage` are the only items with a recorded history, so they
are the only ones with a visual menu. The seven suffixed forms below exist for
both — `mem_usage:bar`, `mem_usage:spark`, … mirror `cpu_usage` exactly.

| Token | Where | What it shows | Example row |
| --- | --- | --- | --- |
| `cpu_usage` | panel · tooltip | CPU utilization, as a percentage. | `CPU usage:  11%` |
| `cpu_usage:bar` | panel | Full bar filling the row, colored by threshold. | `███████░░░` |
| `cpu_usage:spark` | panel | Block sparkline of the recent history. | `▁▂▃▅▇▇▅▃▂▁` |
| `cpu_usage:braille` | panel | Braille sparkline, denser (2 samples per char). | `⣀⣤⣶⣷⣶⣤⣀` |
| `cpu_usage:spark_value` | tooltip | History spark next to the number. | `CPU usage: ▂▃▅▇ 11%` |
| `cpu_usage:braille_value` | tooltip | History braille next to the number. | `CPU usage: ⣤⣶⣷ 11%` |
| `cpu_usage:bar_spark` | tooltip | "Now" bar plus the history spark. | `CPU usage: ███░ ▂▃▅▇` |
| `cpu_usage:bar_braille` | tooltip | "Now" bar plus the history braille. | `CPU usage: ███░ ⣤⣶⣷` |
| `mem_usage` | panel · tooltip | RAM utilization, %. Same seven variants as `cpu_usage`; in the tooltip the used/total GB ride in the middle like `disk_usage`. | `Mem usage: 6G / 15G  35%` |
| `swap_usage` | panel · tooltip | Swap utilization, %. Shown only when swap exists. | `Swap usage:  12%` |
| `cpu_temp` | panel · tooltip | CPU package temperature. | `CPU temp:  43°C` |
| `cpu_freq` | panel · tooltip | CPU clock; the turbo state rides along in front. | `CPU freq: Turbo  3.2 GHz` |
| `cpu_turbo` | panel · tooltip | Turbo/boost state on its own, on/off. | `CPU turbo:  on` |

## Drives

| Token | Where | What it shows | Example row |
| --- | --- | --- | --- |
| `disk_usage` | panel · tooltip | Filesystem usage, one row per mounted disk. In the tooltip the used/total GB ride in the middle. | `Root: 30G / 98G  32%` |
| `hd_temp` | panel · tooltip | Disk temperature (SMART), one row per drive. | `Disk Nvme:  31°C` |
| `hd_temp:pair` | tooltip | The same, two drives per row to save space. | `Nvme: 31°C   Sda: 34°C` |
| `disk_smart:pair` | tooltip | SMART health verdict per drive, two per row. | `Nvme: OK   Sda: OK` |
| `disk_io` | panel · tooltip | Read/write throughput of the main disk (adapts to orientation). | `Read: 0   Write: 4M` |

## GPU

Every vendor's rows are gated on that hardware being present, so listing all
three families costs nothing on a machine that has one of them. AMD reads plain
`amdgpu` sysfs (no library, no fork) and exposes no decoder counter, so it has no
`dec_usage` twin; its fan is RPM, where NVIDIA's is a duty percent.

| Token | Where | What it shows | Example row |
| --- | --- | --- | --- |
| `gpu_nvidia_usage` | panel · tooltip | NVIDIA GPU utilization, %. | `Gpu usage:  35%` |
| `gpu_nvidia_mem_usage` | panel · tooltip | NVIDIA VRAM utilization, %. | `Gpu mem usage:  48%` |
| `gpu_nvidia_dec_usage` | panel · tooltip | NVIDIA video-decoder utilization, %. | `Gpu decoder usage:  0%` |
| `gpu_nvidia_temp` | panel · tooltip | NVIDIA GPU temperature. | `Gpu temp:  52°C` |
| `gpu_nvidia_fan_speed` | panel · tooltip | NVIDIA fan, %; `off` when idle at 0. | `Gpu fan speed:  off` |
| `gpu_amd_usage` | panel · tooltip | AMD GPU utilization, %. | `Gpu usage:  9%` |
| `gpu_amd_mem_usage` | panel · tooltip | AMD VRAM occupancy (used/total), %. | `Gpu vram usage:  11%` |
| `gpu_amd_temp` | panel · tooltip | AMD GPU temperature (the `edge` sensor). | `Gpu temp:  48°C` |
| `gpu_amd_fan_speed` | panel · tooltip | AMD fan in RPM; `off` when idle at 0. | `Gpu fan speed:  off` |
| `gpu_amd_freq` | panel · tooltip | AMD GPU clock frequency (`sclk`). | `Gpu freq:  144 MHz` |
| `gpu_intel_usage` | panel · tooltip | Intel iGPU utilization, %. | `Igpu usage:  0%` |
| `gpu_intel_freq` | panel · tooltip | Intel iGPU clock frequency. | `Igpu freq:  300 MHz` |
| `gpu_intel_dec_usage` | panel · tooltip | Intel iGPU video-decoder utilization, %. | `Igpu decoder:  0%` |
| `screen_brightness` | panel · tooltip | Backlight level, %. | `Brightness:  70%` |

## Thermal & fans

`cpu_temp`, `hd_temp` (above) are temperatures too; grouped here in most themes.

| Token | Where | What it shows | Example row |
| --- | --- | --- | --- |
| `fan_speed` | panel · tooltip | Fan speed in RPM, one row per fan; `off` when stopped. | `Fan1:  off` |
| `fan_speed:pair` | tooltip | The same, two fans per row. | `Fan1: 2400   Fan2: off` |

## Batteries

| Token | Where | What it shows | Example row |
| --- | --- | --- | --- |
| `battery_sys` | panel · tooltip | System battery charge, %. In the tooltip the charge/discharge rate (or charge-limit) rides in the middle. | `Battery 0: -8W  71%` |
| `battery_mouse` | panel · tooltip | Wireless mouse battery, %. | `Logitech Mouse:  55%` |
| `battery_kbd` | panel · tooltip | Wireless keyboard battery, %. | `Keyboard:  80%` |

## Network

| Token | Where | What it shows | Example row |
| --- | --- | --- | --- |
| `net_speed` | panel · tooltip | Up/down throughput (adapts to orientation). | `Upload: 2K   Dload: 0` |
| `wifi_signal` | panel · tooltip | Wi-Fi signal strength, %. | `Signal:  72%` |
| `net_device` | tooltip | Active network interface name. | `Network: wlan0` |
| `net_ip` | tooltip | Local IP address. | `IP: 192.168.1.5` |
| `net_device_ip` | tooltip | Interface and IP together on one row. | `Network: wlan0 - 192.168.1.5` |
| `wifi_ssid` | tooltip | Connected Wi-Fi network name. | `Wifi: MyWifi` |
| `wifi_ssid_signal` | tooltip | SSID and signal together on one row. | `Wifi: MyWifi - 72%` |

## System

| Token | Where | What it shows | Example row |
| --- | --- | --- | --- |
| `uptime` | tooltip | Time since boot. | `Uptime: 3d 4h` |
| `load_avg` | tooltip | Load average (1/5/15 min). | `Load avg: 0.82 0.61 0.55` |
| `top_process` | tooltip | The heaviest CPU process and its share. | `firefox  12%` |
| `system_updates` | panel · tooltip | Count of pending package updates. | `System updates:  14` |
| `server_check` | panel · tooltip | Reachability of a configured host. | `Server check:  OK` |

---

## Separators

Between items you can drop a spacer instead of a metric:

- `separator_small` — a thin gap.
- `separator_big` — a wide gap.

They are layout, not data, so they carry no value.
