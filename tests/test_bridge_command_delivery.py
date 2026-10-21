"""Session round-trip: a queued application-command body must be delivered as a
DL 0x74 frame in the sensor's 0x53 command window, framed as lorabrd does in
every keyhook capture: seq_hi = gateway DL count, seq_lo = 0xC0 | acked UL
seq_hi (0x63 ACKs: 0x80 | seq_hi), counter = shared DL counter.

This exercises the real delivery path (_command_window -> _build_command) that
carries LOCATE / REBOOT / DEVICE_INFO_REQUEST / PROPERTY_REQUEST / PROPERTY_SET /
PING to hardware — the same path FACTORY_RESET was verified on.
"""
import pytest

from superlink import appmsg
from superlink.bridge.core import OutgoingFrame
from superlink.bridge.session import DeviceSession, State
from superlink.bridge.store import DeviceRecord
from superlink.bridge.profiles import ProfileRegistry
from superlink.decoder import build_frame, compute_mic, parse_frame, decrypt_frame
from tests.fixtures.captured_frames import SENSOR_MAC, DEFAULT_PAIRING_KEY

GW_MAC = bytes.fromhex("AABBCCDDEE01")
SESSION_KEY = bytes(range(32))


def _active_adopted_session():
    s = DeviceSession(DeviceRecord(mac=SENSOR_MAC, adopted=True),
                      gw_mac=GW_MAC, pairing_key=DEFAULT_PAIRING_KEY,
                      profiles=ProfileRegistry.load())
    s.start()
    s._state = State.ACTIVE
    s.session_key = SESSION_KEY
    s.sensor_mac = SENSOR_MAC
    s._adopted = True
    s._ul_counter_offset = 0
    return s


def _feed_0x53(s, channel=1, seq_hi=0x22):
    """Feed a synthetic 0x53 mgmt poll (encrypted body, counter=0)."""
    header = bytes([0xE0, 0x53]) + SENSOR_MAC + bytes([seq_hi, 0x20])
    body = b"\x01\x00"
    raw = build_frame(0xE0, 0x53, SENSOR_MAC, seq_hi, 0x20,
                      compute_mic(header, body), body, SESSION_KEY, counter=0)
    return s.feed(parse_frame(raw), channel, now=1.0), seq_hi


COMMANDS = [
    ("locate", appmsg.encode_locate),
    ("reboot", appmsg.encode_reboot),
    ("device_info", appmsg.encode_device_info_request),
    ("ping", lambda tag: appmsg.encode_ping_request(tag, b"\xbe\xef")),
    ("property_request", lambda tag: appmsg.encode_property_request([1, 3], tag)),
    ("property_set", lambda tag: appmsg.encode_property_set([(14, 0, b"\x01")], tag)),
]


@pytest.mark.parametrize("name,encoder", COMMANDS, ids=[c[0] for c in COMMANDS])
def test_queued_command_delivered_in_0x53_window(name, encoder):
    s = _active_adopted_session()
    body = encoder(0x35)             # non-zero tag (sensor rejects tag 0)
    s.queue_body(body)

    (frames, _events), seq_hi = _feed_0x53(s)

    assert len(frames) == 1 and isinstance(frames[0], OutgoingFrame)
    tx = parse_frame(frames[0].data)
    assert tx.dctrl == 0x74, f"{name} must go out as a 0x74 command"
    assert tx.seq_hi == 0x01, "first DL of the session"
    assert tx.seq_lo == 0xC0 | (seq_hi & 0x3F), "acks the 0x53 poll"
    # counter 0 (first DL). Decrypt and confirm the body is verbatim.
    dec = decrypt_frame(tx, SESSION_KEY, ul_counter_offset=tx.seq_hi, dl_counter=0)
    assert dec.payload == body


def _feed_0x54(s, channel=1, seq_hi=0x23, seq_lo=0x81, ul_ctr=1):
    header = bytes([0xE0, 0x54]) + SENSOR_MAC + bytes([seq_hi, seq_lo])
    body = bytes.fromhex("0c000100000008")
    raw = build_frame(0xE0, 0x54, SENSOR_MAC, seq_hi, seq_lo,
                      compute_mic(header, body), body, SESSION_KEY, counter=ul_ctr)
    return s.feed(parse_frame(raw), channel, now=2.0)


def _assert_ack(frames, dl_seq, ul_seq_hi, dl_ctr):
    assert len(frames) == 1
    tx = parse_frame(frames[0].data)
    assert tx.dctrl == 0x63
    assert (tx.seq_hi, tx.seq_lo) == (dl_seq, 0x80 | (ul_seq_hi & 0x3F))
    dec = decrypt_frame(tx, SESSION_KEY, ul_counter_offset=tx.seq_hi,
                        dl_counter=dl_ctr)
    assert dec.payload == b"\x01\x00"


def test_empty_0x53_poll_is_acked():
    """No queued body: the confirmed 0x53 still gets lorabrd's bare 0x63 ACK."""
    s = _active_adopted_session()
    (frames, _), seq_hi = _feed_0x53(s)
    _assert_ack(frames, 0x01, seq_hi, dl_ctr=0)


def test_telemetry_acked_and_dl_counter_follows_pass2():
    """pass2: 0x74 01.c1 ctr0 -> UL 0x54 02.81 (acks it) -> 0x63 02.82 ctr1;
    pair7: UL 0x54 06.03 -> 0x63 05.86. High UL counters still decode."""
    s = _active_adopted_session()
    s.queue_body(appmsg.encode_device_info_request(0x37))
    (frames, _), _ = _feed_0x53(s)
    assert parse_frame(frames[0].data).dctrl == 0x74
    frames, _ = _feed_0x54(s, seq_hi=0x02, seq_lo=0x81, ul_ctr=1)
    _assert_ack(frames, 0x02, 0x02, dl_ctr=1)
    # Unconfirmed 0x63 does not advance the DL counter; high UL counters decode.
    frames, _ = _feed_0x54(s, seq_hi=0x07, seq_lo=0x2e, ul_ctr=200)
    _assert_ack(frames, 0x03, 0x07, dl_ctr=1)


def test_command_not_delivered_before_adoption():
    """Pre-adoption, a 0x53 poll is answered with the ADOPT_REQUEST, not a
    queued app command — the command window only opens once adopted."""
    s = _active_adopted_session()
    s._adopted = False               # not yet committed
    s.queue_body(appmsg.encode_locate(0x35))
    (frames, _), _ = _feed_0x53(s)
    # It replies (ADOPT_REQUEST), but the queued LOCATE stays pending.
    assert s._pending_bodies == [appmsg.encode_locate(0x35)]
