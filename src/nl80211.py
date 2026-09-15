"""Wi-Fi link data straight from the kernel's nl80211, over a generic netlink socket.

This is what `iw` does under the hood, minus the fork: one station dump costs
~0.2ms against ~3ms for spawning `iw`, cheap enough to read every poll. Pure
stdlib (`socket` + `struct`), no permissions needed — the station and interface
queries are unprivileged.

Three queries, each keyed by interface name:
  station(ifname)  → signal, per-antenna (chain) signal, tx/rx bitrate + MCS/NSS
  ssid(ifname)     → the connected network's name
  antennas(ifname) → how many receive antennas the radio has (fixed hardware)

Numbers are the ones in <linux/nl80211.h>; they're ABI, so they never move.
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from typing import Optional

# ── netlink / generic netlink framing ─────────────────────────────────────────
_NETLINK_GENERIC   = 16
_NLM_F_REQUEST     = 0x1
_NLM_F_ACK         = 0x4
_NLM_F_DUMP        = 0x300
_NLMSG_ERROR       = 2
_NLMSG_DONE        = 3
_NLA_TYPE_MASK     = 0x3FFF   # strips NLA_F_NESTED / NLA_F_NET_BYTEORDER
_NLMSG_HDR         = struct.Struct("=IHHII")   # len, type, flags, seq, pid
_GENL_HDR          = struct.Struct("=BBH")     # cmd, version, reserved
_NLA_HDR           = struct.Struct("=HH")      # len, type

_GENL_ID_CTRL          = 0x10
_CTRL_CMD_GETFAMILY    = 3
_CTRL_ATTR_FAMILY_ID   = 1
_CTRL_ATTR_FAMILY_NAME = 2

# ── nl80211 ───────────────────────────────────────────────────────────────────
_CMD_GET_WIPHY     = 1
_CMD_GET_INTERFACE = 5
_CMD_GET_STATION   = 17

_ATTR_WIPHY               = 1
_ATTR_IFINDEX             = 3
_ATTR_STA_INFO            = 21
_ATTR_SSID                = 52
_ATTR_WIPHY_ANTENNA_AVAIL_RX = 114

_STA_INFO_SIGNAL       = 7
_STA_INFO_TX_BITRATE   = 8
_STA_INFO_RX_BITRATE   = 14
_STA_INFO_CHAIN_SIGNAL = 25

_RATE_BITRATE   = 1    # u16, 100 kbit/s (legacy, capped at 6553.5 Mbit/s)
_RATE_MCS       = 2    # u8, HT (Wi-Fi 4) index: streams folded in, 8 per stream
_RATE_BITRATE32 = 5    # u32, 100 kbit/s
_RATE_VHT_MCS   = 6    # Wi-Fi 5
_RATE_VHT_NSS   = 7
_RATE_HE_MCS    = 13   # Wi-Fi 6
_RATE_HE_NSS    = 14
_RATE_EHT_MCS   = 19   # Wi-Fi 7
_RATE_EHT_NSS   = 20

_TIMEOUT_S = 1.0


@dataclass(frozen=True)
class Rate:
    """One direction of the link. MCS/NSS are None on a legacy (pre-HT) rate."""
    mbit: float
    mcs: Optional[int] = None
    nss: Optional[int] = None


@dataclass(frozen=True)
class Station:
    signal: Optional[int]        # dBm, the combined figure
    chains: tuple[int, ...]      # dBm per antenna, in chain order
    tx: Optional[Rate]
    rx: Optional[Rate]


# ── pure parsing (tested) ─────────────────────────────────────────────────────

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


def _s8(b: bytes) -> int:
    return struct.unpack_from("=b", b)[0]


def parse_rate(buf: bytes) -> Optional[Rate]:
    """A nested NL80211_STA_INFO_*_BITRATE → Rate. The newest standard present
    wins (EHT, then HE, VHT, HT), since a driver reports only the one in use."""
    a = parse_attrs(buf)
    if _RATE_BITRATE32 in a:
        mbit = struct.unpack_from("=I", a[_RATE_BITRATE32])[0] / 10
    elif _RATE_BITRATE in a:
        mbit = struct.unpack_from("=H", a[_RATE_BITRATE])[0] / 10
    else:
        return None
    for mcs_attr, nss_attr in ((_RATE_EHT_MCS, _RATE_EHT_NSS),
                               (_RATE_HE_MCS, _RATE_HE_NSS),
                               (_RATE_VHT_MCS, _RATE_VHT_NSS)):
        if mcs_attr in a:
            nss = a[nss_attr][0] if nss_attr in a else None
            return Rate(mbit, a[mcs_attr][0], nss)
    if _RATE_MCS in a:
        # HT packs the stream count into the index (0-7 one stream, 8-15 two, …),
        # so split it to read like the newer standards. 32+ are special
        # modulations with no clean split: report the raw index.
        idx = a[_RATE_MCS][0]
        return Rate(mbit, idx % 8, idx // 8 + 1) if idx < 32 else Rate(mbit, idx)
    return Rate(mbit)


def parse_station(sta_info: bytes) -> Station:
    """A nested NL80211_ATTR_STA_INFO → Station."""
    a = parse_attrs(sta_info)
    chains: tuple[int, ...] = ()
    if _STA_INFO_CHAIN_SIGNAL in a:
        # One s8 per chain, the attribute type being the chain's index.
        chains = tuple(_s8(v) for _, v in sorted(parse_attrs(a[_STA_INFO_CHAIN_SIGNAL]).items()))
    return Station(
        signal=_s8(a[_STA_INFO_SIGNAL]) if _STA_INFO_SIGNAL in a else None,
        chains=chains,
        tx=parse_rate(a[_STA_INFO_TX_BITRATE]) if _STA_INFO_TX_BITRATE in a else None,
        rx=parse_rate(a[_STA_INFO_RX_BITRATE]) if _STA_INFO_RX_BITRATE in a else None,
    )


# ── the socket ────────────────────────────────────────────────────────────────

class Nl80211:
    """A long-lived nl80211 socket. The family id is resolved once per socket;
    any socket error closes it and the call returns None, so the next call
    starts over on a fresh one (a daemon that outlives a driver reload recovers
    on its own)."""

    def __init__(self) -> None:
        self._sock: Optional[socket.socket] = None
        self._family = 0
        self._seq = 0

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def station(self, ifname: str) -> Optional[Station]:
        """The access point this interface is associated with, or None when not
        connected. In managed mode the dump holds exactly that one station."""
        msgs = self._query(_CMD_GET_STATION, _NLM_F_DUMP, ifname)
        for m in msgs or ():
            if _ATTR_STA_INFO in m:
                return parse_station(m[_ATTR_STA_INFO])
        return None

    def ssid(self, ifname: str) -> Optional[str]:
        msgs = self._query(_CMD_GET_INTERFACE, _NLM_F_ACK, ifname)
        raw = msgs[0].get(_ATTR_SSID) if msgs else None
        return raw.decode("utf-8", "replace") if raw else None

    def antennas(self, ifname: str) -> int:
        """Receive antennas the radio has (bits set in its available-RX mask);
        0 when the driver doesn't report one."""
        iface = self._query(_CMD_GET_INTERFACE, _NLM_F_ACK, ifname)
        if not iface or _ATTR_WIPHY not in iface[0]:
            return 0
        wiphy = self._request(_CMD_GET_WIPHY, _NLM_F_ACK, nla(_ATTR_WIPHY, iface[0][_ATTR_WIPHY]))
        mask = wiphy[0].get(_ATTR_WIPHY_ANTENNA_AVAIL_RX) if wiphy else None
        return bin(struct.unpack_from("=I", mask)[0]).count("1") if mask else 0

    # ── plumbing ──

    def _query(self, cmd: int, flags: int, ifname: str) -> Optional[list[dict[int, bytes]]]:
        try:
            ifindex = socket.if_nametoindex(ifname)
        except OSError:
            return None
        return self._request(cmd, flags, nla(_ATTR_IFINDEX, struct.pack("=I", ifindex)))

    def _request(self, cmd: int, flags: int, attrs: bytes) -> Optional[list[dict[int, bytes]]]:
        try:
            if self._sock is None:
                self._open()
            return self._exchange(self._family, cmd, flags, attrs)
        except (OSError, struct.error, IndexError):
            self.close()
            return None

    def _open(self) -> None:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, _NETLINK_GENERIC)
        sock.settimeout(_TIMEOUT_S)
        sock.bind((0, 0))
        self._sock = sock
        reply = self._exchange(_GENL_ID_CTRL, _CTRL_CMD_GETFAMILY, _NLM_F_ACK,
                               nla(_CTRL_ATTR_FAMILY_NAME, b"nl80211\0"))
        self._family = struct.unpack_from("=H", reply[0][_CTRL_ATTR_FAMILY_ID])[0]

    def _exchange(self, family: int, cmd: int, flags: int, attrs: bytes) -> list[dict[int, bytes]]:
        """Send one request and collect its replies up to the terminator (DONE for
        a dump, the ACK for a plain request — which is why every request carries
        either flag). Replies are matched on the sequence number, so a late
        answer to an earlier, timed-out request can't be read as this one's."""
        assert self._sock is not None
        self._seq += 1
        seq = self._seq
        body = _GENL_HDR.pack(cmd, 1, 0) + attrs
        self._sock.send(_NLMSG_HDR.pack(_NLMSG_HDR.size + len(body), family,
                                        _NLM_F_REQUEST | flags, seq, 0) + body)
        out: list[dict[int, bytes]] = []
        while True:
            data = self._sock.recv(65536)
            i = 0
            while i + _NLMSG_HDR.size <= len(data):
                length, msg_type, _, msg_seq, _ = _NLMSG_HDR.unpack_from(data, i)
                if length < _NLMSG_HDR.size:
                    raise OSError("malformed netlink message")
                payload = data[i + _NLMSG_HDR.size:i + length]
                i += (length + 3) & ~3
                if msg_seq != seq:
                    continue
                if msg_type == _NLMSG_DONE:
                    return out
                if msg_type == _NLMSG_ERROR:
                    err = struct.unpack_from("=i", payload)[0]
                    if err:
                        raise OSError(-err, "nl80211 request failed")
                    return out
                out.append(parse_attrs(payload[_GENL_HDR.size:]))
