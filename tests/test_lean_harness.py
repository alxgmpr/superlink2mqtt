"""The correspondence harness must fail when production behavior regresses.

These fault-injection checks need Python dependencies but not a Lean toolchain.
The actual model vectors are built and checked separately by check_lean.py.
"""

import pytest

from tools.check_lean import appmsg, check_vector, decoder


def test_detects_nonce_padding_regression(monkeypatch):
    original = decoder.build_nonce

    def wrong_padding(*args, **kwargs):
        nonce = bytearray(original(*args, **kwargs))
        nonce[10] = 1
        return bytes(nonce)

    monkeypatch.setattr(decoder, "build_nonce", wrong_padding)
    with pytest.raises(AssertionError, match="nonce"):
        check_vector([
            "nonce", "224,84,0,1,2,3,4,5,6,7", "-1",
            "224,84,0,1,2,3,4,5,6,7,0,0,0,0,0,0,0,0,0,0,0,0,0,255",
        ])


def test_detects_short_frame_acceptance(monkeypatch):
    monkeypatch.setattr(decoder, "parse_frame", lambda raw: object())
    with pytest.raises(AssertionError, match="short frame"):
        check_vector(["short", "0,1,2"])


def test_detects_dctrl_classification_regression(monkeypatch):
    monkeypatch.setitem(decoder.DCTRL_TABLE, 0x54, ("DL", "data"))
    with pytest.raises(AssertionError, match="Dctrl classification"):
        check_vector([
            "frame", "224,84,0,1,2,3,4,5,6,7,1,2,3,4",
            "224,84,0,1,2,3,4,5,6,7", "1,2,3,4", "UL", "data",
        ])


def test_detects_tag_encoding_regression(monkeypatch):
    monkeypatch.setattr(appmsg, "encode_reboot", lambda tag: bytes([6, 0]))
    with pytest.raises(AssertionError, match="application encoder"):
        check_vector(["message", "6", "-1", "", "6,255"])


def test_detects_invalid_property_acceptance(monkeypatch):
    monkeypatch.setattr(appmsg, "encode_property_request", lambda ids, tag: b"")
    with pytest.raises(AssertionError, match="out-of-range property IDs accepted"):
        check_vector(["property", "1,256,2", "0", "reject"])


def test_unknown_vector_is_an_error():
    with pytest.raises(ValueError, match="unknown vector kind"):
        check_vector(["unexpected"])
