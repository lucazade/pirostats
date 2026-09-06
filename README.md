# PiroStats

System stats in your **KDE Plasma** panel, plus a rich, paginated tooltip — driven
by a lightweight Python daemon.

<table>
  <tr>
    <td align="center" width="50%"><img src="screenshots/panel-horizontal.png" alt="Horizontal panel + full stats tooltip" width="50%"><br><em>Horizontal panel + full stats tooltip</em></td>
    <td align="center" width="50%"><img src="screenshots/panel-vertical.png" alt="Vertical panel + full stats tooltip" width="50%"><br><em>Vertical panel + full stats tooltip</em></td>
  </tr>
  <tr>
    <td align="center" width="50%"><img src="screenshots/graphs.png" alt="Graphs page" width="50%"><br><em>Graphs page</em></td>
    <td align="center" width="50%"><img src="screenshots/process.png" alt="Top processes page" width="50%"><br><em>Top processes page</em></td>
  </tr>
  <tr>
    <td align="center" width="50%"><img src="screenshots/desktop-white-text.png" alt="Desktop mode, white text" width="50%"><br><em>Desktop mode — transparent, white text</em></td>
    <td align="center" width="50%"><img src="screenshots/desktop-black-text.png" alt="Desktop mode, black text" width="50%"><br><em>Desktop mode — transparent, black text</em></td>
  </tr>
</table>

PiroStats renders CPU, memory, drives, GPU, temperatures, batteries, network and
load as HTML that a bundled Plasma applet displays. The daemon runs in memory and
atomically writes the panel and tooltip to /tmp, and the applet just cats them, so
there are **zero process forks in the hot path**.

## Features

- **Panel** — compact live stats (usage bars/sparks, temperatures, battery, …),
  auto-fitted to the panel's size and orientation (horizontal or vertical).
- **Tooltip** — the full stats view on hover, grouped into sections.
- **Deep-dive pages** — scroll the mouse wheel over the widget to page through the
  tooltip: top processes, per-core CPU, listening connections, fastfetch system
  info, and history graphs (CPU / memory / GPU / network area charts). Enable and
  order them in the config.
- **Pin** — middle-click keeps the tooltip open as a persistent popup, so you can
  watch the graphs live without holding the pointer over the widget.
- **Desktop mode** — drop the widget straight onto the desktop for an always-on,
  conky-style readout. Choose Plasma's background or keep it transparent on the
  wallpaper, pick the text and outline colors for legibility over any image, and
  scroll to page through the views. Set it in the widget's *Appearance* page (the
  Desktop options appear only when it's on the desktop).
- **Auto light/dark** — follows the Plasma color scheme, hot-reloaded.
- **Per-machine overrides** — sensor mappings and item tweaks auto-detected from
  the DMI board/product name, so one config works across all your machines.

## Requirements

- **KDE Plasma 6**
- **Python 3.11+** (standard library only)
- A **Nerd Font** for the glyphs (the applet defaults to *NotoSansM Nerd Font Mono*).
  Pick it in the widget's *Appearance* page.
- Optional, per page/sensor: ss (iproute2) for the connections page, fastfetch
  for system info; psutil, pynvml (NVIDIA), and UPower/UDisks2 (via GDBus) for
  the corresponding hardware. AMD GPUs need nothing extra — they are read
  straight from `amdgpu`'s sysfs.

## Install

```bash
git clone https://github.com/lucazade/pirostats.git
cd pirostats
./install.sh
```

install.sh installs system-wide: the code tree under /usr/lib/pirostats, the
pirostats CLI in /usr/bin, the applet and icon, and the systemd --user service.
The file steps use sudo; enabling the service runs as you. Your settings live in
~/.config/pirostats and are never touched — see [Configuration](#configuration).

Then add the widget: **right-click a panel → Add Widgets → search "PiroStats"**.

Re-run install.sh to upgrade (it restarts plasmashell to reload the applet).
Remove everything with uninstall.sh.

## Configuration

The installed tree under **/usr/lib/pirostats/** holds read-only defaults; your
overrides go in **~/.config/pirostats/**. Copy a default across and edit it —
everything hot-reloads.

- **config.toml** — behavior and data only (thresholds, glyphs, item order,
  hardware). Each surface (panel, tooltip) is a set of typed sections (cpumem,
  thermal, drives, gpu, batteries, io, load) with an order and per-section items.
  A config.toml in ~/.config/pirostats replaces the shipped one; run
  **pirostats list-items** for the valid metric:form names — it also prints where
  each one can go. An item listed on a surface it isn't meant for (a bare
  `cpu_usage:spark`, which carries no label, in a tooltip section) is dropped
  with a warning on the daemon's log, as is a typo'd name.
- **Machines** — got more than one PC? The shipped machines.toml is just a how-to;
  list your machines in ~/.config/pirostats/machines.toml, each with a detection
  rule and its tweaks. The one matching the current host is merged on top of the
  config — one synced config works everywhere.
- **Style** — the shipped style/style-dark.css and style-light.css hold colors and
  spacing; drop a same-named file in ~/.config/pirostats/style/ to override it.
  config.toml never carries colors. Glyphs live in style/icons.toml, labels in
  lang/en.toml.

Everything hot-reloads: editing the TOML/CSS, switching the Global Theme, or moving
the panel between edges re-adapts the daemon on the next poll — no restart needed.

## CLI

```bash
pirostats render                    # render to text in the terminal (no daemon)
pirostats render --page processes   # render one tooltip deep-dive page (processes|cpu_cores|connections|fastfetch|graphs)
pirostats probe                     # hardware discovery + raw readings
pirostats list-items                # valid metric:form tokens
pirostats profiling                 # per-item timing and cache state
systemctl --user status pirostats   # the live daemon
```

## How it works

The daemon's pipeline (in src/: sensors → formatter → render_model → mono_render)
renders the stats as monospace-aligned HTML; the applet in plasmoid/ displays it
and publishes the live panel geometry back to the daemon, which auto-fits the bars
and sparks to it. See [docs/](docs/) for the design (DESIGN.md), layout
(LAYOUT.md), item catalogue (ITEMS.md) and performance (PERFORMANCE.md) notes.

## License & credits

GPL-2.0-or-later — see [LICENSE](LICENSE).

The Plasma applet under [plasmoid/](plasmoid/) builds on **Command Output** by
Chris Holland (Zren) — <https://github.com/Zren/plasma-applet-commandoutput> —
also GPL-2.0-or-later. See [NOTICE](NOTICE).
