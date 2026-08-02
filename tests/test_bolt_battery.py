"""bolt_battery: the HID++ 2.0 exchange, against a scripted receiver.

The one thing that goes wrong on real hardware is a matter of timing, so it is
the thing worth pinning here. Waking a sleeping Logitech device costs about as
much as TIMEOUT_MS allows for a whole round trip (measured on an MX Keys S:
975ms for the first exchange, 15-40ms for every one after it), so the first
request regularly answers *after* its own read has given up — and its reply is
then still queued when the next request goes looking for one.

That used to be silently destructive rather than merely slow. The two ROOT
queries a full read makes back to back, one to resolve DEVICE_NAME and one to
resolve UNIFIED_BATTERY, are identical byte for byte, so the second exchange
would collect the first one's late answer and walk away with the wrong feature
index. The battery request then landed on getDeviceName and its first payload
byte became the charge: an MX Keys S reported ord('M') = 77%.

The receiver below is scripted rather than mocked call-by-call, so the tests
describe what the *device* does (answers late, broadcasts unasked, stays silent)
and leave the exchange free to solve it however it likes.
"""
import bolt_battery
import pytest
from bolt_battery import _get_feature_idx, _next_sw_id, _pkt, query

NAME = b"MX Keys S"
LEVEL = 50
FEAT_NAME, FEAT_BATT = 0x03, 0x08   # feature indices as a real MX Keys S reports them


class _Receiver:
    """A Bolt receiver with one paired device.

    `sleepy` holds the very first reply back by one read, which is exactly what
    the wake-up looks like from here: the read times out, and the answer turns up
    in the queue immediately after.
    """

    def __init__(self, sleepy):
        self.queue = []        # reports waiting for the host to read
        self.held = None       # the reply the wake-up delays
        self.sleepy = sleepy
        self.writes = []       # every request the host sent, for cost assertions

    def _reply(self, pkt, payload):
        # A response echoes bytes 0-3 (report, device, feature, function+sw-id).
        return bytes(pkt[:4]) + bytes(payload).ljust(16, b"\0")

    def write(self, pkt):
        self.writes.append(pkt)
        feat, func = pkt[2], pkt[3] >> 4
        if feat == 0x00:                          # ROOT: which index is this feature?
            fid = (pkt[4] << 8) | pkt[5]
            reply = self._reply(pkt, [{0x0005: FEAT_NAME, 0x1004: FEAT_BATT}.get(fid, 0)])
        elif feat == FEAT_NAME and func == 1:     # getDeviceName(charIndex=0)
            reply = self._reply(pkt, NAME)
        elif feat == FEAT_BATT and func == 1:     # getStatus -> state of charge
            reply = self._reply(pkt, [LEVEL])
        else:
            return                                # unsupported: no answer at all
        if self.sleepy:
            self.sleepy = False
            self.held = reply
        else:
            self.queue.append(reply)

    def read(self, blocking):
        if self.queue:
            return self.queue.pop(0)
        if self.held is not None and blocking:
            self.queue.append(self.held)          # lands just after this read gives up
            self.held = None
        return None                               # a poll never waits for the device


class _Lib:
    """Stands in for libhidapi. Only the four calls bolt_battery makes."""

    def __init__(self, receiver):
        self.receiver = receiver

    def hid_open_path(self, path):
        return 1

    def hid_close(self, handle):
        pass

    def hid_write(self, handle, pkt, length):
        self.receiver.write(bytes(pkt))
        return length

    def hid_read_timeout(self, handle, buf, size, timeout_ms):
        report = self.receiver.read(blocking=timeout_ms > 0)
        if report is None:
            return 0
        buf.raw = report.ljust(64, b"\0")
        return len(report)


@pytest.fixture
def receiver(monkeypatch):
    """A device that answers its first request too late. Pass `awake` for one
    that doesn't — see `_Receiver`."""
    def build(sleepy=True):
        dev = _Receiver(sleepy)
        monkeypatch.setattr(bolt_battery, "_lib", _Lib(dev))
        monkeypatch.setattr(bolt_battery, "_bolt_hidraw", lambda: "/dev/hidraw-test")
        return dev
    return build


# ── the late reply ───────────────────────────────────────────────────────────

def test_late_reply_is_not_mistaken_for_the_battery_level(receiver):
    receiver(sleepy=True)
    name, level = query(1, want_name=True)
    # 77 is ord('M'): the name answering a question about the battery.
    assert level == LEVEL, f"got {level}; ord('M') is {ord('M')}"
    assert name == NAME.decode()


def test_late_reply_does_not_cost_the_device_name(receiver):
    """The name is what the old code lost outright — the retry is what wins it
    back, since by the second attempt the device is awake."""
    receiver(sleepy=True)
    assert query(1, want_name=True)[0] == NAME.decode()


def test_level_only_read_survives_a_sleeping_device(receiver):
    receiver(sleepy=True)
    assert query(1, want_name=False) == ("", LEVEL)


def test_unsolicited_report_is_drained_before_asking(receiver):
    """hidraw copies every report to every open reader, so a device-initiated
    battery broadcast can be sitting there before we ask anything."""
    dev = receiver(sleepy=False)
    dev.queue.append(bytes([0x11, 0x01, FEAT_BATT, 0x00, 99]).ljust(20, b"\0"))
    assert query(1, want_name=True)[1] == LEVEL


def test_reply_carrying_a_foreign_tag_is_refused(receiver):
    """The software id is the whole point: a response that doesn't carry this
    exchange's tag belongs to some other exchange."""
    dev = receiver(sleepy=False)
    answer = dev.write

    def with_wrong_tag(pkt):
        answer(bytes(pkt[:3]) + bytes([pkt[3] ^ 0x0F]) + pkt[4:])
    dev.write = with_wrong_tag
    assert _get_feature_idx(1, 1, 0x1004) == 0


# ── no regression on the ordinary paths ──────────────────────────────────────

def test_awake_device_reads_name_and_level(receiver):
    receiver(sleepy=False)
    assert query(1, want_name=True) == (NAME.decode(), LEVEL)


def test_retry_stays_out_of_the_way_when_nothing_fails(receiver):
    """One ROOT exchange per feature, not two: the retry must cost nothing on a
    device that answers, because every exchange wakes the hardware and drains
    the very battery being measured (see sensors.BOLT_CACHE_TTL)."""
    dev = receiver(sleepy=False)
    query(1, want_name=True)
    assert len([w for w in dev.writes if w[2] == 0x00]) == 2


def test_silent_device_gives_up_rather_than_inventing_a_reading(receiver):
    dev = receiver(sleepy=False)
    dev.write = lambda pkt: None
    assert query(1, want_name=True) == ("", None)


def test_unsupported_feature_reports_no_battery(receiver):
    """A device without UNIFIED_BATTERY answers ROOT with index 0."""
    dev = receiver(sleepy=False)
    answer = dev.write
    dev.write = lambda pkt: None if pkt[2] == 0x00 and pkt[5] == 0x04 else answer(pkt)
    assert query(1, want_name=False)[1] is None


# ── packet shape ─────────────────────────────────────────────────────────────

def test_software_id_rotates_within_its_four_bits(receiver):
    tags = [_next_sw_id() for _ in range(40)]
    assert all(1 <= t <= 15 for t in tags), "0 is reserved for device-initiated events"
    assert all(a != b for a, b in zip(tags, tags[1:])), "consecutive exchanges shared a tag"
    assert len(set(tags)) == 15


def test_request_is_a_well_formed_long_report(receiver):
    pkt = _pkt(1, FEAT_BATT, 1, 0x00)
    assert len(pkt) == 20
    assert pkt[0] == 0x11 and pkt[1] == 1 and pkt[2] == FEAT_BATT
    assert pkt[3] >> 4 == 1                  # function
    assert 1 <= pkt[3] & 0x0F <= 15          # software id
