#!/usr/bin/env python3
"""Read HID++ 2.0 battery and device name from a Logitech Bolt receiver device.
Usage:
  logitech-bolt-battery [device_index]          → print "<N>%"
  logitech-bolt-battery --name [device_index]   → print device name
  logitech-bolt-battery --info [device_index]   → print "<name>\\n<N>%" (one call)
Exits 1 if the data cannot be read.
Requires: libhidapi-hidraw (Logitech udev rules grant hidraw access to the user).
"""
import ctypes, ctypes.util, glob, os, sys

# ── libhidapi ──────────────────────────────────────────────────────────────────

def _load_hidapi():
    for name in ("hidapi-hidraw", "hidapi"):
        path = ctypes.util.find_library(name)
        if path:
            return ctypes.CDLL(path)
    raise OSError("libhidapi-hidraw not found")

_lib = _load_hidapi()
_lib.hid_open_path.restype     = ctypes.c_void_p
_lib.hid_open_path.argtypes    = [ctypes.c_char_p]
_lib.hid_write.restype         = ctypes.c_int
_lib.hid_write.argtypes        = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
_lib.hid_read_timeout.restype  = ctypes.c_int
_lib.hid_read_timeout.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
_lib.hid_close.restype         = None
_lib.hid_close.argtypes        = [ctypes.c_void_p]

# ── Device discovery ───────────────────────────────────────────────────────────

BOLT_PID       = "c548"
BOLT_USB_IFACE = 2        # DJ/HID++ control interface
SW_ID_MAX      = 15       # software-id is 4 bits; 0 is reserved for device-initiated events
TIMEOUT_MS     = 1000

_sw_id = 0


def _next_sw_id():
    """Rotate the software-id tag, one per exchange. It exists precisely so a
    response can be paired with its request, and two consecutive exchanges must
    not share it: the ROOT query that resolves one feature index is otherwise
    identical, byte for byte, to the one that resolves the next. A reply arriving
    after its own read timed out would then be collected by the following
    exchange, handing it the wrong feature index — an MX Keys S once reported
    ord('M') = 77 as its battery level that way, having answered getDeviceName
    while we thought we were reading UNIFIED_BATTERY.
    """
    global _sw_id
    _sw_id = _sw_id % SW_ID_MAX + 1
    return _sw_id


def _pkt(dev_idx, feat, func, *params):
    """A HID++ 2.0 long request: report id, device index, feature index, then the
    function and this exchange's software id packed in one byte, then params."""
    return bytes([0x11, dev_idx, feat, (func << 4) | _next_sw_id()]
                 + list(params) + [0] * (16 - len(params)))


def _bolt_hidraw():
    """Return the hidraw path of the Bolt receiver's HID++ control interface."""
    for h in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            cur, prev = os.path.realpath(h + "/device"), None
            for _ in range(8):
                if os.path.exists(os.path.join(cur, "idProduct")):
                    pid = open(os.path.join(cur, "idProduct")).read().strip().lower()
                    if pid == BOLT_PID and prev is not None:
                        iface = int(os.path.basename(prev).rsplit(".", 1)[-1])
                        if iface == BOLT_USB_IFACE:
                            return "/dev/" + os.path.basename(h)
                    break
                prev, cur = cur, os.path.dirname(cur)
        except (OSError, ValueError):
            pass
    return None

# ── HID++ 2.0 helpers ─────────────────────────────────────────────────────────

def _xfer(handle, pkt, expect_feat):
    """Write pkt, return the response carrying this exchange's own software id, or
    None on timeout. Two guards against picking up somebody else's answer: the
    queue is drained first, since hidraw hands every reader a copy of every report
    (a second pirostats mid-restart, an unsolicited device notification, or a reply
    to an exchange that already timed out), and byte 3 is matched, not just the
    device and feature indices — see _next_sw_id."""
    buf = ctypes.create_string_buffer(64)
    while _lib.hid_read_timeout(handle, buf, 64, 0) > 0:
        pass
    if _lib.hid_write(handle, pkt, len(pkt)) < 0:
        return None
    for _ in range(10):
        n = _lib.hid_read_timeout(handle, buf, 64, TIMEOUT_MS)
        if n >= 5:
            raw = buf.raw[:n]
            if raw[1] == pkt[1] and raw[2] == expect_feat and raw[3] == pkt[3]:
                return raw
        if n <= 0:
            break
    return None


def _get_feature_idx(handle, dev_idx, feature_id):
    """Ask ROOT (feature 0) for the index of feature_id; return 0 if unsupported."""
    pkt = _pkt(dev_idx, 0x00, 0, feature_id >> 8, feature_id & 0xFF)
    r = _xfer(handle, pkt, 0x00)
    return r[4] if r else 0


def _get_battery(handle, dev_idx):
    """Return battery percentage (int) or None."""
    feat = _get_feature_idx(handle, dev_idx, 0x1004)  # UNIFIED_BATTERY
    if not feat:
        return None
    pkt = _pkt(dev_idx, feat, 1)          # function 1 = getStatus
    r = _xfer(handle, pkt, feat)
    return r[4] if r else None


def _get_name(handle, dev_idx):
    """Return device name string (ASCII) or empty string."""
    feat = _get_feature_idx(handle, dev_idx, 0x0005)  # DEVICE_NAME
    if not feat:
        return ""
    pkt = _pkt(dev_idx, feat, 1, 0x00)    # function 1 = getDeviceName(charIndex=0)
    r = _xfer(handle, pkt, feat)
    if not r:
        return ""
    payload = r[4:]
    end = payload.find(0)
    return payload[:end if end >= 0 else len(payload)].decode("ascii", errors="replace").strip()

# ── Public API ─────────────────────────────────────────────────────────────────

def query(dev_idx=1, want_name=False):
    """Return (name, level) where name is "" if not requested, level is None on failure."""
    path = _bolt_hidraw()
    if not path:
        return ("", None)
    handle = _lib.hid_open_path(path.encode())
    if not handle:
        return ("", None)
    try:
        name  = _get_name(handle, dev_idx) if want_name else ""
        level = _get_battery(handle, dev_idx)
        return (name, level)
    finally:
        _lib.hid_close(handle)

# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    mode = "battery"
    if args and args[0] in ("--name", "--info"):
        mode = args.pop(0).lstrip("-")

    dev_idx = int(args[0]) if args else 1

    name, level = query(dev_idx, want_name=(mode in ("name", "info")))

    if mode == "name":
        if not name:
            sys.exit(1)
        print(name)
    elif mode == "info":
        if level is None:
            sys.exit(1)
        print(name or "Unknown")
        print(f"{level}%")
    else:
        if level is None:
            sys.exit(1)
        print(f"{level}%")
