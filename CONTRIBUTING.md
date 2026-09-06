# Contributing to PiroStats

Thanks for taking the time. This file is the short version of what the code
expects from a change; the reference docs under `docs/` carry the reasoning,
and `CLAUDE.md` is the same map written for an AI coding agent — read whichever
suits you, they describe one codebase.

## Before you write code

Open an issue first for anything bigger than a fix. PiroStats is opinionated
about a few things — one clock, zero forks in the hot path, no colors outside
CSS — and it is cheaper to find out in an issue than in a review that a design
runs against one of them.

Hardware support is the exception: a pull request that adds a sensor family
nobody here owns is welcome cold, because the only person who can write it is
someone with the hardware.

## Running it

There is no build step and nothing to install: `./pirostats` prepends `src/` to
`sys.path`, and the config is resolved relative to the source, so the repo works
wherever you cloned it. Python 3.11+ (stdlib `tomllib`), plus `psutil`.

```bash
python3 -m pytest tests/ -v          # the whole gate
./pirostats probe                    # every item, raw readings, no daemon
./pirostats render                   # the HTML, stripped to text
./pirostats list-items               # every valid metric:form token
./pirostats profiling                # per-item timing and cache state
```

`pytest` is the gate — run it before you open the PR, and CI runs the same
thing on 3.11 and 3.13. Two of its checks shell out to optional tools and
**skip when the tool is absent**, which means a bare checkout can look green
while they never ran:

```bash
pip install vulture ruff             # or: pacman -S vulture ruff
```

`vulture` is the dead-code gate: a helper, constant or config field that
nothing reads fails the suite instead of accumulating. If your name is only
reachable through a runtime `getattr`, add it to `tests/vulture_whitelist.py`
**with the lookup that reaches it** — that file is for real dynamic lookups,
not for silencing a finding. `ruff` is the lint gate; its config lives in
`ruff.toml` and each ignore there is a deliberate house style with the reason
next to it.

## Adding an item

An item is not a name but a pair, `metric:form` (`cpu_usage:bar`,
`hd_temp:pair`). The *what* axis lives in `src/metrics.py`, the *how* axis in
`src/forms.py`, and `src/registry.py` dispatches the pair to a renderer. Read
those three together before you start, then `docs/ITEMS.md` for how the result
should read to a user.

A new metric usually touches all of these — the last two are the ones people
forget:

- `src/metrics.py` — the metric, its `needs`, its hardware `gate`
- `src/registry.py` — the `(metric, form)` entry, built from `src/items.py`'s
  cell factories
- `src/sensors.py` — the `HardwareInfo` path(s), the `Readings` field, the read
  itself in `collect`
- `lang/en.toml` (label) and `style/icons.toml` (glyph)
- `src/config.py` + `config/config.toml` — thresholds, notification defaults,
  and the item in the shipped surface lists
- `docs/ITEMS.md` — one row in the catalogue
- `formatter._maxed_readings` — **the tooltip width is derived, not
  configured**, so a new width-driving field must be maxed there or it can push
  the layout wider than the width every page was sized to. A registry-driven
  test in `tests/test_formatter.py` fails if you skip it
- `tests/golden/` — if the render changes, confirm the diff is what you
  intended, then regenerate: `UPDATE_GOLDEN=1 python3 -m pytest
  tests/test_golden_render.py`

Placement is derived and enforced: an item's real surfaces are the intersection
of the form's and the metric's, and `config.py` drops a token listed on a
surface it does not belong to, with a warning. You do not need to declare it
twice.

## House rules that are easy to trip over

- **All text in the repo is English** — comments, docstrings, stderr, CLI help,
  notifications, docs. Comments describe the current state, not the history of
  how it got there.
- **Comments explain the why.** The what is in the code. If a constant is a
  measurement or a compromise, the number's reason belongs next to it.
- **Glyphs go in `style/icons.toml`, labels in `lang/*.toml`** — never in
  `config.toml`. Write Nerd Font PUA codepoints with a small Python script;
  editors mangle them.
- **No colors in `config.toml`.** It is data and behavior; the state classes
  (`.good`/`.warn`/`.crit`) are assigned in Python, the colors only in CSS. A
  layout change in `style/style-dark.css` must be mirrored in
  `style-light.css`, which differs from it in colors alone.
- **The runtime directory is a contract**, not a scratch dir: only `panel.html`
  and `tooltip.html` may sit in it, because the applet has an inotify watch
  there and anything else costs a repaint. Everything that churns for other
  reasons goes in `<runtime>/state/`. See `src/runtime.py`.
- **The poll loop has a budget.** Read `docs/PERFORMANCE.md` before adding a
  sensor read: no forks in the hot path, TTL-cache anything slow, and know
  roughly what your read costs. Say the number in the PR.
- **Qt's RichText CSS subset is much smaller than a browser's.** The reference
  block at the top of `style-dark.css` says what actually works, and
  `tools/qt_shot.py` renders HTML with the real engine so you can look instead
  of theorize.

## Pull requests

One logical change per PR. Write the commit message the way the log does:
an imperative subject line, then a body explaining *why* this shape and not the
obvious alternative — the reviewer can read the diff, what they cannot read is
the option you rejected.

If your change depends on hardware, say plainly what you tested it on and paste
the `./pirostats probe` output from that machine. Nobody else can verify a
sensor they do not own, and a maintainer merging blind is worse for you than a
short wait.

By contributing you agree that your work is licensed under GPL-2.0-or-later,
like the rest of the project.
