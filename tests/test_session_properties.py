"""Generated pairing and reconnect sequences through DeviceSession's public API."""

import hashlib

import pysodium
from hypothesis import given, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from superlink.adopt import KDF_SALT_H
from superlink.bridge.events import DeviceStateEvent
from superlink.bridge.session import DeviceSession, State, LORA_DEVICE_DEFAULT_ADOPTION_KEY
from superlink.bridge.store import DeviceRecord
from superlink.decoder import parse_frame
from tests.fixtures.captured_frames import DEFAULT_PAIRING_KEY, SENSOR_MAC
from tests.support.sensor_peer import (
    FALLBACK_KEY, GW_MAC, PRIMARY_KEY, SENSOR_PRIVATE, challenge, discovery, unwrap, wire,
    deterministic_crypto, deterministic_entropy,
)


BYTE = st.integers(0, 255)
CHANNEL = st.integers(1, 8)
PRIVATE_KEY = st.binary(min_size=32, max_size=32)


def feed(session, raw, now, channel=1):
    return session.feed(parse_frame(raw), channel=channel, now=now)


def state_events(events):
    return [e.state for e in events if isinstance(e, DeviceStateEvent)]


def adopted_session(**kwargs):
    record = DeviceRecord(mac=SENSOR_MAC, adopted=True, primary_key=PRIMARY_KEY,
                          fallback_key=FALLBACK_KEY, kdf_context=PRIMARY_KEY,
                          transport_key=FALLBACK_KEY, prop_sizes={15: 1})
    return DeviceSession(record, GW_MAC, DEFAULT_PAIRING_KEY, **kwargs)


def complete_handshake(session, now, *, context=PRIMARY_KEY, transport=FALLBACK_KEY,
                       private=SENSOR_PRIVATE, channel=1, seq=16, value=bytes(4)):
    raw, key = challenge(session._pubkey, context=context, transport_key=transport,
                         private=private, seq_hi=seq, value=value)
    frames, events = feed(session, raw, now, channel)
    assert state_events(events) == ["active"]
    assert session._state == State.ACTIVE
    assert session.session_key == key
    assert len(frames) == 1
    body = unwrap(frames[0].data, transport)
    assert body[:2] == b"\x01\x03"
    inner = pysodium.crypto_stream_xor(body[2:], len(body) - 2, bytes(24), key)
    assert inner == GW_MAC + SENSOR_MAC + value
    return raw, key


@given(version=st.sampled_from([1, 2]), private=PRIVATE_KEY,
       fallback_private=PRIVATE_KEY, seq=BYTE, channel=CHANNEL,
       value=st.binary(min_size=4, max_size=4), retries=st.integers(1, 5))
@deterministic_crypto
def test_pairing_retries_commit_and_restored_reconnect(
        version, private, fallback_private, seq, channel, value, retries):
    session = DeviceSession(None, GW_MAC, DEFAULT_PAIRING_KEY)
    assert session.state == "idle"
    session.start(now=100)
    context = DEFAULT_PAIRING_KEY if version == 1 else LORA_DEVICE_DEFAULT_ADOPTION_KEY
    advertisement = discovery(version=version, seq_hi=seq)
    for attempt in range(retries):
        frames, events = feed(session, advertisement, 101 + attempt, channel)
        assert len(frames) == 1
        assert frames[0].data[1] == 0x62
        assert state_events(events) == []
        assert session.state == "adopting"
        assert session.session_key is None
        assert unwrap(frames[0].data, DEFAULT_PAIRING_KEY)[2:34] == session._pubkey

    challenge_raw, key = complete_handshake(
        session, 110, context=context, transport=DEFAULT_PAIRING_KEY,
        private=private, channel=channel, seq=seq, value=value)
    # Retrying the challenge while ACTIVE must not repeat the transition.
    assert feed(session, challenge_raw, 111, channel) == ([], [])
    assert session.session_key == key

    command = b"\x08\x01"
    session.queue_body(command)
    for attempt in range(retries):
        poll = wire(b"\x01\x00", dctrl=0x53, key=key, seq_hi=seq)
        frames, events = feed(session, poll, 112 + attempt, channel)
        assert len(frames) == 1
        request = unwrap(frames[0].data, key, counter=attempt)
        assert len(request) == 70 and request[0] == 2
        assert session._pending_bodies == [command]
        assert not session.to_record().adopted

    sensor_pub = pysodium.crypto_scalarmult_curve25519_base(private)
    sensor_fb_pub = pysodium.crypto_scalarmult_curve25519_base(fallback_private)
    primary = hashlib.blake2b(
        pysodium.crypto_scalarmult_curve25519(private, request[2:34])
        + request[2:34] + sensor_pub + KDF_SALT_H, digest_size=32).digest()
    fallback = hashlib.blake2b(
        pysodium.crypto_scalarmult_curve25519(fallback_private, request[34:66])
        + request[34:66] + sensor_fb_pub + KDF_SALT_H, digest_size=32).digest()
    response = bytes([3, request[1]]) + sensor_pub + sensor_fb_pub
    frames, events = feed(session, wire(response, dctrl=0x54, key=key,
                                        seq_hi=seq, counter=5), 120, channel)
    assert len(frames) == 1 and frames[0].data[1] == 0x63
    assert unwrap(frames[0].data, key, (seq - 1) & 255) == b"\x01\x00"
    assert state_events(events) == []
    assert session._state == State.BEACONING
    assert not session.to_record().adopted
    assert session.to_record().transport_key == DEFAULT_PAIRING_KEY
    assert session.to_record().primary_key == primary
    assert session.to_record().fallback_key == fallback

    # Lost commit / repeated unadopted advertisements cannot rotate transport keys.
    for attempt in range(retries):
        feed(session, advertisement, 121 + attempt, channel)
        assert not session.to_record().adopted
        assert session.to_record().transport_key == DEFAULT_PAIRING_KEY
    committed = discovery(version=version, adopted=True)
    frames, events = feed(session, committed, 130, channel)
    assert state_events(events) == ["adopted"]
    assert unwrap(frames[0].data, fallback)[:2] == b"\x01\x01"
    record = session.to_record()
    assert record.adopted and record.kdf_context == primary
    assert record.transport_key == fallback
    assert session._pending_bodies == [command]
    assert "adopted" not in state_events(feed(session, committed, 131, channel)[1])

    # Reconstruct via the same record API used after restart; no state injection.
    restored = DeviceSession(record, GW_MAC, DEFAULT_PAIRING_KEY)
    restored.start(now=200)
    complete_handshake(restored, 201, context=primary, transport=fallback,
                       private=private, channel=channel, value=value)
    assert restored.to_record().adopted


@given(payload=st.binary(max_size=48), channel=CHANNEL, seq=BYTE)
@deterministic_crypto
def test_short_challenge_never_activates_or_rotates_keys(payload, channel, seq):
    session = adopted_session(watchdog_timeout=0)
    session.start(now=100)
    before = session.to_record()
    raw = wire(payload, dctrl=0x42, key=FALLBACK_KEY, seq_hi=seq)
    _, events = feed(session, raw, 101, channel)
    assert session._state == State.BEACONING
    assert session.session_key is None
    assert state_events(events) == []
    assert session.to_record().primary_key == before.primary_key
    assert session.to_record().transport_key == before.transport_key


@given(public=st.sampled_from([bytes(32), b"\x01" + bytes(31)]),
       blob=st.binary(min_size=10, max_size=10), seq=BYTE, channel=CHANNEL)
@deterministic_crypto
def test_invalid_dh_public_key_is_rejected_without_partial_session_state(
        public, blob, seq, channel):
    session = adopted_session()
    session.start(now=100)
    before = session.to_record()
    payload = b"\x01\x02\x01" + public + blob + b"\x03\xfe\xff\x03"
    assert feed(session, wire(payload, dctrl=0x42, key=FALLBACK_KEY,
                              seq_hi=seq), 101, channel) == ([], [])
    assert session._state == State.BEACONING
    assert session.session_key is None
    assert session._remote_pubkey is None
    assert session.to_record() == before


@given(threshold=st.integers(1, 6), channel=CHANNEL, seq=BYTE,
       queued=st.lists(st.binary(min_size=2, max_size=16), max_size=5))
@deterministic_crypto
def test_resume_retry_watchdog_preserves_queue_and_persistent_keys(
        threshold, channel, seq, queued):
    session = adopted_session(watchdog_timeout=0, watchdog_short_challenge_k=threshold)
    session.start(now=100)
    for body in queued:
        session.queue_body(body)
    old_public = session._pubkey
    for attempt in range(threshold):
        raw = wire(b"\x01\x00", dctrl=0x42, key=FALLBACK_KEY, seq_hi=seq)
        frames, _ = feed(session, raw, 101 + attempt, channel)
        assert len(frames) == 1
        assert unwrap(frames[0].data, FALLBACK_KEY)[:2] == b"\x01\x01"
        _, events = session.tick(101 + attempt)
        assert state_events(events) == (["lost"] if attempt == threshold - 1 else [])
        if attempt < threshold - 1:
            assert session._pubkey == old_public
    assert session._pubkey != old_public
    assert session._pending_bodies == queued
    assert session.to_record().primary_key == PRIMARY_KEY
    assert session.to_record().transport_key == FALLBACK_KEY
    fresh_public = session._pubkey
    assert session.tick(101 + threshold) == ([], [])
    assert session._pubkey == fresh_public


@given(kind=st.sampled_from([0x54, 0x44, 0x53]), counter=st.integers(0, 63),
       command=st.binary(min_size=2, max_size=32), channel=CHANNEL)
@deterministic_crypto
def test_bad_mic_cannot_emit_events_or_consume_a_command(kind, counter, command, channel):
    session = adopted_session()
    session.start(now=100)
    _, key = complete_handshake(session, 101)
    session.queue_body(command)
    raw = bytearray(wire(b"\x05\x01ok", dctrl=kind, key=key,
                         counter=counter, seq_hi=16 + counter))
    raw[10] ^= 1
    frames, events = session.feed(parse_frame(bytes(raw)), channel, now=105, rssi=-50)
    assert frames == [] and events == []
    assert session._pending_bodies == [command]
    assert session._last_data_rx == 101
    assert session._state == State.ACTIVE


@given(counter=st.integers(64, 239), channel=CHANNEL)
@deterministic_crypto
def test_verified_telemetry_beyond_counter_scan_still_refreshes_liveness(counter, channel):
    session = adopted_session()
    session.start(now=100)
    _, key = complete_handshake(session, 101, seq=16)
    raw = wire(b"\x05\x01ok", dctrl=0x54, key=key,
               counter=counter, seq_hi=16 + counter)
    feed(session, raw, 150, channel)
    assert session._last_data_rx == 150
    session.tick(now=200)
    assert session._state == State.ACTIVE


@given(invalid_fallback=st.booleans(), private=PRIVATE_KEY, channel=CHANNEL)
@deterministic_crypto
def test_invalid_adoption_key_does_not_partially_replace_persistent_keys(
        invalid_fallback, private, channel):
    session = DeviceSession(None, GW_MAC, DEFAULT_PAIRING_KEY)
    session.start(now=100)
    _, key = complete_handshake(session, 101, context=DEFAULT_PAIRING_KEY,
                                transport=DEFAULT_PAIRING_KEY)
    frames, _ = feed(session, wire(b"\x01\x00", dctrl=0x53, key=key), 102, channel)
    request = unwrap(frames[0].data, key)
    public = pysodium.crypto_scalarmult_curve25519_base(private)
    primary, fallback = (public, bytes(32)) if invalid_fallback else (bytes(32), public)
    body = bytes([3, request[1]]) + primary + fallback
    before = session.to_record()
    assert feed(session, wire(body, dctrl=0x54, key=key), 103, channel) == ([], [])
    assert session.to_record() == before
    assert session._state == State.ACTIVE and session.session_key == key
    assert session._eph_priv_r is not None and session._eph_priv_o is not None
    retry = bytes([3, request[1]]) + public + public
    frames, _ = feed(session, wire(retry, dctrl=0x54, key=key), 104, channel)
    assert len(frames) == 1 and frames[0].data[1] == 0x63
    assert session._state == State.BEACONING and session._adopt_pending
    assert session._eph_priv_r is None and session._eph_priv_o is None


@given(threshold=st.integers(1, 5), timeout=st.integers(1, 10),
       cooldown=st.integers(1, 30), channel=CHANNEL)
@deterministic_crypto
def test_reconnect_cooldown_blocks_all_handshake_retries_until_exact_deadline(
        threshold, timeout, cooldown, channel):
    session = adopted_session(link_lost_timeout=timeout, watchdog_timeout=0,
                               watchdog_short_challenge_k=0,
                               reconnect_storm_k=threshold,
                               reconnect_storm_window=threshold * timeout,
                               reconnect_backoff=cooldown)
    now = 100
    session.start(now=now)
    for _ in range(threshold):
        complete_handshake(session, now, channel=channel)
        now += timeout
        assert state_events(session.tick(now)[1]) == ["lost"]
    deadline = now + cooldown
    assert session._backoff_until == deadline
    raw, _ = challenge(session._pubkey, context=PRIMARY_KEY, transport_key=FALLBACK_KEY)
    for retry in [discovery(adopted=True), raw,
                  wire(b"\x01\x00", dctrl=0x42, key=FALLBACK_KEY)]:
        assert feed(session, retry, deadline - 1, channel) == ([], [])
        assert session.session_key is None
    complete_handshake(session, deadline, channel=channel)


@given(timeout=st.integers(1, 300), private=PRIVATE_KEY,
       queued=st.lists(st.binary(min_size=2, max_size=16), max_size=5))
@deterministic_crypto
def test_silence_watchdog_rearms_once_and_allows_real_reconnect(timeout, private, queued):
    session = adopted_session(watchdog_timeout=timeout)
    session.start(now=100)
    for command in queued:
        session.queue_body(command)
    before = session.to_record()
    public = session._pubkey
    assert session.tick(100 + timeout - 1) == ([], [])
    assert session._pubkey == public
    assert state_events(session.tick(100 + timeout)[1]) == ["lost"]
    assert session._pubkey != public
    assert session.to_record() == before
    assert session._pending_bodies == queued
    fresh_public = session._pubkey
    assert session.tick(100 + timeout) == ([], [])
    assert session._pubkey == fresh_public
    complete_handshake(session, 100 + timeout, private=private)


class ReconnectMachine(RuleBasedStateMachine):
    """Queue/liveness/cooldown oracle driven by generated event sequences.

    No assignment to production state: ACTIVE is reached through real DH frames.
    Watchdog is covered separately so its independent clock doesn't obscure
    link-loss and storm-backoff boundary checks here.
    """

    def __init__(self):
        super().__init__()
        self.entropy = deterministic_entropy()
        self.entropy.__enter__()
        self.now = 100
        self.session = adopted_session(link_lost_timeout=10, watchdog_timeout=0,
                                       watchdog_short_challenge_k=0,
                                       reconnect_storm_k=3, reconnect_storm_window=60,
                                       reconnect_backoff=20)
        self.session.start(now=self.now)
        self.active = False
        self.last_data = self.now
        self.losses = []
        self.backoff_until = 0
        self.queue = []
        self.key = None

    def teardown(self):
        self.entropy.__exit__(None, None, None)

    @rule(body=st.binary(min_size=2, max_size=32))
    def queue_command(self, body):
        self.session.queue_body(body)
        self.queue.append(body)

    @rule(elapsed=st.sampled_from([0, 1, 9, 10, 11, 19, 20, 21, 61]))
    def advance_time(self, elapsed):
        self.now += elapsed
        lost = self.active and self.now - self.last_data >= 10
        if lost:
            self.active = False
            self.key = None
            self.losses = [t for t in self.losses if self.now - t <= 60] + [self.now]
            if len(self.losses) >= 3:
                self.backoff_until = self.now + 20
                self.losses = []
        frames, events = self.session.tick(self.now)
        assert frames == []
        assert state_events(events) == (["lost"] if lost else [])

    @precondition(lambda self: not self.active)
    @rule(channel=CHANNEL, seq=BYTE)
    def discovery_or_retry(self, channel, seq):
        frames, events = feed(self.session, discovery(adopted=True, seq_hi=seq),
                              self.now, channel)
        assert len(frames) == (0 if self.now < self.backoff_until else 1)
        assert state_events(events) == []

    @precondition(lambda self: not self.active)
    @rule(channel=CHANNEL, seq=BYTE, private=PRIVATE_KEY)
    def connection_challenge(self, channel, seq, private):
        if self.now < self.backoff_until:
            raw, _ = challenge(self.session._pubkey, context=PRIMARY_KEY,
                               transport_key=FALLBACK_KEY, private=private, seq_hi=seq)
            assert feed(self.session, raw, self.now, channel) == ([], [])
        else:
            _, self.key = complete_handshake(self.session, self.now,
                                             private=private, channel=channel, seq=seq)
            self.active = True
            self.last_data = self.now

    @precondition(lambda self: self.active)
    @rule(channel=CHANNEL, seq=BYTE, counter=st.integers(0, 63),
          kind=st.sampled_from([0x54, 0x44, 0x53]))
    def receive_window(self, channel, seq, counter, kind):
        raw = wire(b"\x0c\x00\x0f\x00\x01", dctrl=kind, key=self.key,
                   seq_hi=seq, counter=counter)
        frames, _ = feed(self.session, raw, self.now, channel)
        if kind in (0x54, 0x44):
            self.last_data = self.now
        if self.queue:
            assert len(frames) == 1
            assert unwrap(frames[0].data, self.key) == self.queue.pop(0)
        else:
            assert frames == []

    @precondition(lambda self: self.active)
    @rule(kind=st.sampled_from([0x54, 0x44, 0x53]), seq=BYTE, channel=CHANNEL)
    def corrupt_packet(self, kind, seq, channel):
        raw = bytearray(wire(b"\x0c\x00\x0f\x00\x01", dctrl=kind,
                             key=self.key, seq_hi=seq))
        raw[10] ^= 1  # Corrupt only the MIC; plaintext still looks meaningful.
        frames, events = feed(self.session, bytes(raw), self.now, channel)
        assert frames == []
        assert events == []

    @rule(raw=st.binary(max_size=13))
    def truncated_radio_packet(self, raw):
        assert feed(self.session, raw, self.now) == ([], [])

    @invariant()
    def state_keys_and_queue_agree(self):
        assert self.session._state == (State.ACTIVE if self.active else State.BEACONING)
        assert self.session.session_key == self.key
        assert self.session._last_data_rx == self.last_data
        assert self.session._pending_bodies == self.queue
        assert self.session._backoff_until == self.backoff_until
        record = self.session.to_record()
        assert record.mac == SENSOR_MAC and record.adopted
        assert (record.primary_key, record.fallback_key) == (PRIMARY_KEY, FALLBACK_KEY)
        assert (record.kdf_context, record.transport_key) == (PRIMARY_KEY, FALLBACK_KEY)
        assert record.prop_sizes == {15: 1}


TestReconnectSequences = ReconnectMachine.TestCase
