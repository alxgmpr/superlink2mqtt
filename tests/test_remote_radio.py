"""Exercise the network protocol with a real stream and a fake radio."""
import io
import json
import socket
import threading

import paramiko
import pytest

from superlink.hal import RxPacket, BW_500KHZ
from superlink.radio_remote import RemoteRadioConfig, RemoteSX1302
from superlink.radio_server import serve, dispatch


class Radio:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.sent = []
        self.packets = []

    def start(self): self.started = True
    def stop(self): self.stopped = True
    def version(self): return "test-hal"
    def receive(self):
        packets, self.packets = self.packets, []
        return packets
    def send(self, *args, **kwargs): self.sent.append((args, kwargs))


def config(tmp_path):
    return RemoteRadioConfig("radio.local", "radio", str(tmp_path / "key"),
                             str(tmp_path / "known_hosts"))


def test_stream_roundtrip_preserves_hardware_timing_and_payload(tmp_path):
    local, peer = socket.socketpair()
    local.settimeout(2)
    peer.settimeout(2)
    radio = Radio()
    packet = RxPacket(915600000, 3, 1, -51.5, 7.25, True,
                      bytes(range(256)), 0xFFFFFFF0, 5, 4, 1)
    radio.packets = [packet]
    reader, writer = peer.makefile("rb"), peer.makefile("wb")
    thread = threading.Thread(target=serve, args=(reader, writer, radio))
    thread.start()
    client = RemoteSX1302(config(tmp_path))
    client.channel, client.reader = local, local.makefile("rb")
    try:
        assert client._rpc({"op": "start", "protocol": 1})["version"] == "test-hal"
        assert client.receive() == [packet]
        assert client.receive() == []
        client.send(920400000, b"\x00\xff", tx_timestamp_us=packet.timestamp_us + 1000000)
        assert radio.sent == [((920400000, b"\x00\xff"), {
            "rf_power": 10, "bandwidth": BW_500KHZ,
            "tx_timestamp_us": 999984, "invert_pol": False})]
    finally:
        client.stop()
        thread.join(3)
        reader.close()
        writer.close()
        peer.close()
    assert not thread.is_alive()
    assert radio.started and radio.stopped


@pytest.mark.parametrize("field,value", [
    ("freq_hz", 100000000), ("freq_hz", True), ("rf_power", 30),
    ("payload", "00" * 257), ("payload", "no hex"), ("payload", ""),
    ("tx_timestamp_us", -1), ("bandwidth", 4), ("invert_pol", "false"),
    ("op", "exec"),
])
def test_invalid_transmission_never_reaches_hardware(field, value):
    request = dict(op="send", freq_hz=920400000, rf_power=10, payload="0102",
                   bandwidth=BW_500KHZ, tx_timestamp_us=42, invert_pol=False)
    request[field] = value
    radio = Radio()
    with pytest.raises(ValueError):
        dispatch(radio, request)
    assert not radio.sent


@pytest.mark.parametrize("data", [b"x" * 65537, b'{"op":"start"}', b"[]\n"])
def test_malformed_stream_closes_radio(data):
    radio = Radio()
    with pytest.raises(ValueError):
        serve(io.BytesIO(data), io.BytesIO(), radio)
    assert radio.stopped


def test_protocol_mismatch_does_not_start_radio():
    radio = Radio()
    output = io.BytesIO()
    serve(io.BytesIO(b'{"op":"start","protocol":2}\n'), output, radio)
    assert json.loads(output.getvalue())["ok"] is False
    assert not radio.started


def test_disconnect_during_send_is_fatal_and_never_retries(tmp_path):
    class Channel:
        sends = 0
        closed = False
        def sendall(self, data): self.sends += 1
        def close(self): self.closed = True
    channel = Channel()
    client = RemoteSX1302(config(tmp_path))
    client.channel, client.reader = channel, io.BytesIO(b"")
    with pytest.raises(ConnectionError):
        client.send(920400000, b"\x01")
    assert channel.sends == 1 and channel.closed


def test_ssh_uses_pinned_host_and_dedicated_key(tmp_path, monkeypatch):
    calls = {}
    class SSH:
        def load_host_keys(self, path): calls["hosts"] = path
        def set_missing_host_key_policy(self, policy): calls["policy"] = policy
        def connect(self, host, **kwargs):
            calls.update(host=host, **kwargs)
            raise paramiko.SSHException("host mismatch")
        def close(self): calls["closed"] = True
    monkeypatch.setattr(paramiko, "SSHClient", SSH)
    client = RemoteSX1302(config(tmp_path))
    with pytest.raises(paramiko.SSHException):
        client.start()
    assert isinstance(calls["policy"], paramiko.RejectPolicy)
    assert calls["hosts"] == str(tmp_path / "known_hosts")
    assert calls["key_filename"] == str(tmp_path / "key")
    assert calls["allow_agent"] is calls["look_for_keys"] is False
    assert calls["closed"]
