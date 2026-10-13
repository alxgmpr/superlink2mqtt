"""Bounded SLK1 → WebSocket → UCP4 decoder for the migration capture helper.

Only complete, acknowledged persistent addDevice pairs are eligible. No raw
plaintext or key material belongs in exceptions, status responses, or logs.
"""
from dataclasses import dataclass, field
import json
import struct
import zlib

from ..config import DEFAULT_PAIRING_KEY
from ..importing import parse_import, parse_mac

MAX_CAPTURE = 16 * 1024 * 1024
MAX_MESSAGE = 256 * 1024


class CaptureError(ValueError):
    pass


def invalid():
    return CaptureError("Capture was incomplete or used unsupported framing; retry extraction")


def json_object(raw):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise invalid()
            result[k] = v
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise invalid() from None
    if not isinstance(value, dict):
        raise invalid()
    return value


@dataclass
class Stream:
    buffer: bytearray = field(default_factory=bytearray)
    fragment: bytearray = field(default_factory=bytearray)
    application: bytearray = field(default_factory=bytearray)
    envelope: dict | None = None
    fragmented: bool = False
    compressed: bool = False
    inflater: object = None

    def feed(self, chunk, *, deflate, no_context):
        self.buffer.extend(chunk)
        if len(self.buffer) > MAX_MESSAGE + 14:
            raise invalid()
        while len(self.buffer) >= 2:
            first, second = self.buffer[:2]
            final, compressed, opcode = bool(first & 128), bool(first & 64), first & 15
            if first & 48 or opcode not in (0, 2, 8, 9, 10):
                raise invalid()
            size, offset = second & 127, 2
            extra = 2 if size == 126 else 8 if size == 127 else 0
            if len(self.buffer) < offset + extra:
                return
            if extra:
                size = int.from_bytes(self.buffer[offset:offset + extra], "big")
                offset += extra
            if size > MAX_MESSAGE:
                raise invalid()
            masked = bool(second & 128)
            if masked:
                offset += 4
            if len(self.buffer) < offset + size:
                return
            payload = bytes(self.buffer[offset:offset + size])
            if masked:
                mask = self.buffer[offset - 4:offset]
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            del self.buffer[:offset + size]
            if opcode >= 8:
                if not final or compressed or size > 125:
                    raise invalid()
                continue
            if opcode == 2:
                if self.fragmented:
                    raise invalid()
                self.compressed = compressed
            elif not self.fragmented or compressed:
                raise invalid()
            self.fragment.extend(payload)
            if len(self.fragment) > MAX_MESSAGE:
                raise invalid()
            self.fragmented = not final
            if not final:
                continue
            payload = bytes(self.fragment)
            self.fragment.clear()
            if self.compressed:
                if not deflate:
                    raise invalid()
                if self.inflater is None or no_context:
                    self.inflater = zlib.decompressobj(-15)
                try:
                    payload = self.inflater.decompress(payload + b"\x00\x00\xff\xff", MAX_MESSAGE + 1)
                except zlib.error:
                    raise invalid() from None
                if len(payload) > MAX_MESSAGE or self.inflater.unconsumed_tail or self.inflater.unused_data:
                    raise invalid()
            self.application.extend(payload)
            if len(self.application) > MAX_MESSAGE:
                raise invalid()
            while len(self.application) >= 8:
                header = self.application[:8]
                if header[0] not in (1, 2) or header[1:6] != b"\x01\0\0\0\0":
                    raise invalid()
                length = int.from_bytes(header[6:8], "big")
                if len(self.application) < length + 8:
                    break
                obj = json_object(bytes(self.application[8:8 + length]))
                del self.application[:8 + length]
                if header[0] == 1:
                    if self.envelope is not None:
                        raise invalid()
                    self.envelope = obj
                else:
                    if self.envelope is None:
                        raise invalid()
                    yield self.envelope, obj
                    self.envelope = None

    def finish(self):
        if self.buffer or self.fragmented or self.application or self.envelope is not None:
            raise invalid()


@dataclass
class Connection:
    headers: dict = field(default_factory=dict)
    buffers: dict = field(default_factory=lambda: {"R": bytearray(), "W": bytearray()})
    chunks: list = field(default_factory=list)

    def feed(self, direction, data):
        if direction in self.headers:
            self.chunks.append((direction, data))
            return
        buf = self.buffers[direction]
        buf.extend(data)
        split = buf.find(b"\r\n\r\n")
        if split < 0:
            if len(buf) > 16384:
                raise invalid()
            return
        if split > 16384:
            raise invalid()
        self.headers[direction] = bytes(buf[:split]).lower()
        if buf[split + 4:]:
            self.chunks.append((direction, bytes(buf[split + 4:])))
        buf.clear()

    def pairs(self):
        # Ignore unrelated TLS streams, but never a partially captured UCP4 stream.
        if not any(b"ucp4" in h for h in self.headers.values()):
            return
        if len(self.headers) != 2:
            raise invalid()
        request_dir = next((d for d, h in self.headers.items() if h.startswith(b"get ")), None)
        response_dir = next((d for d, h in self.headers.items() if h.startswith(b"http/1.1 101 ")), None)
        if request_dir is None or response_dir is None or request_dir == response_dir:
            raise invalid()
        response = self.headers[response_dir]
        if b"sec-websocket-protocol: ucp4" not in response:
            raise invalid()
        extension = next((line.split(b":", 1)[1].strip() for line in response.split(b"\r\n")
                          if line.startswith(b"sec-websocket-extensions:")), b"")
        deflate = bool(extension)
        if deflate and (not extension.startswith(b"permessage-deflate") or b"," in extension):
            raise invalid()
        streams = {d: Stream() for d in ("R", "W")}
        for direction, data in self.chunks:
            role = b"client" if direction == request_dir else b"server"
            for env, pay in streams[direction].feed(data, deflate=deflate,
                    no_context=role + b"_no_context_takeover" in extension):
                yield direction, env, pay
        for stream in streams.values():
            stream.finish()


def decode_capture(raw):
    """Return a server-private migration document and a public exclusion count."""
    if not raw or len(raw) > MAX_CAPTURE + 20:
        raise invalid()
    active, connections = {}, []
    offset = 0
    while offset < len(raw):
        if len(raw) - offset < 20:
            raise invalid()
        magic, direction, reserved, pointer, size = struct.unpack_from("<4sc3sQI", raw, offset)
        offset += 20
        if magic != b"SLK1" or reserved != bytes(3) or size > 65536 or offset + size > len(raw):
            raise invalid()
        data = raw[offset:offset + size]
        offset += size
        if direction == b"N" and not size and pointer not in active:
            conn = Connection()
            active[pointer] = conn
            connections.append(conn)
            if len(connections) > 128:
                raise invalid()
        elif direction == b"F" and not size and pointer in active:
            del active[pointer]
        elif direction in (b"R", b"W") and size and pointer in active:
            active[pointer].feed(direction.decode(), data)
        else:
            raise invalid()
    # Keep each lifetime separate. Different candidate pairs on separate
    # connections are ambiguous, regardless of logging order.
    candidates, excluded, revoked = {}, set(), set()
    for conn in connections:
        current, requests = {}, {}
        for direction, env, pay in conn.pairs():
            kind, action, ident = env.get("type"), env.get("action"), env.get("id")
            if kind == "request" and action in ("addDevice", "removeDevice"):
                try:
                    mac = parse_mac(pay.get("mac")).hex()
                except ValueError:
                    raise invalid() from None
                excluded.add(mac)
                current[mac] = None
                if action == "removeDevice":
                    # A later remove invalidates keys even across TLS lifetimes.
                    revoked.add(mac)
                    continue
                try:
                    rec, = parse_import({"version": 1, "devices": [pay]})
                except ValueError:
                    continue
                if rec.primary_key == DEFAULT_PAIRING_KEY or rec.fallback_key == DEFAULT_PAIRING_KEY:
                    continue
                if not isinstance(ident, str) or not ident or len(ident) > 128 or (direction, ident) in requests:
                    raise invalid()
                item = {"mac": mac, "key": rec.primary_key.hex(), "fallbackKey": rec.fallback_key.hex()}
                if rec.name:
                    item["name"] = rec.name
                if rec.network_id is not None:
                    item["networkId"] = rec.network_id
                current[mac] = (item, False, ident)
                requests[direction, ident] = mac
                if len(requests) > 4096 or len(current) > 100:
                    raise invalid()
            elif kind == "response" and isinstance(ident, str):
                other = "W" if direction == "R" else "R"
                mac = requests.get((other, ident))
                if mac is None or current.get(mac) is None or current[mac][2] != ident:
                    continue
                item, _, _ = current[mac]
                # An absent errorCode is permitted by UCP4, an error string is not.
                accepted = not env.get("error") and env.get("errorCode", 0) == 0
                current[mac] = (item, accepted, ident)
        for mac, candidate in current.items():
            if candidate is None or not candidate[1]:
                candidates.setdefault(mac, []).append(None)
            else:
                candidates.setdefault(mac, []).append(candidate[0])
    selected = []
    for mac, choices in candidates.items():
        first = choices[0]
        if mac not in revoked and first is not None and all(c is not None and c["key"] == first["key"] and
                                    c["fallbackKey"] == first["fallbackKey"] for c in choices):
            selected.append(first)
            excluded.discard(mac)
    if not selected:
        raise CaptureError("No acknowledged persistent key pairs found. Keep the UniFi controller connected and retry; do not reset or re-adopt sensors")
    selected.sort(key=lambda d: d["mac"])
    return {"version": 1, "devices": selected}, len(excluded)
