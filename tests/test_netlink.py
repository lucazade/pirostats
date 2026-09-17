import socket
import struct

import netlink
from netlink import nla, parse_attrs, parse_route


# ── attributes ────────────────────────────────────────────────────────────────

def test_parse_attrs_pads_and_masks_the_nested_flag():
    buf = nla(1, b"abc") + nla(0x8000 | 2, b"xy")      # 7 bytes padded to 8; NLA_F_NESTED set
    assert parse_attrs(buf) == {1: b"abc", 2: b"xy"}


def test_parse_attrs_stops_at_a_truncated_attribute():
    buf = nla(1, b"abcd") + struct.pack("=HH", 40, 2) + b"short"
    assert parse_attrs(buf) == {1: b"abcd"}


# ── routes ────────────────────────────────────────────────────────────────────

def _rtmsg(attrs):
    return netlink._RTMSG.pack(socket.AF_INET, 32, 0, 0, 254, 0, 0, 1, 0) + attrs


def test_parse_route_reads_the_interface_and_source():
    # ip: "8.8.8.8 via 192.168.1.1 dev wlan0 src 192.168.1.5"
    reply = _rtmsg(nla(netlink._RTA_DST, socket.inet_aton("8.8.8.8"))
                   + nla(netlink._RTA_OIF, struct.pack("=I", 3))
                   + nla(netlink._RTA_PREFSRC, socket.inet_aton("192.168.1.5")))
    assert parse_route(reply) == (3, "192.168.1.5")


def test_parse_route_without_a_source_address():
    assert parse_route(_rtmsg(nla(netlink._RTA_OIF, struct.pack("=I", 2)))) == (2, None)


# ── the exchange ──────────────────────────────────────────────────────────────

class _FakeSock:
    """Replays canned datagrams in place of the kernel."""
    def __init__(self, datagrams):
        self.datagrams = list(datagrams)
    def send(self, data):
        pass
    def recv(self, _):
        return self.datagrams.pop(0)
    def close(self):
        pass


def _msg(msg_type, seq, payload):
    return netlink.NLMSG_HDR.pack(16 + len(payload), msg_type, 0, seq, 0) + payload + b"\0" * (-len(payload) % 4)


def test_exchange_skips_a_late_reply_to_an_earlier_request():
    sock = netlink.Socket(netlink.NETLINK_ROUTE)
    stale = _msg(24, 1, b"old!")               # answer to a request that timed out
    fresh = _msg(24, 2, b"new!")
    ack = _msg(netlink.NLMSG_ERROR, 2, struct.pack("=i", 0))
    sock._sock, sock._seq = _FakeSock([stale + fresh, ack]), 1
    assert sock._exchange(26, netlink.NLM_F_ACK, b"") == [b"new!"]


def test_a_kernel_error_closes_the_socket_and_returns_none():
    sock = netlink.Socket(netlink.NETLINK_ROUTE)
    sock._sock = _FakeSock([_msg(netlink.NLMSG_ERROR, 1, struct.pack("=i", -101))])   # ENETUNREACH
    assert sock.request(26, netlink.NLM_F_ACK, b"") is None
    assert sock._sock is None


def test_a_dump_collects_every_part_until_done():
    sock = netlink.Socket(netlink.NETLINK_GENERIC)
    sock._sock = _FakeSock([_msg(0x1C, 1, b"aaaa") + _msg(0x1C, 1, b"bbbb"), _msg(netlink.NLMSG_DONE, 1, b"")])
    assert sock.request(0x1C, netlink.NLM_F_DUMP, b"") == [b"aaaa", b"bbbb"]
