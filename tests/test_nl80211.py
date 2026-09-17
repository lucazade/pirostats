import struct

import nl80211
from netlink import nla
from nl80211 import Rate, parse_rate, parse_station


def _u8(t, v):  return nla(t, struct.pack("=B", v))
def _s8(t, v):  return nla(t, struct.pack("=b", v))
def _u16(t, v): return nla(t, struct.pack("=H", v))
def _u32(t, v): return nla(t, struct.pack("=I", v))


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
