"""Offline sensor-side wire helpers, independent of the gateway's encoders.

Uses real libsodium and hashlib with synthetic keys. No HAL, sockets, or radio.
"""

import hashlib
from contextlib import contextmanager
from functools import wraps
import random
from unittest.mock import patch

import pysodium

from tests.fixtures.captured_frames import DEFAULT_PAIRING_KEY, SENSOR_MAC


GW_MAC = bytes.fromhex("aabbccddeeff")
PRIMARY_KEY = bytes(range(32))
FALLBACK_KEY = bytes(range(32, 64))
SENSOR_PRIVATE = bytes(range(64, 96))


@contextmanager
def deterministic_entropy():
    """Keep Hypothesis replay/shrinking stable while still using real crypto."""
    rng = random.Random(0)

    def keypair():
        private = rng.randbytes(32)
        return private, pysodium.crypto_scalarmult_curve25519_base(private)

    with patch("superlink.bridge.session.generate_keypair", side_effect=keypair), \
            patch("superlink.bridge.session.secrets.token_bytes", side_effect=rng.randbytes):
        yield


def deterministic_crypto(test):
    @wraps(test)
    def run(*args, **kwargs):
        with deterministic_entropy():
            return test(*args, **kwargs)
    return run


def wire(payload, *, dctrl, key, mac=SENSOR_MAC, seq_hi=16, seq_lo=0, counter=0):
    header = bytes([0xE0, dctrl]) + mac + bytes([seq_hi, seq_lo])
    mic = hashlib.blake2b(header + bytes(4) + payload, digest_size=4).digest()
    nonce = header + bytes(13) + bytes([counter & 255])
    plaintext = mic + payload
    return header + pysodium.crypto_stream_xor(plaintext, len(plaintext), nonce, key)


def unwrap(raw, key, counter=0):
    nonce = raw[:10] + bytes(13) + bytes([counter & 255])
    plaintext = pysodium.crypto_stream_xor(raw[10:], len(raw) - 10, nonce, key)
    expected = hashlib.blake2b(raw[:10] + bytes(4) + plaintext[4:], digest_size=4).digest()
    assert plaintext[:4] == expected
    return plaintext[4:]


def discovery(*, version=1, adopted=False, mac=SENSOR_MAC, seq_hi=1):
    payload = bytes([version, 0xAE, 0x94, 0x86 if adopted else 6])
    payload += (0x048F if adopted else 0).to_bytes(4, "big")
    if version == 2:
        payload += b"\x00\x02"
    return wire(payload, dctrl=0x40, key=DEFAULT_PAIRING_KEY, mac=mac, seq_hi=seq_hi)


def challenge(gateway_public, *, context, transport_key, private=SENSOR_PRIVATE,
              mac=SENSOR_MAC, value=bytes(4), seq_hi=16, seq_lo=0):
    public = pysodium.crypto_scalarmult_curve25519_base(private)
    shared = pysodium.crypto_scalarmult_curve25519(private, gateway_public)
    key = hashlib.blake2b(shared + gateway_public + public + context,
                          digest_size=32).digest()
    blob = pysodium.crypto_stream_xor(mac + value, 10, bytes(24), key)
    payload = b"\x01\x02\x01" + public + blob + b"\x03\xfe\xff\x03"
    return wire(payload, dctrl=0x42, key=transport_key, mac=mac,
                seq_hi=seq_hi, seq_lo=seq_lo), key
