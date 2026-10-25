"""
Tooltip pages: the mouse wheel over the plasmoid cycles the tooltip through a
few pages. Page 0 is the normal full stats view (rendered by the formatter);
the others are on-demand deep dives whose body is the text output of an external
command (fastfetch, ps, ss) wrapped in the same tooltip shell.

State lives in a tiny file (see daemon.PAGE_FILE) written by `pirostats page
next|prev` (the wheel commands) and read by the daemon loop each poll. The click
command (`pirostats click`) reads the same page, so its action can be made
page-aware later; today every page opens plasma-systemmonitor (see click_command).
"""
from __future__ import annotations

import html
import re
import shlex
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass

# The plasmoid's formatOutputText converts SGR color escapes (\033[..m) to HTML
# and is applied to the tooltip too, so we KEEP color codes and let the widget
# render them. This only strips the noise around them: carriage returns and
# non-SGR escapes (cursor moves, OSC) a pty run adds, which would show as litter.
_STRIP = re.compile(
    r"\r"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC ... (BEL or ST)
    r"|\x1b\[[0-9;?]*[@-ln-~]"              # CSI with any final byte except 'm' (SGR)
    r"|\x1b[=>NOPX^_c]"                     # a few standalone escapes
)
# SGR color codes alone — used to test whether a line is visually blank.
_SGR = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class Page:
    id: str                       # stable slug (state/cache key, logs)
    label: str                    # human name (logs; not shown yet)
    argv: tuple[str, ...] | None  # None = page 0, rendered by the formatter
    ttl: float = 0.0              # cache the command output this many seconds (0 = always fresh)
    max_lines: int = 0            # truncate output to this many lines (0 = unlimited)
    pty: bool = False             # run under a pty (via `script`) to coax color out of tools that only paint to a TTY
    drop_re: str = ""             # drop output lines matching this regex (e.g. our own `ps` process)
    subs: tuple[tuple[str, str], ...] = ()  # (pattern, repl) regex rewrites applied per line, to trim verbose output
    render: str = ""              # builtin renderer key (body comes from readings, not a command)
    colorize: str = ""            # semantic colorizer key applied to the command output (e.g. "listening")
    # Command run by `pirostats click` while on this page. Uniform today; this
    # is the single seam where per-page click actions plug in later.
    click: tuple[str, ...] = ("plasma-systemmonitor",)


# The full stats view is always page 0; the rest are the deep-dive pages the
# user enables and orders via pages.order (see build_pages).
FULL_PAGE = Page("full", "Full stats", None)

REGISTRY: dict[str, Page] = {p.id: p for p in (
    # Instantaneous top, from the same /proc-diff sensor as the panel's Top 1/2/3
    # (not ps's lifetime-average %CPU) — rendered by formatter.format_top_process.
    Page("processes",   "Top processes",  None, render="top_process"),
    # -4: IPv4 only (drops the wide [::] rows). The process column is parsed and
    # tidied in _format_connections (name, and for interpreters the script).
    Page("connections", "Connections",      ("ss", "-4tlnp"), max_lines=20,
         colorize="connections"),
    # --structure (no "Title") drops fastfetch's default user@host line and its
    # rule; the "Separator" entries draw the group divider lines instead.
    Page("fastfetch",   "System info",
         ("fastfetch", "--logo", "none", "--structure",
          "OS:Kernel:Loadavg:Uptime:Separator:Chassis:Board:Bios:CPU:GPU:Display:"
          "BluetoothRadio:Separator:Memory:Disk:Battery:PowerAdapter:Wifi:LocalIP:"
          "DNS:Separator:InitSystem:Shell:LM:DE:WM"),
         ttl=30.0, pty=True),
    # Per-core CPU braille + %, rendered by the formatter (needs braille_html).
    Page("cpu_cores",   "CPU cores",      None, render="cpu_cores"),
    # systemmonitor-style history graphs (CPU + memory stacked): PNG area charts
    # (chart.py) drawn from readings by the formatter (format_graphs).
    Page("graphs",      "Graphs",         None, render="graphs"),
)}


def build_pages(page_ids: list[str]) -> list[Page]:
    """The active page list: the full view (page 0) followed by the configured
    deep-dive pages, in order, skipping unknown ids. A single entry (just the
    full view) means no pager."""
    return [FULL_PAGE] + [REGISTRY[i] for i in page_ids if i in REGISTRY]


# --- command page rendering -------------------------------------------------

_cache: dict[str, tuple[float, str]] = {}


def _run_command(page: Page) -> str:
    """Text output of the page's command, cached per its TTL. A missing binary
    or a failure yields a short one-line message instead of an empty tooltip."""
    now = time.monotonic()
    if page.ttl > 0.0:
        hit = _cache.get(page.id)
        if hit and now - hit[0] < page.ttl:
            return hit[1]

    assert page.argv is not None
    exe = page.argv[0]
    if shutil.which(exe) is None:
        return f"{exe}: not found"

    # pty pages run under `script` so the tool sees a terminal and emits color;
    # fall back to a plain (colorless) run when `script` isn't installed.
    argv = list(page.argv)
    if page.pty and shutil.which("script"):
        argv = ["script", "-qec", shlex.join(page.argv), "/dev/null"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=5.0)
    except Exception as e:  # timeout, OSError, ...
        return f"{exe}: {e}"

    lines = _STRIP.sub("", proc.stdout or proc.stderr).split("\n")
    # Drop trailing lines that are blank once color codes are removed — e.g.
    # fastfetch's Colors swatches, which we render as nothing (their bg codes
    # aren't colored), leaving empty lines at the foot of the page.
    while lines and _SGR.sub("", lines[-1]).strip() == "":
        lines.pop()
    if page.drop_re:
        drop = re.compile(page.drop_re)
        lines = [ln for ln in lines if not drop.search(ln)]
    for pat, repl in page.subs:
        rx = re.compile(pat)
        lines = [rx.sub(repl, ln) for ln in lines]
    if not lines:
        lines = [f"{exe}: no output"]
    if page.max_lines > 0:
        lines = lines[:page.max_lines]
    text = "\n".join(lines)

    if page.ttl > 0.0:
        _cache[page.id] = (now, text)
    return text


def text_to_mono_html(text: str) -> str:
    """Escape command output and keep its spacing under Qt RichText: spaces →
    &nbsp; (RichText collapses whitespace runs like HTML), newlines → <br>."""
    lines = text.split("\n") or [""]
    rendered = [html.escape(ln).replace(" ", "&nbsp;") or "&nbsp;" for ln in lines]
    return "<br>".join(rendered)


def _text_width(text: str) -> int:
    """Widest line in *visible* chars — the monospace content width a page lays
    out to. Strips SGR color codes (fastfetch keeps them for the widget to
    render) so they don't inflate the width and throw the pager off-center."""
    return max((len(_SGR.sub("", ln)) for ln in text.split("\n")), default=0)


def _esc(s: str) -> str:
    return html.escape(s).replace(" ", "&nbsp;")


def _ellipsize(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(1, n - 1)] + "…"


# Well-known daemons by port, so a root-owned listener (whose process `ss` can't
# name without privilege the --user daemon lacks) still gets a label. Curated to
# the usual daemon names; anything else falls back to /etc/services. Inferred
# names are shown muted, not green, since the port is a guess not a confirmation.
_PORT_DAEMON = {
    22: "sshd", 25: "smtpd", 53: "named", 111: "rpcbind", 139: "smbd",
    143: "imapd", 445: "smbd", 465: "smtpd", 587: "smtpd", 631: "cupsd",
    993: "imapd", 995: "pop3d", 3306: "mysqld", 5432: "postgres",
    6379: "redis", 11211: "memcached", 27017: "mongod",
}


# ss reports the interpreter (python, node, …), which says little — resolve
# those to the script/module they're running via the pid's cmdline.
_INTERP = {"python", "python3", "python2", "node", "nodejs", "ruby", "perl",
           "java", "php", "sh", "bash", "zsh", "dash"}
_SS_PROC = re.compile(r'\("([^"]+)",pid=(\d+)')


def _proc_name(comm: str, pid: int) -> str:
    """A telling name for a socket's process: the comm as-is, except for an
    interpreter, where the first script/module argument in its cmdline is what
    the user actually recognizes (python -> the .py, python -m -> the module)."""
    if comm.lower() not in _INTERP:
        return comm
    try:
        raw = open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "replace")
    except OSError:
        return comm
    args = [a for a in raw.split("\0") if a]
    for i, a in enumerate(args[1:], 1):   # skip the interpreter itself (args[0])
        if a == "-m" and i + 1 < len(args):
            return args[i + 1].rsplit("/", 1)[-1]
        if a.startswith("-"):
            continue
        base = a.rsplit("/", 1)[-1]
        return base[:-3] if base.endswith(".py") else base
    return comm


def _service_for_port(addr: str) -> str:
    """Best-guess daemon name for a listening address's port, for the sockets
    `ss` couldn't name. "" when the port is unknown."""
    try:
        port = int(addr.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return ""
    if port in _PORT_DAEMON:
        return _PORT_DAEMON[port]
    try:
        return socket.getservbyport(port, "tcp")
    except OSError:
        return ""


def _format_connections(text: str, min_width: int = 0) -> tuple[str, int]:
    """Fold `ss -4tlnp` down to the two columns that matter for a listening
    socket — the process and its local address:port — one per line, dropping
    ss's State/Recv-Q/Send-Q/Peer noise (State is always LISTEN, Peer always
    *:*). The process is green; a network-exposed bind (anything but loopback)
    is flagged. Floors to min_width so the page matches the other tooltip pages.
    Returns (html, monospace width)."""
    rows: list[tuple[str, str, bool]] = []   # (process, local addr:port, inferred)
    for line in text.split("\n"):
        if not line.strip() or line.startswith(("State", "Netid")):  # drop ss header
            continue
        f = line.split(None, 5)   # State RecvQ SendQ Local Peer Process(may hold spaces)
        if len(f) < 5:
            continue
        addr = f[3]
        m = _SS_PROC.search(f[5]) if len(f) > 5 else None
        if m:
            rows.append((_proc_name(m.group(1), int(m.group(2))), addr, False))  # confirmed
        else:                                           # root-owned: guess from the port
            rows.append((_service_for_port(addr) or "-", addr, True))
    if not rows:
        msg = "no listening sockets"
        return text_to_mono_html(msg), max(min_width, len(msg))

    # Reserve 5 cols for the port (max 65535) and right-align the whole address
    # to the row's right edge: that puts every colon 6 chars from the edge, so
    # the colons — and the IP right edges — line up with no explicit IP padding.
    entries = []   # (process, ip, rendered addr:port, inferred)
    for proc, addr, inf in rows:
        ip, _, port = addr.rpartition(":")
        entries.append((proc, ip, f"{ip}:{port.ljust(5)}", inf))

    w_addr = max(len(a) for _, _, a, _ in entries)
    # Give the process column whatever's left after the address at the shared
    # width, ellipsizing only the long names so the page stays near min_width.
    budget = max(10, min_width - 2 - w_addr)
    w_proc = max(len(_ellipsize(p, budget)) for p, _, _, _ in entries)
    width  = max(min_width, w_proc + 2 + w_addr)

    out: list[str] = []
    for proc, ip, addr, inferred in entries:
        proc     = _ellipsize(proc, budget)
        exposed  = not ip.startswith("127.")           # non-loopback = network-exposed
        # exposed flags the whole row; otherwise green = ss-confirmed, muted = guessed.
        proc_cls = "warn" if exposed else ("label" if inferred else "active")
        addr_h   = f'<span class="warn">{_esc(addr)}</span>' if exposed else _esc(addr)
        gap      = "&nbsp;" * (width - len(proc) - len(addr))   # push addr:port flush right
        out.append(f'<span class="{proc_cls}">{_esc(proc)}</span>{gap}{addr_h}')
    return "<br>".join(out), width


def page_inner(page: Page, idx: int, n: int, min_width: int = 0) -> str:
    """Body + pager for a command page: the command output wrapped in `.page`,
    with the pager centered on the body's own width. The formatter-rendered
    pages (top_process, cpu_cores) don't go through here."""
    text = _run_command(page)
    if page.colorize == "connections":
        inner, width = _format_connections(text, min_width)
        return f'<div class="page">{inner}</div>' + pager_html(idx, width, n)
    inner = text_to_mono_html(text)
    return f'<div class="page">{inner}</div>' + pager_html(idx, _text_width(text), n)


def title_html(page: Page) -> str:
    """Page title rendered exactly like a full-view section header: a left-aligned,
    underlined .title span. Uppercased to match the section titles (Qt's RichText
    has no text-transform, so do it here)."""
    label = html.escape(page.label.upper())
    return f'<div><span class="title">{label}</span></div>'


def pager_html(idx: int, width: int, n: int) -> str:
    """A row of `n` dots, the active one (idx) lit. Centered on `width` (chars)
    with leading &nbsp; and LEFT-aligned — anchored to the content, not the
    tooltip box, so it doesn't drift while Plasma lazily resizes the popup
    (align="center" would recenter on every box-width change). Same monospace
    font as the body so the char padding lines up. Colors: `.pager .on/.off`.
    Returns "" when there's a single page (no deep-dive pages configured)."""
    if n <= 1:
        return ""
    dots = "&nbsp;".join(
        f'<span class="{"on" if i == idx else "off"}">●</span>'
        for i in range(n)
    )
    visible = 2 * n - 1                        # dots plus one &nbsp; between each
    pad = max(0, (width - visible) // 2)
    return f'<div class="pager">{"&nbsp;" * pad}{dots}</div>'


# --- click routing ----------------------------------------------------------

def default_click() -> tuple[str, ...]:
    """The click action while per-page routing isn't wired: every page's default
    (plasma-systemmonitor). Page.click is the seam for making it page-aware."""
    return FULL_PAGE.click
