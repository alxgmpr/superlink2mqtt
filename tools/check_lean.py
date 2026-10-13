#!/usr/bin/env python3
"""Build Lean proofs, then compare executable model vectors to production code.

Run with the repository's Python environment; no radio or gateway is accessed.
Missing Lean, failed proofs, malformed output, and mismatches all fail the run.
"""

from collections import Counter
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "verification" / "lean"
sys.path.insert(0, str(ROOT / "tools" / "sx1302"))

from superlink import appmsg, decoder  # noqa: E402
import pysodium  # noqa: E402


def byte_list(value: str) -> bytes:
    return bytes(int(item) for item in value.split(",") if item)


def equal(actual, expected, context: str) -> None:
    # Explicit checks still run when Python is invoked with -O.
    if actual != expected:
        raise AssertionError(f"{context}: expected {expected!r}, got {actual!r}")


def check_vector(fields: list[str]) -> None:
    kind = fields[0]
    if kind == "nonce":
        _, header_csv, counter, expected_csv = fields
        h = byte_list(header_csv)
        expected = byte_list(expected_csv)
        args = (h[0], h[1], h[2:8], h[8], h[9])
        equal(decoder.build_nonce(*args, int(counter)), expected, "nonce")

        # Exercise the real frame builder and crypto call using the model nonce.
        # This checks wiring/layout; it is not a proof of the cipher itself.
        key = bytes(range(32))
        mic = bytes([0xA1, 0xB2, 0xC3, 0xD4])
        payload = bytes(range(h[8] % 33))
        wire = decoder.build_frame(*args, mic, payload, key, int(counter))
        equal(wire[:10], h, "build_frame header")
        plain = pysodium.crypto_stream_xor(wire[10:], len(wire) - 10, expected, key)
        equal(plain, mic + payload, "build_frame MIC/payload layout")
    elif kind == "frame":
        _, raw_csv, header_csv, encrypted_csv, direction, frame_type = fields
        frame = decoder.parse_frame(byte_list(raw_csv))
        if frame is None:
            raise AssertionError("parse_frame rejected a model-valid frame")
        h = byte_list(header_csv)
        equal((frame.mctrl, frame.dctrl, frame.mac, frame.seq_hi, frame.seq_lo),
              (h[0], h[1], h[2:8], h[8], h[9]), "parsed header")
        equal(frame.encrypted, byte_list(encrypted_csv), "opaque encrypted body")
        equal((frame.direction, frame.frame_type), (direction, frame_type),
              "Dctrl classification")
    elif kind == "short":
        _, raw_csv = fields
        equal(decoder.parse_frame(byte_list(raw_csv)), None, "short frame")
    elif kind == "message":
        _, id_text, tag_text, body_csv, expected_csv = fields
        message_id, tag = int(id_text), int(tag_text)
        body, expected = byte_list(body_csv), byte_list(expected_csv)
        encoders = {
            6: appmsg.encode_reboot,
            7: appmsg.encode_factory_reset,
            8: appmsg.encode_locate,
            9: appmsg.encode_device_info_request,
        }
        wire = (appmsg.encode_ping_request(tag, body) if message_id == 4
                else encoders[message_id](tag))
        equal(wire, expected, "application encoder")
        decoded = appmsg.decode_message(expected)
        equal((decoded["messageId"], decoded["messageTag"]),
              (expected[0], expected[1]), "application header decoder")
        equal(decoded["data" if message_id == 4 else "raw"], body,
              "application body decoder")
    elif kind == "property":
        _, ids_csv, tag_text, expected_csv = fields
        ids = [int(item) for item in ids_csv.split(",") if item]
        tag = int(tag_text)
        if expected_csv == "reject":
            try:
                appmsg.encode_property_request(ids, tag)
            except ValueError:
                return
            raise AssertionError(f"out-of-range property IDs accepted: {ids}")
        expected = byte_list(expected_csv)
        equal(appmsg.encode_property_request(ids, tag), expected, "property encoder")
        equal(appmsg.decode_message(expected),
              {"messageId": 11, "messageTag": expected[1], "propertyIds": ids},
              "property decoder")
    else:
        raise ValueError(f"unknown vector kind: {kind!r}")


def main() -> None:
    lake = shutil.which("lake")
    if lake is None:
        candidate = Path.home() / ".elan" / "bin" / "lake"
        if not candidate.is_file():
            raise SystemExit("Lean is required. Install elan; see verification/lean/README.md")
        lake = str(candidate)
    subprocess.run([lake, "build"], cwd=PROJECT, check=True)
    executable = PROJECT / ".lake" / "build" / "bin" / "vectors"
    result = subprocess.run([str(executable)], cwd=PROJECT, check=True,
                            capture_output=True, text=True)
    counts = Counter()
    for line_number, line in enumerate(result.stdout.splitlines(), 1):
        fields = line.split("\t")
        try:
            check_vector(fields)
        except Exception as exc:
            raise AssertionError(f"Lean vector {line_number} ({fields[0]}): {exc}") from exc
        counts[fields[0]] += 1
    # Fail closed if a generator accidentally emits nothing or loses a category.
    equal(dict(counts), {"nonce": 3586, "frame": 2563, "short": 14,
                         "message": 3850, "property": 265}, "vector coverage")
    print(f"Lean proofs and {sum(counts.values())} Python correspondence vectors passed.")
    for kind, count in sorted(counts.items()):
        print(f"  {kind}: {count}")


if __name__ == "__main__":
    main()
