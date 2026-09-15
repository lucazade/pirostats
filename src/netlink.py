"""Talking to the kernel over netlink, in pure stdlib (`socket` + `struct`).

Netlink is the socket interface `ip` and `iw` use under the hood; asking it
directly skips their fork (~3ms each) for a sub-millisecond round trip. Two users:

  Routes     (here)       → which interface and source address reach the internet
  Nl80211    (nl80211.py) → the Wi-Fi link: SSID, signal, rates

This module holds the framing both share: attributes, the request/reply exchange,
and a socket that reopens itself after an error. Every query here is unprivileged.
Numbers are the ones in <linux/netlink.h> / <linux/rtnetlink.h>; they're ABI.
"""
from __future__ import annotations

import socket
import struct
from typing import Callable, Optional, Union

NETLINK_ROUTE   = 0
NETLINK_GENERIC = 16

NLM_F_REQUEST = 0x1
NLM_F_ACK     = 0x4
NLM_F_DUMP    = 0x300
NLMSG_ERROR   = 2
NLMSG_DONE    = 3

NLMSG_HDR = struct.Struct("=IHHII")   # len, type, flags, seq, pid
_NLA_HDR  = struct.Struct("=HH")      # len, type
_NLA_TYPE_MASK = 0x3FFF               # strips NLA_F_NESTED / NLA_F_NET_BYTEORDER

_TIMEOUT_S = 1.0

# ── rtnetlink ─────────────────────────────────────────────────────────────────
_RTM_GETROUTE = 26
_RTMSG        = struct.Struct("=BBBBBBBBI")   # family, dst_len, src_len, tos, table, protocol, scope, type, flags
_RTA_DST      = 1
_RTA_OIF      = 4
_RTA_PREFSRC  = 7


# ── attributes (pure, tested) ─────────────────────────────────────────────────

def nla(attr_type: int, payload: bytes) -> bytes:
    """One netlink attribute: header + payload, padded to 4 bytes."""
    length = _NLA_HDR.size + len(payload)
    return _NLA_HDR.pack(length, attr_type) + payload + b"\0" * (-length % 4)


def parse_attrs(buf: bytes) -> dict[int, bytes]:
    """A run of netlink attributes → {type: payload}. Stops at a malformed header
    instead of raising, so a truncated message yields what it holds."""
    out: dict[int, bytes] = {}
    i = 0
    while i + _NLA_HDR.size <= len(buf):
        length, attr_type = _NLA_HDR.unpack_from(buf, i)
        if length < _NLA_HDR.size or i + length > len(buf):
            break
        out[attr_type & _NLA_TYPE_MASK] = buf[i + _NLA_HDR.size:i + length]
        i += (length + 3) & ~3
    return out


def parse_route(payload: bytes) -> tuple[Optional[int], Optional[str]]:
    """An RTM_NEWROUTE reply → (output interface index, preferred source IPv4)."""
    a = parse_attrs(payload[_RTMSG.size:])
    oif = struct.unpack_from("=I", a[_RTA_OIF])[0] if _RTA_OIF in a else None
    src = socket.inet_ntoa(a[_RTA_PREFSRC]) if len(a.get(_RTA_PREFSRC, b"")) == 4 else None
    return oif, src


# ── the socket ────────────────────────────────────────────────────────────────

class Socket:
    """A long-lived netlink socket for one protocol. Any error closes it and the
    request returns None, so the next one starts over on a fresh socket (a daemon
    that outlives a driver reload recovers on its own)."""

    def __init__(self, protocol: int) -> None:
        self._protocol = protocol
        self._sock: Optional[socket.socket] = None
        self._seq = 0

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def request(self, msg_type: Union[int, Callable[[], int]], flags: int, body: bytes) -> Optional[list[bytes]]:
        """The payloads of the replies to one request, or None on any failure
        (no socket, a kernel error such as "no route", a malformed reply).
        `msg_type` may be a callable, read once the socket is open: a generic
        netlink family's id is only known after _on_open resolves it."""
        try:
            if self._sock is None:
                sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, self._protocol)
                sock.settimeout(_TIMEOUT_S)
                sock.bind((0, 0))
                self._sock = sock
                self._on_open()
            return self._exchange(msg_type() if callable(msg_type) else msg_type, flags, body)
        except (OSError, struct.error, KeyError, IndexError):
            self.close()
            return None

    def _on_open(self) -> None:
        """Per-protocol setup on a fresh socket (nl80211 resolves its family id)."""

    def _exchange(self, msg_type: int, flags: int, body: bytes) -> list[bytes]:
        """Send one request and collect its replies up to the terminator (DONE for
        a dump, the ACK for a plain request — which is why every request should
        carry one of the two flags). Replies are matched on the sequence number,
        so a late answer to an earlier, timed-out request can't be read as this
        one's."""
        assert self._sock is not None
        self._seq += 1
        seq = self._seq
        self._sock.send(NLMSG_HDR.pack(NLMSG_HDR.size + len(body), msg_type,
                                       NLM_F_REQUEST | flags, seq, 0) + body)
        out: list[bytes] = []
        while True:
            data = self._sock.recv(65536)
            i = 0
            while i + NLMSG_HDR.size <= len(data):
                length, reply_type, _, reply_seq, _ = NLMSG_HDR.unpack_from(data, i)
                if length < NLMSG_HDR.size:
                    raise OSError("malformed netlink message")
                payload = data[i + NLMSG_HDR.size:i + length]
                i += (length + 3) & ~3
                if reply_seq != seq:
                    continue
                if reply_type == NLMSG_DONE:
                    return out
                if reply_type == NLMSG_ERROR:
                    err = struct.unpack_from("=i", payload)[0]
                    if err:
                        raise OSError(-err, "netlink request failed")
                    return out
                out.append(payload)


class Routes(Socket):
    def __init__(self) -> None:
        super().__init__(NETLINK_ROUTE)

    def route_to(self, dst: str) -> tuple[Optional[str], Optional[str]]:
        """(interface, source address) the kernel would use to reach IPv4 `dst` —
        what `ip route get <dst>` prints as `dev` and `src`. (None, None) with no
        route (network down)."""
        body = _RTMSG.pack(socket.AF_INET, 32, 0, 0, 0, 0, 0, 0, 0) + nla(_RTA_DST, socket.inet_aton(dst))
        replies = self.request(_RTM_GETROUTE, NLM_F_ACK, body)
        if not replies:
            return None, None
        oif, src = parse_route(replies[0])
        try:
            dev = socket.if_indextoname(oif) if oif else None
        except OSError:
            dev = None
        return dev, src
