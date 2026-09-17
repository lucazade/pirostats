"""Wi-Fi link data straight from the kernel's nl80211 (generic netlink).

This is what `iw` does under the hood, minus the fork: one station dump costs
~0.2ms against ~3ms for spawning `iw`, cheap enough to read every poll. The
framing and the socket are netlink.py's; this module is the nl80211 vocabulary.

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

from netlink import NETLINK_GENERIC, NLM_F_ACK, NLM_F_DUMP, Socket, nla, parse_attrs

_GENL_HDR = struct.Struct("=BBH")     # cmd, version, reserved

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

class Nl80211(Socket):
    """The nl80211 queries, on a netlink.Socket whose family id is resolved
    each time the socket (re)opens."""

    def __init__(self) -> None:
        super().__init__(NETLINK_GENERIC)
        self._family = 0

    def station(self, ifname: str) -> Optional[Station]:
        """The access point this interface is associated with, or None when not
        connected. In managed mode the dump holds exactly that one station."""
        for m in self._query(_CMD_GET_STATION, NLM_F_DUMP, ifname) or ():
            if _ATTR_STA_INFO in m:
                return parse_station(m[_ATTR_STA_INFO])
        return None

    def ssid(self, ifname: str) -> Optional[str]:
        msgs = self._query(_CMD_GET_INTERFACE, NLM_F_ACK, ifname)
        raw = msgs[0].get(_ATTR_SSID) if msgs else None
        return raw.decode("utf-8", "replace") if raw else None

    def antennas(self, ifname: str) -> int:
        """Receive antennas the radio has (bits set in its available-RX mask);
        0 when the driver doesn't report one."""
        iface = self._query(_CMD_GET_INTERFACE, NLM_F_ACK, ifname)
        if not iface or _ATTR_WIPHY not in iface[0]:
            return 0
        wiphy = self._genl(_CMD_GET_WIPHY, NLM_F_ACK, nla(_ATTR_WIPHY, iface[0][_ATTR_WIPHY]))
        mask = wiphy[0].get(_ATTR_WIPHY_ANTENNA_AVAIL_RX) if wiphy else None
        return bin(struct.unpack_from("=I", mask)[0]).count("1") if mask else 0

    # ── plumbing ──

    def _query(self, cmd: int, flags: int, ifname: str) -> Optional[list[dict[int, bytes]]]:
        try:
            ifindex = socket.if_nametoindex(ifname)
        except OSError:
            return None
        return self._genl(cmd, flags, nla(_ATTR_IFINDEX, struct.pack("=I", ifindex)))

    def _genl(self, cmd: int, flags: int, attrs: bytes) -> Optional[list[dict[int, bytes]]]:
        """One nl80211 command → the attributes of each reply. The family id goes
        in lazily, so a request that (re)opens the socket still goes out under
        the id _on_open just resolved."""
        replies = self.request(lambda: self._family, flags, _GENL_HDR.pack(cmd, 1, 0) + attrs)
        return None if replies is None else [parse_attrs(r[_GENL_HDR.size:]) for r in replies]

    def _on_open(self) -> None:
        reply = self._exchange(_GENL_ID_CTRL, NLM_F_ACK, _GENL_HDR.pack(_CTRL_CMD_GETFAMILY, 1, 0)
                               + nla(_CTRL_ATTR_FAMILY_NAME, b"nl80211\0"))
        attrs = parse_attrs(reply[0][_GENL_HDR.size:])
        self._family = struct.unpack_from("=H", attrs[_CTRL_ATTR_FAMILY_ID])[0]
