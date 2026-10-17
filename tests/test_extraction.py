"""Synthetic controller traffic and fake SSH only; never contacts bench devices."""
import json
import secrets
from pathlib import Path
import struct
import subprocess
import threading
import time
import zlib

import pytest

from superlink.bridge.extraction.decoder import CaptureError, decode_capture
from superlink.bridge.extraction.jobs import ExtractionJobs
from superlink.bridge.extraction import jobs, ssh
from superlink.bridge.extraction.build import NATIVE
from superlink.bridge.config import DEFAULT_PAIRING_KEY
from tests.test_management import bundle

ITEM = bundle()["devices"][0]
# Disposable fixture value: this suite never opens a network connection.
FAKE_SSH_PASSWORD = secrets.token_urlsafe(24)


def record(direction, data=b"", pointer=123):
    return struct.pack("<4sc3sQI", b"SLK1", direction.encode(), bytes(3), pointer, len(data)) + data


def pair(env, pay):
    def part(kind, obj):
        data = json.dumps(obj, separators=(",", ":")).encode()
        return bytes([kind, 1, 0, 0, 0, 0]) + len(data).to_bytes(2, "big") + data
    return part(1, env) + part(2, pay)


def add(ident="one", item=None):
    return pair({"type": "request", "action": "addDevice", "id": ident}, ITEM if item is None else item)


def response(ident="one", **extra):
    return pair({"type": "response", "id": ident, **extra}, {})


def frame(data, *, compressed=False, masked=False, opcode=2, final=True):
    size = len(data)
    head = bytes([(128 if final else 0) | (64 if compressed else 0) | opcode])
    head += bytes([(128 if masked else 0) | (size if size < 126 else 126)])
    if size >= 126:
        head += size.to_bytes(2, "big")
    if masked:
        head += b"test"
        data = bytes(b ^ b"test"[i % 4] for i, b in enumerate(data))
    return head + data


def capture(messages=None, *, compressed=False, fragment=False):
    request_header = b"GET /device HTTP/1.1\r\nUpgrade: websocket\r\nSec-WebSocket-Protocol: ucp4\r\n\r\n"
    response_header = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nSec-WebSocket-Protocol: ucp4\r\n"
    if compressed:
        response_header += b"Sec-WebSocket-Extensions: permessage-deflate\r\n"
    raw = record("N") + record("W", request_header[:17]) + record("W", request_header[17:]) + record("R", response_header + b"\r\n")
    compressors = {d: zlib.compressobj(wbits=-15) for d in ("R", "W")}
    for direction, data in messages or [("R", add()), ("W", response())]:
        if compressed:
            compressor = compressors[direction]
            data = (compressor.compress(data) + compressor.flush(zlib.Z_SYNC_FLUSH))[:-4]
        if fragment:
            wire = frame(data[:10], compressed=compressed, masked=True, final=False)
            wire += frame(b"ping", opcode=9)
            wire += frame(data[10:], opcode=0, masked=True)
        else:
            wire = frame(data, compressed=compressed, masked=direction == "R")
        # Deliberately split inside headers and payloads across SSL calls.
        for offset in range(0, len(wire), 7):
            raw += record(direction, wire[offset:offset + 7])
    return raw + record("F")


@pytest.mark.parametrize("compressed,fragment", [(False, False), (True, False), (False, True), (True, True)])
def test_complete_pairs_across_ssl_chunks_ws_fragments_and_deflate_context(compressed, fragment):
    doc, excluded = decode_capture(capture([
        ("R", add()), ("W", response()),
        ("R", add("two")), ("W", response("two")),
    ], compressed=compressed, fragment=fragment))
    assert doc == bundle()
    assert excluded == 0


def test_ucp4_pairs_can_span_websocket_messages_or_be_coalesced():
    data = add()
    doc, _ = decode_capture(capture([("R", data[:50]), ("R", data[50:] + add("two")),
                                     ("W", response() + response("two"))]))
    assert doc == bundle()


@pytest.mark.parametrize("messages", [
    [("R", add())],  # no response
    [("R", add()), ("R", response())],  # response in wrong direction
    [("R", add()), ("W", response(error="denied", errorCode=4))],
    [("R", add()), ("W", response(errorCode=4))],
    [("R", add()), ("W", response()), ("R", add("new"))],
    [("R", add()), ("R", add("new")), ("W", response())],  # stale ack
    [("R", add()), ("W", response()), ("R", add("bad", {"mac": ITEM["mac"], "key": ITEM["key"]}))],
    [("R", add()), ("W", response()), ("R", pair({"type": "request", "action": "removeDevice"}, {"mac": ITEM["mac"]}))],
    [("R", add(item={**ITEM, "key": DEFAULT_PAIRING_KEY.hex()})), ("W", response())],
])
def test_never_returns_unacknowledged_removed_rotated_or_factory_keys(messages):
    with pytest.raises(CaptureError, match="No acknowledged"):
        decode_capture(capture(messages))


def test_latest_acknowledged_rotation_wins_only_within_one_connection():
    updated = {**ITEM, "key": "33" * 32}
    doc, _ = decode_capture(capture([("R", add()), ("W", response()),
                                    ("R", add("new", updated)), ("W", response("new"))]))
    assert doc["devices"] == [updated]
    # Reused SSL pointer is a separate lifetime; conflicting pairs are ambiguous.
    with pytest.raises(CaptureError, match="No acknowledged"):
        decode_capture(capture() + capture([("R", add(item=updated)), ("W", response())]))


def test_exclusions_are_counted_and_never_leak_candidate_secrets():
    other = {**ITEM, "mac": "020000000008", "key": "44" * 32}
    doc, excluded = decode_capture(capture([("R", add()), ("W", response()), ("R", add("pending", other))]))
    assert excluded == 1
    assert "44" * 32 not in json.dumps(doc)


@pytest.mark.parametrize("damage", ["record", "payload", "overflow", "framing", "duplicate_json", "zip_bomb"])
def test_incomplete_ambiguous_and_oversized_captures_fail_closed(damage):
    raw = capture()
    if damage == "record":
        raw = raw[:-1]
    elif damage == "payload":
        raw = raw[:-20] + record("R", frame(add())[:-1])
    elif damage == "overflow":
        raw += record("E")
    elif damage == "framing":
        raw = capture([("R", add()[:3] + b"!" + add()[4:]), ("W", response())])
    elif damage == "duplicate_json":
        data = b'{"type":"request","type":"event"}'
        wire = bytes([1, 1, 0, 0, 0, 0]) + len(data).to_bytes(2, "big") + data
        raw = capture([("R", wire)])
    elif damage == "zip_bomb":
        raw = capture([("R", b"a" * 300000)], compressed=True)
    with pytest.raises(CaptureError) as error:
        decode_capture(raw)
    assert ITEM["key"] not in str(error.value)


@pytest.mark.parametrize("host,port", [("127.0.0.1", 22), ("8.8.8.8", 22), ("169.254.169.254", 22),
    ("example.com; id", 22), ("192.168.1.2", True), (None, 22), ("192.168.1.2", 65536)])
def test_only_explicit_private_lan_endpoints(host, port):
    with pytest.raises(ValueError):
        ssh.endpoint(host, port)


def test_host_key_mismatch_never_sends_password():
    client = object.__new__(ssh.SSH)
    client.fingerprint = "SHA256:actual"
    class Transport:
        def auth_password(self, *args, **kwargs):
            pytest.fail("Credentials must not be sent to an unverified key")
    client.transport = Transport()
    with pytest.raises(ValueError, match="changed"):
        client.authenticate("SHA256:expected", "root", "very-secret")


class FakeSSH:
    fingerprint = "SHA256:verified"
    commands = []
    def __init__(self, *args): pass
    def close(self): pass
    def authenticate(self, expected, username, password):
        assert expected == self.fingerprint
        assert username == "root" and password == FAKE_SSH_PASSWORD
    def run(self, command, **kwargs):
        self.commands.append(command)
        if command == ssh.PREFLIGHT: return b"supported"
        if command.startswith("cat ") and "/status" in command: return b"ready"
        if "then cat " in command and "/capture.bin" in command: return capture()
        return b""


class FastEvent:
    def is_set(self): return False
    def wait(self, timeout): return False


def test_remote_capture_preflights_uploads_restores_verifies_and_removes_plaintext():
    client = FakeSSH()
    client.commands = []
    raw = ssh.capture(client, b"helper", lambda *_: None, FastEvent())
    assert decode_capture(raw)[0] == bundle()
    assert client.commands[0] == ssh.PREFLIGHT
    assert client.commands.count(ssh.PREFLIGHT) == 2
    assert "rm -f" in client.commands[-1] and "capture.bin" in client.commands[-1]
    assert all(FAKE_SSH_PASSWORD not in c for c in client.commands)


def test_unsupported_firmware_never_uploads_or_restarts():
    class Unsupported(FakeSSH):
        def run(self, command, **kwargs):
            assert command == ssh.PREFLIGHT
            raise ValueError()
    with pytest.raises(ValueError, match="Unsupported"):
        ssh.capture(Unsupported(), b"helper", lambda *_: None, FastEvent())


def test_disconnect_after_launch_requires_recovery_and_does_not_race_restore():
    class Disconnected(FakeSSH):
        def run(self, command, **kwargs):
            if command.startswith("cat ") and "/status" in command:
                raise OSError("disconnected")
            return super().run(command, **kwargs)
    client = Disconnected()
    client.commands = []
    with pytest.raises(ssh.RecoveryRequired) as error:
        ssh.capture(client, b"helper", lambda *_: None, FastEvent())
    assert error.value.recovery_command.startswith("sh /tmp/superlink-extract-")
    assert not any(c.startswith("sh ") or "rm -f" in c for c in client.commands)


def ready_jobs(monkeypatch):
    monkeypatch.setattr(jobs, "SSH", FakeSSH)
    monkeypatch.setattr(jobs, "build_helper", lambda: b"helper")
    monkeypatch.setattr(jobs, "capture", lambda *args: capture())
    manager = ExtractionJobs()
    probe = manager.probe({"host": "192.168.1.20"})
    job = manager.start({"probe_id": probe["probe_id"], "username": "root",
                         "password": FAKE_SSH_PASSWORD, "confirmed": True})
    for _ in range(100):
        if manager.status()["state"] == "ready": break
        time.sleep(.01)
    assert manager.status()["state"] == "ready"
    return manager, job


def test_job_status_never_exposes_keys_and_import_consumes_server_document(monkeypatch):
    manager, job = ready_jobs(monkeypatch)
    try:
        assert FAKE_SSH_PASSWORD not in json.dumps(manager.status())
        assert ITEM["key"] not in json.dumps(manager.status())
        with pytest.raises(ValueError):
            manager.use_document("wrong-job", lambda d: d)
        assert manager.use_document(job["id"], lambda d: d, consume=True) == bundle()
        assert manager.document is None
        assert manager.status()["state"] == "imported"
        with pytest.raises(ValueError): manager.use_document(job["id"], lambda d: d)
    finally:
        manager.close()


def test_expired_keys_cannot_be_imported_and_are_released(monkeypatch):
    manager, job = ready_jobs(monkeypatch)
    try:
        manager.expires = 0
        with pytest.raises(ValueError, match="expired"):
            manager.use_document(job["id"], lambda d: d)
        assert manager.document is None
        assert manager.status()["state"] == "expired"
    finally: manager.close()


def test_research_extraction_demo_guard():
    with pytest.raises(ValueError, match="Demo"):
        ExtractionJobs(demo=True).probe({"host": "192.168.1.20"})


def test_remote_scripts_parse():
    for name in ("run.sh", "restore.sh", "watchdog.sh"):
        subprocess.run(["sh", "-n", str(NATIVE / name)], check=True)


@pytest.mark.parametrize("stop_fails", [False, True])
def test_remote_worker_restores_service_even_when_stop_fails(tmp_path, stop_fails):
    """Execute the real shell control flow against fake service/proc tools."""
    import os
    root = tmp_path
    job = root / "job"
    job.mkdir()
    bin_dir = root / "bin"
    bin_dir.mkdir()
    proc = root / "proc" / "99999999"
    proc.mkdir(parents=True)
    (proc / "environ").write_bytes(b"ordinary-service=1\0")
    (root / "pids").write_text("99999999\n")
    def executable(path, content):
        path.write_text("#!/bin/sh\n" + content)
        path.chmod(0o700)
    executable(bin_dir / "pidof", f'cat "{root}/pids"')
    executable(bin_dir / "sleep", "exit 0")
    executable(bin_dir / "timeout", 'shift 3\nexec "$@"')
    executable(bin_dir / "chrt", 'shift 2\nexec "$@"')
    executable(root / "service", f'''echo "$1" >> "{root}/service-log"
case "$1" in
stop) {'exit 1' if stop_fails else f': > "{root}/pids"'};;
start) echo 99999999 > "{root}/pids";;
esac
''')
    executable(root / "lorabrd", 'printf synthetic > "$SUPERLINK_CAPTURE_PATH"')
    for name in ("run.sh", "restore.sh"):
        source = (NATIVE / name).read_text().replace("/proc/", str(root / "proc") + "/")
        source = source.replace("/etc/init.d/lorabr", str(root / "service"))
        source = source.replace("/usr/sbin/lorabrd", str(root / "lorabrd"))
        (job / name).write_text(source)
    subprocess.run(["sh", str(job / "run.sh"), str(job)],
                   env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]},
                   capture_output=True, timeout=5)
    assert (root / "service-log").read_text().splitlines() == ["stop", "start"]
    assert (job / "restored").is_file()
    assert (job / "status").read_text().strip() == ("failed" if stop_fails else "ready")


def test_removed_device_is_excluded_across_interleaved_connection_lifetimes():
    # Processing one entire connection at a time must never resurrect a key
    # removed on another connection whose lifetime began earlier.
    removed = capture([("R", add()), ("W", response()),
        ("R", pair({"type": "request", "action": "removeDevice"}, {"mac": ITEM["mac"]}))])
    with pytest.raises(CaptureError, match="No acknowledged"):
        decode_capture(removed + capture())


def test_active_job_cannot_be_started_twice_and_failed_build_never_connects(monkeypatch):
    gate = threading.Event()
    started = threading.Event()
    def build():
        started.set()
        gate.wait(2)
        raise ValueError("Build unavailable")
    monkeypatch.setattr(jobs, "build_helper", build)
    monkeypatch.setattr(jobs, "SSH", FakeSSH)
    manager = ExtractionJobs()
    probe = manager.probe({"host": "192.168.1.20"})
    body = {"probe_id": probe["probe_id"], "username": "root", "password": FAKE_SSH_PASSWORD, "confirmed": True}
    monkeypatch.setattr(jobs, "SSH", lambda *_: pytest.fail("Build failure must precede SSH authentication"))
    manager.start(body)
    assert started.wait(1)
    with pytest.raises(ValueError, match="active"):
        manager.start(body)
    gate.set()
    for _ in range(100):
        if manager.status()["state"] == "failed": break
        time.sleep(.01)
    assert manager.status()["state"] == "failed"
    manager.close()


def test_recovery_keeps_import_blocked_until_source_is_verified(monkeypatch):
    monkeypatch.setattr(jobs, "SSH", FakeSSH)
    monkeypatch.setattr(jobs, "build_helper", lambda: b"helper")
    def interrupted(*args): raise ssh.RecoveryRequired("/tmp/superlink-extract-test")
    monkeypatch.setattr(jobs, "capture", interrupted)
    manager = ExtractionJobs()
    probe = manager.probe({"host": "192.168.1.20"})
    credentials = {"username": "root", "password": FAKE_SSH_PASSWORD}
    job = manager.start({**credentials, "probe_id": probe["probe_id"], "confirmed": True})
    for _ in range(100):
        if manager.status()["state"] == "recovery_required": break
        time.sleep(.01)
    assert manager.status()["state"] == "recovery_required"
    with pytest.raises(ValueError): manager.use_document(job["id"], lambda doc: doc)
    manager.recover({**credentials, "job_id": job["id"]})
    for _ in range(100):
        if manager.status()["state"] == "failed": break
        time.sleep(.01)
    assert manager.status()["state"] == "failed"
    assert "verified" in manager.status()["message"]
    assert manager.document is None
    manager.close()


@pytest.mark.parametrize("restored", [True, False])
def test_watchdog_erases_plaintext_and_only_releases_lock_after_restore(tmp_path, restored):
    import os
    job, lock, bin_dir = (tmp_path / name for name in ("job", "lock", "bin"))
    for directory in (job, lock, bin_dir): directory.mkdir()
    (lock / "owner").write_text(str(job))
    (job / "capture.bin").write_bytes(b"synthetic-secret")
    (job / "restore.sh").write_text("#!/bin/sh\nexit " + ("0" if restored else "1") + "\n")
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "sleep").chmod(0o700)
    source = (NATIVE / "watchdog.sh").read_text().replace("/tmp/superlink-extraction.lock", str(lock))
    script = job / "watchdog.sh"
    script.write_text(source)
    subprocess.run(["sh", str(script), str(job)], env={**os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}, check=True, timeout=5)
    assert not (job / "capture.bin").exists()
    if restored:
        assert not job.exists() and not lock.exists()
    else:
        assert lock.exists() and (job / "status").read_text().strip() == "recovery_required"


def test_command_timeout_closes_channel_even_without_exec_acknowledgement():
    class StalledChannel:
        def __init__(self): self.closed = threading.Event()
        def settimeout(self, value): pass
        def exec_command(self, command):
            assert self.closed.wait(1), "exec acknowledgement wait was not bounded"
            raise OSError("channel closed")
        def close(self): self.closed.set()
    channel = StalledChannel()
    class Transport:
        def open_session(self, timeout): return channel
    client = object.__new__(ssh.SSH)
    client.transport = Transport()
    started = time.monotonic()
    with pytest.raises(OSError): client.run("true", timeout=.02)
    assert time.monotonic() - started < .5


def test_no_tls_capture_is_reported_only_after_verified_restore_and_cleanup():
    class NoTraffic(FakeSSH):
        def run(self, command, **kwargs):
            if "then cat " in command and "/capture.bin" in command:
                self.commands.append(command)
                return b""
            return super().run(command, **kwargs)
    client = NoTraffic()
    client.commands = []
    with pytest.raises(ValueError, match="no TLS traffic") as error:
        ssh.capture(client, b"helper", lambda *_: None, FastEvent())
    assert not isinstance(error.value, ssh.RecoveryRequired)
    assert "rm -f" in client.commands[-1]
    assert client.commands.count(ssh.PREFLIGHT) == 2
