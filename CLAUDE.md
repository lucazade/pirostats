# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python daemon that renders KDE Plasma panel + tooltip system stats as HTML. It runs in memory and atomically writes `<runtime>/{panel,tooltip}.html` (`<runtime>` = `$XDG_RUNTIME_DIR/pirostats`, see `src/runtime.py`); the bundled Plasma applet (`plasmoid/`, id `com.github.lucazade.pirostats`) watches that directory and `cat`s the files when they change. **There is one clock in the system, `display.poll_interval`** — the applet has none, so nothing aliases and no frame ages waiting to be noticed. See `README.md` for the full user-facing story — this file covers only what you need to work productively in the code.

## Commands

```bash
# Run all tests
python3 -m pytest tests/ -v

# Run a single test file / test
python3 -m pytest tests/test_config.py -v
python3 -m pytest tests/test_formatter.py -k some_test_name

# Regenerate the golden HTML snapshots after an INTENDED render change
UPDATE_GOLDEN=1 python3 -m pytest tests/test_golden_render.py
# (goldens live in tests/golden/{panel_h,panel_v,tooltip}.html)

# Dead code: the same sweep test_deadcode.py gates, run by hand for the report
vulture src/ tests/ pirostats tests/vulture_whitelist.py --min-confidence 60

# Lint: the same check test_lint.py gates (config in ruff.toml), run by hand
ruff check .

# One-shot diagnostics / render (no daemon, readable in terminal)
./pirostats probe --config config/config.toml      # every item, raw readings
./pirostats render                                 # HTML stripped to text
./pirostats render --component tooltip --format html   # -> /tmp/pirostats_render_tooltip.html
./pirostats render --component panel --layout horizontal|vertical  # force orientation
./pirostats render --page processes|cpu_cores|connections|fastfetch|graphs  # a tooltip deep-dive page (any page, even one not in pages.order)
./pirostats profiling --config config/config.toml  # per-item timing, cache state
./pirostats list-items                             # authoritative list of valid metric:form tokens

# Live daemon
systemctl --user status pirostats
journalctl --user -u pirostats -f
```

Run everything from the repo root; `./pirostats` prepends `src/` to `sys.path`, so there is no package install step in dev. `config.py`/`daemon.py` resolve `config/config.toml` relative to `__file__`, so the repo can live anywhere.

No build step — pure Python 3.11+ (stdlib `tomllib`), the test suite is the gate. Two of its checks shell out to optional dev tools and **skip when the tool is absent**, so a bare checkout still runs everything else: `vulture` (dead-code gate) and `ruff` (lint gate, config in `ruff.toml`). Both are run as tests, so `python3 -m pytest tests/` covers them — see Testing notes. `pacman -S vulture ruff` / `pip install vulture ruff`.

## Architecture: the metric × form model

The central idea is that an item is **not** a flat name but a pair `metric:form` (e.g. `cpu_usage:bar`, `hd_temp:pair`). Three files own the two axes and their dispatch — read all three together before touching item behavior:

- **`metrics.py`** — the *what* axis. A metric declares only what's intrinsic and form-independent: `needs` (which sensor helper feeds it), `gate` (when the hardware is present), `surfaces` (panel/tooltip eligibility), and which generic `forms` it supports. Glyph, label and thresholds are deliberately NOT here.
- **`forms.py`** — the *how* axis: `Form` enum (value/bar/spark/braille/pair/…), `Surface` flags, and `FORM_SURFACES` (which surfaces a form is valid on). A generic form declares no skeleton — how a row lays out is read off its cells by `mono_render._plan_row` (`docs/LAYOUT.md`); `Shape` exists only as the value of a metric's `intrinsic_shape`.
- **`registry.py`** — the dispatch `_RENDER[(metric, form)] -> GroupFn`. Regular entries are built from the declarative cell-factory library in **`items.py`** (`row`/`per` + `label`/`value`/`spark`/…); irregular ones (combos, string joins, batteries, adaptive layouts, own skeletons) are explicit exception functions `(f, ident, r, tooltip) -> list[Row]`. `registry.py` also holds the **token layer** (`parse` of `"metric[:form]"`) that formatter/config/sensors consume.

Placement is **derived, not declared**: an item's real surfaces = intersection of the form's `FORM_SURFACES` and the metric's `surfaces`. It's also **enforced**: `config._drop_misplaced_items` drops a token listed on a surface its surfaces don't admit (with a stderr warning), so `cpu_usage:spark` in a `[tooltip.*]` doesn't render a label-less trace there. `_drop_unknown_items` does the same for typos.

The two-axis identity flows through as an `Ident` (metric + form), which the cells turn into the CSS class `.item-<metric>.form-<form>`. The `BAR` form is orientation-adaptive: it renders as a vertical column in the horizontal panel and an inline bar in the vertical panel, picked in `registry._form_token` — this is also where `[panel_horizontal]`/`[panel_vertical]` config merges land.

## Rendering pipeline

`sensors.py` (collect readings) → `formatter.py` (`PanelFormatter`: builds `Row`/`Cell` per item via the registry, `format_panel`/`format_tooltip`) → `render_model.py` (Cell/Row/Block model, grouping into blocks) → `mono_render.py` (**table-free** HTML: columns aligned with `&nbsp;` monospace padding, not `<table>`). The table-free approach is load-bearing, not stylistic: real `<table>`s cost ~20% plasmashell CPU in Qt's RichText engine (see `docs/PERFORMANCE.md`); monospace padding gives identical columns at near-zero cost. The horizontal panel is a single inline `<span>` row (`render_row_inline`).

All visual style lives in `style/style-{dark,light,overlay}.css`, hot-reloaded like the TOML. `config.toml` stays pure data/behavior (thresholds, glyphs, order, hardware) — never colors. State classes (`.good`/`.warn`/`.crit`/`.active`) are assigned in Python from value+thresholds, but the actual color is CSS-only. `style-light.css` mirrors `style-dark.css`'s selectors 1:1 and differs only in colors — **a layout change in one must be mirrored in the other**.

## Config model

`config.py` loads `config/config.toml` + `config/machines.toml` into a `Config` dataclass. Layering, applied in order:
1. `config.toml` defaults (typed sections: `cpumem`, `thermal`, `drives`, `gpu`, `batteries`, `io`, `load`; each has `order` + per-section `items`).
2. **Machine override** (`machines.toml`) — the block whose `[<name>.detect]` matches the host's DMI (`/sys/class/dmi/id/`) is merged in; there's no selector, the hardware picks it (`config.detect_machine`), and each block specifies only deltas. Grammar: `items` (replace) / `items_add` / `items_remove` / `order_add`.
3. **Panel orientation override** — `[panel_horizontal]`/`[panel_vertical]` merge on top using the same grammar, chosen from the auto-detected panel edge.
4. **Auto-fit** (`config._auto_fit_panel`) — panel bar/column/spark dimensions are derived at runtime from the live panel geometry the plasmoid publishes to `<runtime>/state/geom`, mirrored to `~/.cache/pirostats/geom` across reboots.

Final item visibility = section membership × hardware gate. Empty sections collapse entirely (title + separator included).

**The file is laid out by scope**, and a key belongs to the narrowest scope that owns it: `[display]` (global: `poll_interval`, `history_interval`, `overlay`) → `[panel]` → `[tooltip]` → `[pages]` → the form sizes (`bar_*`/`column_*`/`spark_*`/`braille_*`, grouped by form since a form knob is orthogonal to the surface) → thresholds and the rest. `history_interval` stays global because spark, braille *and* the graphs page all read the one history buffer, so none of them owns it.

A **scalar beside a surface's `order` is an option of that surface**, not a section: `_parse_surface` only walks the keys `order` names and steps over scalars, and the machine/orientation overrides deep-merge before the parse — so an option rides them exactly like items do (`[panel] glyphs = true` + `[panel_horizontal] glyphs = false`), and nothing downstream re-checks the orientation. `Surface.glyphs` is the only such option today; it's **panel-only** (a panel label IS the glyph, so dropping it drops the cell, while a tooltip label is glyph+word — `cfg.tooltip.glyphs` is never read).

## Conventions specific to this repo

- **The runtime directory is a contract, not a scratch dir** (`src/runtime.py`). Only `panel.html` and `tooltip.html` may live in `<runtime>/`, because the applet has an inotify watch on it and any write there costs a repaint's worth of work. Anything that churns for other reasons — the page counter, the published geometry — goes in `<runtime>/state/`, which is invisible to the watcher (inotify reports a directory's own entries, not a subdirectory's; verified). The one-shot `render`/`profile` files stay in `/tmp` for the same reason, plus being human-facing. **Adding a runtime file means deciding which side of that line it's on.** Both the daemon and the applet derive `<runtime>` independently (`$XDG_RUNTIME_DIR` / QStandardPaths' `RuntimeLocation`), so it appears in no config file and can't drift.
- **Network identity and Wi-Fi come from netlink, not `ip`/`iw`**: `src/netlink.py` holds the stdlib framing, the self-reopening socket and the route lookup (`Routes.route_to`, the `ip route get` equivalent); `src/nl80211.py` the Wi-Fi queries on top of it. Both sockets live in `DaemonState`; a round trip is well under 1ms against ~3ms per fork. Everything is read every poll, no TTL: route + SSID are the `net_info` capability, the live link (signal, per-antenna dBm, tx/rx rate + MCS/NSS) the `wifi_link` one. Replies are matched on the netlink sequence number, so a late answer can't be taken for the current request.
- **Glyphs live in `style/icons.toml`, labels in `lang/*.toml`** — NOT in `config.toml`. To write a Nerd Font PUA glyph, use a Python script (Edit tools mangle the codepoint).
- **All text in the repo is English** (comments, docstrings, stderr/print, CLI help, notifications, README/docs). Describe current state, not refactor history.
- The tooltip is **paginable via mouse wheel**: page 0 is the full stats view, deep-dive pages (`processes`/`cpu_cores`/`connections`/`fastfetch`/`graphs`) are configured/ordered in `pages.order`. `pages.py` renders them; a page's command/body is built only while that page is shown. Wheel/click run `pirostats page next|prev` / `pirostats click`, hardcoded as the applet's command defaults (`plasmoid/package/contents/config/main.xml`), not the removed Actions config page. (The panel/tooltip `cat`s are NOT there: they carry the runtime path, which isn't knowable at build time, so `main.qml` derives them.) **Middle-click pins** the tooltip: the applet opens the same `tooltipText` in a persistent popup (its full representation) so you can watch live pages without hovering — handled in the applet's QML, not the daemon. Every page sizes to one width, `display.tooltip_width`, resolved at runtime to the **main page's canonical (max) width** (see the tooltip-width convention below) so nothing resizes across pages or as content fluctuates; the `processes` page's COMMAND column is elastic (grows to fill that width, names from `/proc/[pid]/cmdline`), and the `graphs` page PNGs are sized from `graph_width` (= `tooltip_width` × the tooltip glyph advance the plasmoid publishes, 4th field of `<runtime>/state/geom`).
- The **`graphs` page** stacks systemmonitor-style history area charts: CPU, memory, the active GPU (usage area + decoder line overlay; Nvidia preferred over Intel), and network (download area + upload line, auto-scaled to the window peak — no percent y-labels, rates in the legend). `chart.py` rasterizes a small PNG area chart (grid + y-axis labels + fill + antialiased line, optional overlay line) in **pure stdlib** (`zlib`+`struct`, no Pillow/matplotlib), embedded as a `data:` URI — Qt RichText takes a raster `<img>` (SVG crashes plasmashell). Only the plot is in the image; labels + values are HTML, threshold-colored. Chart colors live in `chart.py` (baked pixels can't read CSS), a theme-agnostic palette. Rendered by `formatter.format_graphs` (same pattern as `cpu_cores`/`top_process`), redrawn only while shown. History is the shared cpu/mem/GPU/network buffers, extended to `pages.graph_history_length` (60) while the page is enabled; GPU/network are sampled by `sensors._sample_gpu_history`/`_sample_net_history` and their caps requested in `registry.needed_capabilities`. The fill is written inline into the transparent buffer with the grid drawn on top (no per-pixel blend in the fill loop).
- **The tooltip width is derived, never configured.** `display.tooltip_width` is the main page's canonical (max) width: `formatter.canonical_width` renders the full page against `_maxed_readings` (every volatile field at its bounded max; memoized on a small reading signature), and `config.apply_canonical_width` floors it at `TOOLTIP_WIDTH_FLOOR` and derives `graph_width`. The deep-dive pages and the graphs PNG all size to it, so one width holds and nothing resizes as values change. **Adding a width-driving item or field means maxing it in `_maxed_readings`** — otherwise the registry-driven guard in `tests/test_formatter.py` fails: it renders every tooltip item alone and asserts `canonical_width` covers a wide render. The raw string fields (`net_device`, `wifi_ssid`) are middle-truncated (`_NETDEV_MAX`/`_SSID_MAX`) so they stay bounded.
- **The Qt RichText CSS subset is smaller than a browser's** — the reference block at the top of `style-dark.css` documents what works. Use `tools/qt_shot.py` to render HTML/QML to a faithful PNG with the real Qt engine before theorizing about layout bugs.
- Inspection overlay is a config key: `overlay = true` under `[display]` in `config.toml` (hot-reloaded) adds `style-overlay.css`'s per-cell diagnostic backgrounds on the live panel/tooltip — never hand-edit CSS. There's no CLI flag; watch it on the widget.
- The Plasma applet is **bundled in `plasmoid/`** and installed by `install.sh` (kpackagetool6). It's derived from Chris Holland (Zren)'s Command Output (GPL-2.0-or-later; see NOTICE) and carries the PiroStats changes: inline HTML/CSS rendering, live geometry publishing, wheel paging, the pinned popup, hardcoded commands, configurable tooltip font/padding.

## Reference docs

Read the relevant one before working in the matching area — they carry the rationale and measurements that the code comments don't:

- **`docs/LAYOUT.md`** — before touching `mono_render.py`: the serializer reduces every row to **five layout plans** (`kind` in `_plan_row`); this explains which and why.
- **`docs/ITEMS.md`** — plain-language catalogue of every item and the row it renders; the human companion to the `metric:form` token naming.
- **`docs/DESIGN.md`** — the "why Python, why this shape" rationale (the original bash script's power draw, design goals).
- **`docs/PERFORMANCE.md`** — timing methodology, TTL-cache design, the hot-loop cost table, the table-free rationale. Consult before changing anything in the poll loop or a sensor's caching.

## Testing notes

Tests cover pure logic only: `config.py` (machine detect/merge), `formatter.py`/`render_model.py` (Row/Cell construction, block grouping, threshold classes), `mono_render.py` (monospace serialization), plus golden HTML snapshots. `sensors.py`/`daemon.py` are **not** covered — they need real I/O (sysfs, UPower, psutil). When a render change trips `test_golden_render.py`, confirm the diff is intended, then regenerate with `UPDATE_GOLDEN=1`.

`test_deadcode.py` is a **dead-code gate**: vulture over `src/` + `tests/` at min-confidence 60, so a helper, constant or config field that nothing reads fails the suite instead of accumulating. Vulture is a static reader, so a name reached only through a runtime `getattr` (the thresholds by item name, the indexed `SensorOverrides`, the `Readings` fields the registry names as strings) looks dead to it: those live in `tests/vulture_whitelist.py`, each with the lookup that reaches it. Add to the whitelist ONLY for a real dynamic lookup — otherwise delete the code. `pip install vulture` / `pacman -S vulture`; the test skips if it's absent.

`test_lint.py` is a **lint gate**: `ruff check .` from the repo root, config in `ruff.toml`. It runs ruff's default rules (E4/E7/E9 + Pyflakes `F`) — the value is `F`, the real bugs a one-file reader can see (dead import, unused local, undefined/redefined name) that vulture doesn't cover the same way. `ruff.toml` silences only the pycodestyle style rules this codebase breaks on purpose (assigned lambdas, dense semicolons, the `page` fast-path's below-guard import — each ignore is commented there), and E501 (line length) is left out entirely since the comments run wide by design. A failure means either fix it or, if it's a deliberate house style, add the rule to `ruff.toml`'s ignore list **with the reason**. The test skips if ruff is absent.
