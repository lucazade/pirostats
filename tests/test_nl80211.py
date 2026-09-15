import struct

import nl80211
from nl80211 import Rate, nla, parse_attrs, parse_rate, parse_station


def _u8(t, v):  return nla(t, struct.pack("=B", v))
def _s8(t, v):  return nla(t, struct.pack("=b", v))
def _u16(t, v): return nla(t, struct.pack("=H", v))
def _u32(t, v): return nla(t, struct.pack("=I", v))


# ── attributes ────────────────────────────────────────────────────────────────

def test_parse_attrs_pads_and_masks_the_nested_flag():
    buf = nla(1, b"abc") + nla(0x8000 | 2, b"xy")      # 7 bytes padded to 8; NLA_F_NESTED set
    assert parse_attrs(buf) == {1: b"abc", 2: b"xy"}


def test_parse_attrs_stops_at_a_truncated_attribute():
    buf = nla(1, b"abcd") + struct.pack("=HH", 40, 2) + b"short"
    assert parse_attrs(buf) == {1: b"abcd"}


# ── rates ─────────────────────────────────────────────────────────────────────

def test_he_rate_matches_iw():
    # iw: "tx bitrate: 1152.8 MBit/s 160MHz HE-MCS 5 HE-NSS 2"
    buf = _u32(nl80211._RATE_BITRATE32, 11528) + _u8(nl80211._RATE_HE_MCS, 5) + _u8(nl80211._RATE_HE_NSS, 2)
    assert parse_rate(buf) == Rate(1152.8, 5, 2)


def test_vht_and_eht_rates():
    vht = _u32(nl80211._RATE_BITRATE32, 8667) + _u8(nl80211._RATE_VHT_MCS, 9) + _u8(nl80211._RATE_VHT_NSS, 2)
    eht = _u32(nl80211._RATE_BITRATE32, 57646) + _u8(nl80211._RATE_EHT_MCS, 13) + _u8(nl80211._RATE_EHT_NSS, 2)
    assert parse_rate(vht) == Rate(866.7, 9, 2)
    assert parse_rate(eht) == Rate(5764.6, 13, 2)


def test_ht_index_is_split_into_mcs_and_streams():
    buf = _u16(nl80211._RATE_BITRATE, 3000) + _u8(nl80211._RATE_MCS, 15)
    assert parse_rate(buf) == Rate(300.0, 7, 2)


def test_legacy_rate_has_no_mcs_or_nss():
    assert parse_rate(_u16(nl80211._RATE_BITRATE, 540)) == Rate(54.0)


def test_rate_without_a_bitrate_is_none():
    assert parse_rate(_u8(nl80211._RATE_HE_MCS, 5)) is None


# ── station ───────────────────────────────────────────────────────────────────

def test_station_reads_signed_signal_and_chains_in_order():
    chains = nla(nl80211._STA_INFO_CHAIN_SIGNAL, _s8(1, -55) + _s8(0, -57))
    tx = nla(nl80211._STA_INFO_TX_BITRATE, _u32(nl80211._RATE_BITRATE32, 11528))
    sta = parse_station(_s8(nl80211._STA_INFO_SIGNAL, -55) + chains + tx)
    assert sta.signal == -55
    assert sta.chains == (-57, -55)
    assert sta.tx == Rate(1152.8)
    assert sta.rx is None


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
    return nl80211._NLMSG_HDR.pack(16 + len(payload), msg_type, 0, seq, 0) + payload + b"\0" * (-len(payload) % 4)


def _genl(seq, attrs):
    return _msg(0x1C, seq, nl80211._GENL_HDR.pack(0, 1, 0) + attrs)


def test_exchange_skips_a_late_reply_to_an_earlier_request():
    nl = nl80211.Nl80211()
    stale = _genl(1, nla(nl80211._ATTR_SSID, b"old"))            # answer to a request that timed out
    fresh = _genl(2, nla(nl80211._ATTR_SSID, b"zip6"))
    ack = _msg(nl80211._NLMSG_ERROR, 2, struct.pack("=i", 0))
    nl._sock, nl._seq = _FakeSock([stale + fresh, ack]), 1
    assert nl._exchange(0x20, nl80211._CMD_GET_INTERFACE, nl80211._NLM_F_ACK, b"") == [{nl80211._ATTR_SSID: b"zip6"}]


def test_a_kernel_error_closes_the_socket_and_returns_none():
    nl = nl80211.Nl80211()
    nl._sock, nl._family = _FakeSock([_msg(nl80211._NLMSG_ERROR, 1, struct.pack("=i", -19))]), 0x20
    assert nl._request(nl80211._CMD_GET_STATION, nl80211._NLM_F_DUMP, b"") is None
    assert nl._sock is None
