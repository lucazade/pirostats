# Performance analysis

Why PiroStats costs almost nothing to run, and where the little cost there is
lives. Read this before touching the poll loop or a sensor's caching — the
numbers and the design rationale behind them are here.

## Methodology

Phases are timed with `time.monotonic()` (and `cProfile` for per-function
breakdowns) running the daemon's real code, not synthetic benchmarks. Absolute
numbers below are from a typical desktop; they shift with the machine (core
count, NVMe model) but the relative bottlenecks hold.

`pirostats profiling` reproduces this on demand: it measures every phase of one
real iteration — config load, hardware discovery, formatter init, `collect()`
with a cold vs. warm cache per section/item, the per-poll bookkeeping (mtime
check, notifier, atomic write), and each cache's freshness — writing to
`/tmp/pirostats_profile_*` so it never touches a running daemon's files.

## Cost per phase (current state)

**Startup** (first write to `/tmp/`), ~150 ms total:

| Phase | Time |
|---|---|
| `load_config` | ~3 ms |
| `discover_hardware` | ~45–60 ms |
| &nbsp;&nbsp;`_find_battery_sys` / `_find_peripherals` (UPower over GDBus) | most of it |
| &nbsp;&nbsp;`_find_cpu_temp` / `_find_hd_temps` / `_find_fans` / `_detect_nvidia` / `_detect_amd_gpu` / `_detect_net_device` | 1–2 ms each |
| First `collect` + format + write | ~60–90 ms |

`discover_hardware` dominates startup — mostly the UPower device enumeration over
GDBus. It's paid once, so it isn't a target; the loop below is what runs forever.

On top of that, ~150 ms of one-time imports, almost entirely
`gi.repository.Notify` (GObject introspection loading system typelibs). A fixed
cost paid once; irrelevant on a process that runs for hours.

**Loop iteration** (`poll_interval = 1.5s`): ~1.5 ms of real work with a warm
cache, then the loop sleeps the remaining ~1.5 s. Rendering (`format_panel` +
`format_tooltip`) is <1 ms; the two atomic writes are <1 ms. The whole cost is
`collect()`, and within it a handful of I/O reads (see below).

## Tooltip pages: cost only when shown

The daemon writes the tooltip every poll regardless of hover (it can't know), but
only the **active** page's body is built, so pages you're not on cost nothing.
Per-page body-build cost (`pirostats profiling` → *TOOLTIP PAGES*, cold):

| Page          | Body build | Notes |
|---------------|-----------:|-------|
| `processes`   | <0.5 ms | reuses the `/proc`-diff sample; 15 rows, `/proc/[pid]/cmdline` names, COMMAND column elastic to the tooltip width |
| `cpu_cores`   | <0.5 ms | braille from per-core history; the `/proc/stat` scan is in `collect`, gated on the page being enabled |
| `connections` | ~13 ms | `ss -4tlnp` subprocess, only while shown |
| `fastfetch`   | ~24 ms | subprocess under a pty, **30 s TTL** so it's not re-run every poll |
| `graphs`      | ~33–70 ms | CPU + memory + (if present) GPU + network area charts, PNGs rasterized in pure stdlib (`chart.py`); only while shown |

Two per-page sampling notes:
- **`top_process`** resamples `/proc` **every poll while on the page** (its own
  prev-state), refreshing at `poll_interval` instead of the panel's 15 s TTL —
  the extra ~15–20 ms `/proc` scan is paid only while that page is active, plus a
  ~0.04 ms `/proc/[pid]/cmdline` read for the 15 shown rows to name them.
- **`cpu_cores`** keeps a per-core history only while the page is in
  `pages.order`; otherwise the per-cpu `/proc/stat` parse is skipped entirely.

Page switches are picked up within `poll_interval`: the sleep is stepped (0.1 s)
and re-renders the tooltip as soon as the counter file changes — a tiny file
read, not a re-poll.

## Where the loop's cost goes

With a warm cache the loop's work is a few I/O reads:

| Function | Time | Note |
|---|---|---|
| `_read_path_millideg` hd_temp (NVMe) | ~5 ms | NVMe controller responds slowly (APST wake-up), so behind a TTL cache |
| `psutil.cpu_freq()` | ~2–4 ms | opens a file per physical core |
| `_read_mem_usage` / `_read_swap_usage` / `_read_net_speed` (psutil) | ~0.4–0.6 ms each | |
| everything else | <0.3 ms each | negligible |

Every bottleneck is **I/O** — hwmon hardware, kernel `/proc`, the occasional
subprocess. The pure-Python layer between them is transparent: regex is
pattern-cached, dynamic `getattr`/f-strings/dict ops are nanoseconds, and
building ~20 rows into HTML is <1 ms. Don't optimize Python here; optimize the
reads.

## Design: how the cost is kept down

**TTL caches on slow or steady sensors.** Anything that's expensive to read and
doesn't change within a poll sits behind a per-sensor TTL, so its full cost is
paid once per interval instead of every 1.5 s: hd_temp (5 s), GPU (3 s),
batteries (30 s), `top_process` (15 s), Intel GPU usage (30 s), `fastfetch`
(30 s). The pattern (`sensors.py`) reads only when `monotonic() - ts >= TTL`.
Two rules learned the hard way, both load-bearing:
- The "not read yet" sentinel is `float("-inf")`, **not** `0.0` — `monotonic()`
  counts from system boot, so a `0.0` sentinel would gate the first read on
  *system uptime* exceeding the TTL, leaving sensors blank for seconds after a
  login early in the boot.
- A cache holding a `None` (a diff-based sensor with no previous sample yet)
  bypasses its TTL and retries every poll until it has real data, instead of
  caching the emptiness for a full interval.

A one-time boot-watch log (`BOOT_WATCH` in `daemon.py`) records when each
TTL-cached sensor first produces a value during the first 90 s of the process,
then turns itself off — a permanent, zero-steady-state guard so a regression here
is visible in `journalctl --user -u pirostats` without re-instrumenting.

**The tooltip width is computed once, not every poll.** `formatter.canonical_width`
renders the full page against maxed-out readings to find its widest form (see the
tooltip-width convention in `CLAUDE.md`) — roughly one extra tooltip build. It's
memoized on a small reading signature (disk mounts + totals, net/wifi identity,
RAM size), so on the common poll it's a cheap key compare, recomputing only when
the item set actually changes (a disk mounted, hardware rescanned).

**No subprocess in the hot path.** Forks are the expensive thing (see the
`_detect_nvidia` note below), so `collect()` is fork-free at steady state:

- hardware presence via sysfs (NVIDIA/AMD/Intel by PCI vendor id
  `0x10de`/`0x1002`/`0x8086`, class `0x03` display);
- `battery_sys` reads `/sys/class/power_supply/BAT*/`; the mouse/keyboard
  batteries and the UPower device enumeration go over **GDBus** (`Gio` on the
  system bus) — a property read, no fork;
- NVIDIA GPU stats via **`pynvml`** (`python-nvidia-ml-py`, ~0.3 ms, read every
  poll); `gpu_intel_*` via sysfs + `/proc/[pid]/fdinfo` DRM counters; `gpu_amd_*`
  via plain amdgpu sysfs (`gpu_busy_percent`, `mem_info_vram_*` and the card's
  hwmon, ~0.5 ms, read every poll — no library, no fork, hence no TTL cache);
- `system_updates`/`server_check` read a plain file written by an external
  checker (a `--user` timer, outside the repo) instead of running `pacman -Qu`
  or `ping` in the loop.

The only forks left are **fallbacks** when a library or service is absent —
`nvidia-smi` (behind a 3 s TTL, if `python-nvidia-ml-py` is missing) and the
`upower` CLI (if the UPower D-Bus service isn't reachable) — plus the
page-only `ss`/`fastfetch`, which run only while their tooltip page is shown.

**sysfs beats a fork by three orders of magnitude.** `_detect_nvidia` once ran
`lspci -nn` and took ~2000 ms, dominating startup; the equivalent sysfs walk
(`/sys/bus/pci/devices/*/vendor` == `0x10de` with a display class) is ~2 ms. The
same lesson drives every detection above.

## Design: table-free rendering (the big plasmashell win)

The single most important rendering decision. With the tooltip **open**,
`plasmashell` climbed to 15–20% CPU (≈300 ms of CPU on every 1.5 s refresh);
closed, ~1%.

**Diagnosis** (measured with `pidstat -p $(pidof plasmashell) 1` while hovering,
serving hand-built HTML variants via the same `cat` command, one visible value
mutated every 1.5 s to force a re-render):

| Tooltip content (one value changing every 1.5 s) | plasmashell CPU, hovering |
|---|---|
| 14 `<table>`s | ~30% in bursts |
| same content, tables flattened to rows | ~1% |
| `<style>` present, no tables | ~1% |
| a **single** 33-row `<table>` | 85–100% (worse) |
| tables without `width%` attributes | ~50% (Qt computes natural widths) |

**Cause.** The live Qt Quick RichText engine re-balances `<table>` columns from
scratch on every content change, **super-linearly in table size** (rows ×
columns). Ruled out (all verified): popup resizing, the `<style>` block, blur,
`width%`. Even a headless `QTextDocument` benchmark misleads — it lays the same
document out ~40× cheaper than the live `QQuickText` path, so this is only
reproducible in the running scene.

**Consequence.** `mono_render.py` serializes the tooltip and vertical panel
**without any `<table>`**: columns are aligned with `&nbsp;` padding computed in
Python once per render (monospace font → characters = pixels), values aligned to
a shared right edge. The 8 px inset lives in the plasmoid's `Text` padding, not a
padding table. Result: tooltip-open CPU dropped from ~20% to ~1–3%. The cost was
never in generating the HTML (sub-ms) but in Qt re-digesting a table every frame
— so the table-free approach is load-bearing, not stylistic. Do not reintroduce
`<table>` on any render path.

_Methodological note: `pidstat -h` puts `%CPU` in `$8`; `$(NF-1)` is the core id,
not the percentage — easy to misread into the opposite conclusion._

## The applet reads on a watch, not a clock

The applet has **no timer of its own**. It puts an inotify watch on the daemon's
runtime directory (a QML `FolderListModel`) and `cat`s the two HTML files when
they change, coalescing the burst of one poll (two files, each a rename-over) into
a single read with a 50 ms debounce. So the only rate in the system is
`display.poll_interval`: a frame reaches the panel as soon as the daemon writes
it, never aging up to a tick first, and there is no second clock to alias against.

It used to run a free-running 1500 ms `Timer`, hardcoded and unrelated to when the
daemon wrote — the panel showed data aged 0…1500 ms, and any `poll_interval` ≠
1500 ms dropped whole frames (the applet sampled a file that changed at a
different rate). The watch removes the aliasing outright.

The fork count is unchanged, not eliminated: reading is still one `cat` per write
(Qt blocks `XMLHttpRequest` on `file://` without a process-env flag an applet
can't set), so it's one fork per poll instead of one per tick — now scaling with
`poll_interval` rather than fixed.

**The tooltip read stays gated on hover/pin; the panel's does not.** Qt reparses
and re-lays-out the tooltip's RichText on every text change, so re-reading it while
nobody is looking is pure waste. Measured cost of that reparse, `pidstat -p
$(pidof plasmashell) 1` on this machine (table-free tooltip, ~15 KB):

| plasmashell | %CPU |
|---|---|
| tooltip closed (panel only, every poll) | ~1.1% |
| tooltip pinned (reparsed every poll) | ~4.9% |

So the gate saves ~3.8% of a core whenever the tooltip is closed — worth keeping,
but nowhere near the **~20%** an earlier comment claimed. That figure predates the
table-free rewrite above (it was measured against HTML tables, the very thing that
rewrite removed) and no longer describes any render path we ship. The gate is
right regardless of the number: not computing what nobody is looking at needs no
CPU argument. The daemon, by contrast, renders the tooltip **every** poll no
matter what — it can't see the mouse — so the file is always fresh and a hover has
nothing to catch up on.
