"""Runtime file locations, shared by the daemon, the `page` command and the applet.

Everything the daemon publishes for the widget lives under one per-user runtime
directory on tmpfs. The layout is not cosmetic — the applet drives its reads off
an inotify watch on the HTML directory (a FolderListModel), so that directory
must churn only when there is something new to show:

    <runtime>/            <- watched: the two HTML files, nothing else
        panel.html
        tooltip.html
        state/            <- not watched: page counter, geometry, lock
            geom
            geom-desktop
            page
            npages
            page.lock

The state files change on every wheel notch and every panel resize; keeping them
in a sibling means those writes never wake the watcher. The atomic-write tmp
files (`panel.tmp`) do land in the watched directory — a rename-over has to stay
on the same filesystem — but the applet's nameFilters keep them out of its model
and its debounce collapses the extra notification.

Kept stdlib-only and import-cheap: `pagestate` imports it, and that module is on
the `pirostats page next` path, fired once per mouse-wheel notch.
"""
import os
from pathlib import Path


def _runtime_dir() -> Path:
    """$XDG_RUNTIME_DIR/pirostats — tmpfs, per-user, cleared at logout, which is
    what per-user runtime state is for. The applet resolves the same directory
    independently via QStandardPaths' RuntimeLocation (which *is* XDG_RUNTIME_DIR
    on Linux), so the two sides agree without sharing a constant.

    The fallback covers the daemon started outside a systemd/PAM session (a bare
    ssh running `probe` or `render`); the applet never gets there, since without
    a session there is no panel to draw in.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "pirostats"
    return Path(f"/tmp/pirostats-{os.getuid()}")


RUNTIME_DIR = _runtime_dir()
STATE_DIR = RUNTIME_DIR / "state"

# Read by the applet (cat). The applet builds these paths itself from the same
# runtime directory, so renaming one means updating its `runtimeDir` users.
PANEL_FILE = RUNTIME_DIR / "panel.html"
TOOLTIP_FILE = RUNTIME_DIR / "tooltip.html"

# Written by the applet: "<usable_px> <glyph_adv_px> <vertical 0|1> <tooltip_adv_px>".
GEOM_FILE = STATE_DIR / "geom"
# Written by the applet only when it sits on the desktop (never from the panel or the
# pinned popup): "<tooltip_adv_px>", the real advance of one monospace glyph at the
# desktop widget's own font. A separate file because GEOM_FILE also carries the
# panel's edge and usable width, which the desktop must not overwrite. Removed by the
# applet when the widget goes away; tmpfs clears it at logout. Never cached.
DESKTOP_GEOM_FILE = STATE_DIR / "geom-desktop"
# Tooltip page counter, its wrap bound, and the flock that serializes concurrent
# wheel processes. See pagestate.
PAGE_FILE = STATE_DIR / "page"
NPAGES_FILE = STATE_DIR / "npages"
LOCK_FILE = STATE_DIR / "page.lock"


def ensure_dirs() -> None:
    """Create the runtime tree. Cheap enough (two stats on tmpfs when it already
    exists) to call from any writer rather than relying on startup order: the
    applet can publish a geometry, or a wheel notch bump the counter, before the
    daemon has ever run.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
