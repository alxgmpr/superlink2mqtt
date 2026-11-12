"""Tests for SuperLink frame decoder."""

from superlink.decoder import parse_frame, build_nonce, format_mac, interpret_payload


def test_parse_frame_too_short():
    assert parse_frame(b"\x00" * 13) is None
    assert parse_frame(b"") is None


def test_format_mac():
    assert format_mac(bytes.fromhex("9041B22E9A53")) == "90:41:B2:2E:9A:53"


def test_build_nonce():
    nonce = build_nonce(0xE0, 0x54, bytes.fromhex("9041B22E9A53"), 0x5B, 0x11)
    assert len(nonce) == 24
    assert nonce[:10] == bytes.fromhex("E0549041B22E9A535B11")
    assert nonce[10:] == b"\x00" * 14


def test_parse_frame_dl():
    raw = bytearray(14)
    raw[0] = 0xE0
    raw[1] = 0x63
    frame = parse_frame(bytes(raw))
    assert frame.direction == "DL"
    assert frame.frame_type == "data"


def test_parse_frame_unknown_dctrl():
    raw = bytearray(14)
    raw[1] = 0xFF
    frame = parse_frame(bytes(raw))
    assert frame.direction == "??"
    assert frame.frame_type == "unknown"


def test_interpret_payload_door_open():
    assert interpret_payload(0x54, bytes([0x0C, 0x00, 0x0F, 0x00, 0x00])) == "DOOR OPEN"


def test_interpret_payload_door_closed():
    assert interpret_payload(0x54, bytes([0x0C, 0x00, 0x0F, 0x00, 0x01])) == "DOOR CLOSED"


def test_interpret_payload_too_short():
    assert interpret_payload(0x54, bytes([0x0C, 0x00])) is None
    assert interpret_payload(0x54, b"") is None
