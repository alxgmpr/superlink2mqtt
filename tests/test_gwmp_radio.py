"""Exercise the GWMP backend against a fake packet forwarder on loopback UDP."""
import base64
import json
import socket
import threading
import time

import pytest

from superlink.bridge.config import RuntimeConfig
from superlink.hal import RxPacket, BW_125KHZ, BW_500KHZ
from superlink.radio_gwmp import GwmpRadioConfig, GwmpSX1302

EUI = bytes.fromhex("0016c001f1500000")


class Forwarder:
    """The gateway side: sends PUSH_DATA/PULL_DATA/TX_ACK, reads acks and PULL_RESP."""
    def __init__(self, port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(2)
        self.server = ("127.0.0.1", port)

    def push(self, token, doc):
        self.sock.sendto(b"\x02" + token + b"\x00" + EUI + json.dumps(doc).encode(), self.server)

    def pull(self, token=b"\x00\x07"):
        self.sock.sendto(b"\x02" + token + b"\x02" + EUI, self.server)

    def tx_ack(self, token, body=b""):
        self.sock.sendto(b"\x02" + token + b"\x05" + EUI + body, self.server)

    def recv(self):
        return self.sock.recv(65535)

    def answer_tx(self, body=b""):
        """Background: TX_ACK the next PULL_RESP; returns (thread, captured txpk list)."""
        seen = []
        def run():
            data = self.recv()
            seen.append(data)
            self.tx_ack(data[1:3], body)
        thread = threading.Thread(target=run)
        thread.start()
        return thread, seen


def connect(radio, token=b"\x00\x07"):
    """Run start() while a fake forwarder supplies the PULL_DATA it waits for."""
    errors = []
    def run():
        try:
            radio.start()
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    while radio.sock is None and thread.is_alive():
        time.sleep(0.001)
    forwarder = Forwarder(radio.sock.getsockname()[1])
    forwarder.pull(token)
    assert forwarder.recv() == b"\x02" + token + b"\x04"
    thread.join(2)
    assert not thread.is_alive() and not errors
    return forwarder


@pytest.fixture
def link():
    radio = GwmpSX1302(GwmpRadioConfig(bind="127.0.0.1", port=0, tx_ack_timeout=1,
                                       forwarder_timeout=5))
    forwarder = connect(radio)
    yield radio, forwarder
    radio.stop()
    forwarder.sock.close()


def rxpk(**over):
    return {"tmst": 0xFFFFFFF0, "freq": 915.6, "chan": 0, "rfch": 0, "stat": 1,
            "modu": "LORA", "datr": "SF5BW125", "codr": "4/5", "rssi": -51,
            "lsnr": 7.2, "size": 256,
            "data": base64.b64encode(bytes(range(256))).decode(), **over}


def poll(radio, forwarder):
    """Wait for the forwarder's datagram to be acked, i.e. processed by the backend."""
    ack = None
    for _ in range(200):
        packets = radio.receive()
        try:
            forwarder.sock.settimeout(0.01)
            ack = forwarder.recv()
            return packets, ack
        except TimeoutError:
            if packets:
                raise AssertionError("packets returned before the ack was sent")
        finally:
            forwarder.sock.settimeout(2)
    raise AssertionError("no ack from backend")


def test_rxpk_becomes_rxpacket_and_push_data_is_acked(link):
    radio, forwarder = link
    forwarder.push(b"\xab\xcd", {"rxpk": [
        rxpk(),
        rxpk(freq=917.0, chan=7, stat=-1, tmst=5, data="AP8=", size=2),
        rxpk(freq=921.0, datr="SF5BW500", data="AQ=="),
    ], "stat": {"rxnb": 3}})
    packets, ack = poll(radio, forwarder)
    assert ack == b"\x02\xab\xcd\x01"
    assert packets == [
        RxPacket(915600000, 0, 1, -51.0, 7.2, True, bytes(range(256)),
                 0xFFFFFFF0, 5, BW_125KHZ, 1),
        RxPacket(917000000, 7, 8, -51.0, 7.2, False, b"\x00\xff", 5, 5, BW_125KHZ, 1),
        RxPacket(921000000, 0, 0, -51.0, 7.2, True, b"\x01", 0xFFFFFFF0, 5, BW_500KHZ, 1),
    ]
    assert radio.receive() == []


def test_malformed_uplinks_are_acked_and_dropped(link):
    radio, forwarder = link
    forwarder.sock.sendto(b"\x02\x00\x00\x00", forwarder.server)          # runt
    forwarder.sock.sendto(b"\x01\x00\x00\x00" + EUI + b"{}", forwarder.server)  # v1
    forwarder.push(b"\x00\x01", {"stat": {"rxnb": 0}})                    # stat only
    assert poll(radio, forwarder) == ([], b"\x02\x00\x01\x01")
    forwarder.sock.sendto(b"\x02\x00\x02\x00" + EUI + b"not json", forwarder.server)
    assert poll(radio, forwarder) == ([], b"\x02\x00\x02\x01")
    forwarder.sock.sendto(b"\x02\x00\x04\x00" + EUI + b"[" * 8000, forwarder.server)
    assert poll(radio, forwarder) == ([], b"\x02\x00\x04\x01")
    body = json.dumps({"rxpk": [
        rxpk(data="!!!"), rxpk(datr=50000), rxpk(datr="SF5BW999"), {"tmst": 1},
        "junk", rxpk(freq=1e999), rxpk(tmst=1e999), rxpk(data="AQ=="),
    ]})
    assert "Infinity" in body
    forwarder.sock.sendto(b"\x02\x00\x03\x00" + EUI + body.encode(), forwarder.server)
    packets, _ = poll(radio, forwarder)
    assert [p.payload for p in packets] == [b"\x01"]


def test_before_any_pull_data_nothing_is_received_or_sent():
    radio = GwmpSX1302(GwmpRadioConfig(bind="127.0.0.1", port=0))
    radio._bind()
    forwarder = Forwarder(radio.sock.getsockname()[1])
    try:
        forwarder.push(b"\x00\x01", {"rxpk": [rxpk()]})   # uplinks alone give no route
        forwarder.tx_ack(b"\x00\x01")
        time.sleep(0.05)
        radio._pump()
        assert radio._pending == [] and radio.forwarder is None
        with pytest.raises(RuntimeError, match="PULL_DATA"):
            radio.send(920400000, b"\x01", tx_timestamp_us=1000000)
        forwarder.sock.settimeout(0.05)
        with pytest.raises(TimeoutError):
            forwarder.recv()
        assert "no forwarder" in radio.version()
    finally:
        radio.stop()
        forwarder.sock.close()


def test_start_fails_until_a_forwarder_sends_pull_data():
    radio = GwmpSX1302(GwmpRadioConfig(bind="127.0.0.1", port=0, forwarder_timeout=0.1))
    with pytest.raises(ConnectionError, match="PULL_DATA"):
        radio.start()
    assert radio.sock is None


def test_silent_forwarder_is_a_connection_error():
    radio = GwmpSX1302(GwmpRadioConfig(bind="127.0.0.1", port=0, forwarder_timeout=1.0))
    forwarder = connect(radio)
    try:
        assert radio.receive() == []
        time.sleep(0.6)
        forwarder.pull()                       # a keepalive extends the deadline
        time.sleep(0.6)
        assert radio.receive() == []
        time.sleep(1.1)
        with pytest.raises(ConnectionError, match="stopped"):
            radio.receive()
    finally:
        radio.stop()
        forwarder.sock.close()


def test_restarted_forwarder_takes_the_route_and_forces_a_reconnect(link):
    radio, forwarder = link
    restarted = Forwarder(forwarder.server[1])        # same host, new source port
    restarted.pull(b"\x00\x09")
    with pytest.raises(ConnectionError, match="restarted"):
        for _ in range(200):
            radio.receive()
            time.sleep(0.005)
    assert restarted.recv() == b"\x02\x00\x09\x04"
    assert radio.forwarder == restarted.sock.getsockname()
    restarted.sock.close()


def test_other_hosts_cannot_inject_or_take_the_route(link):
    radio, forwarder = link
    radio.forwarder = ("192.0.2.9", 1)     # the real forwarder lives elsewhere
    forwarder.pull(b"\x00\x09")
    forwarder.push(b"\x00\x0a", {"rxpk": [rxpk()]})
    time.sleep(0.05)
    assert radio.receive() == [] and radio.forwarder == ("192.0.2.9", 1)
    forwarder.sock.settimeout(0.05)
    with pytest.raises(TimeoutError):
        forwarder.recv()


def test_pinned_forwarder_address_drops_everyone_else():
    radio = GwmpSX1302(GwmpRadioConfig(bind="127.0.0.1", port=0, forwarder="192.0.2.9",
                                       forwarder_timeout=0.3))
    radio._bind()
    stranger = Forwarder(radio.sock.getsockname()[1])
    try:
        stranger.pull()
        time.sleep(0.05)
        radio._pump()
        assert radio.forwarder is None
    finally:
        radio.stop()
        stranger.sock.close()
    radio.config.forwarder = "localhost"
    forwarder = connect(radio)
    assert radio.forwarder == forwarder.sock.getsockname()
    radio.stop()
    forwarder.sock.close()
    radio.config.forwarder = "no-such-host.invalid"
    with pytest.raises(ConnectionError):
        radio.start()


def test_downlink_is_a_timestamped_pull_resp_to_the_forwarder(link):
    radio, forwarder = link
    thread, seen = forwarder.answer_tx(b'{"txpk_ack":{"error":"NONE"}}')
    radio.send(920400000, b"\x00\xff", tx_timestamp_us=0xFFFFFFF0 + 1000000, invert_pol=True)
    thread.join(2)
    data = seen[0]
    assert data[0] == 2 and data[3] == 3
    assert json.loads(data[4:]) == {"txpk": {
        "tmst": 999984, "freq": 920.4, "rfch": 0, "powe": 10, "modu": "LORA",
        "datr": "SF5BW500", "codr": "4/5", "ipol": True, "prea": 12, "ncrc": False,
        "size": 2, "data": "AP8="}}
    assert str(forwarder.sock.getsockname()[1]) in radio.version()


def test_untimed_send_is_immediate_and_empty_tx_ack_is_success(link):
    radio, forwarder = link
    thread, seen = forwarder.answer_tx()
    radio.send(927600000, b"\x01", rf_power=14, bandwidth=BW_125KHZ)
    thread.join(2)
    txpk = json.loads(seen[0][4:])["txpk"]
    assert txpk["imme"] is True and "tmst" not in txpk
    assert (txpk["datr"], txpk["powe"], txpk["freq"]) == ("SF5BW125", 14, 927.6)


@pytest.mark.parametrize("body,match", [
    (b'{"txpk_ack":{"error":"TOO_LATE"}}', "TOO_LATE"),
    (b'{"txpk_ack":{"error":"TX_FREQ"}}', "TX_FREQ"),
    (b'{"nonsense":1}', "Malformed"),
    (b'garbage', "Malformed"),
])
def test_tx_ack_errors_are_raised(link, body, match):
    radio, forwarder = link
    thread, _ = forwarder.answer_tx(body)
    with pytest.raises(RuntimeError, match=match):
        radio.send(920400000, b"\x01", tx_timestamp_us=42)
    thread.join(2)


def test_tx_ack_warning_is_logged_not_raised(link, caplog):
    radio, forwarder = link
    thread, _ = forwarder.answer_tx(b'{"txpk_ack":{"warn":"TX_POWER","value":12}}')
    radio.send(920400000, b"\x01", tx_timestamp_us=42)
    thread.join(2)
    assert "TX_POWER" in caplog.text


def test_missing_or_mismatched_tx_ack_is_unconfirmed_and_uplinks_survive_the_wait(link):
    radio, forwarder = link
    radio.config.tx_ack_timeout = 0.2
    forwarder.tx_ack(b"\xee\xee")                     # stale ack, wrong token
    forwarder.push(b"\x00\x09", {"rxpk": [rxpk(data="AQ==")]})
    with pytest.raises(RuntimeError, match="unconfirmed"):
        radio.send(920400000, b"\x01", tx_timestamp_us=42)
    assert [p.payload for p in radio.receive()] == [b"\x01"]


def test_tx_ack_from_another_port_is_ignored(link):
    radio, forwarder = link
    radio.config.tx_ack_timeout = 0.2
    other = Forwarder(forwarder.server[1])
    def spoof():
        data = forwarder.recv()
        other.tx_ack(data[1:3])
    thread = threading.Thread(target=spoof)
    thread.start()
    with pytest.raises(RuntimeError, match="unconfirmed"):
        radio.send(920400000, b"\x01", tx_timestamp_us=42)
    thread.join(2)
    other.sock.close()


def test_invalid_payload_size_never_reaches_the_forwarder(link):
    radio, forwarder = link
    for payload in (b"", bytes(257)):
        with pytest.raises(ValueError):
            radio.send(920400000, payload)
    with pytest.raises(ValueError):
        radio.send(920400000, b"\x01", bandwidth=99)


def test_lifecycle_errors_are_connection_errors():
    radio = GwmpSX1302(GwmpRadioConfig(bind="127.0.0.1", port=0))
    with pytest.raises(ConnectionError):
        radio.receive()
    radio._bind()
    try:
        clash = GwmpSX1302(GwmpRadioConfig(bind="127.0.0.1",
                                           port=radio.sock.getsockname()[1]))
        with pytest.raises(ConnectionError, match="Cannot open"):
            clash.start()
    finally:
        radio.stop()
    radio.stop()


def test_gwmp_is_opt_in_configuration(tmp_path):
    def load(radio):
        path = tmp_path / "c.yaml"
        path.write_text('gw_mac: "010203040506"\n' + radio)
        return RuntimeConfig.load(str(path))
    assert load("").radio is None
    cfg = load("radio:\n  gwmp:\n    bind: 192.0.2.5\n    port: 1701\n"
               "    forwarder: sx1302.local\n").radio
    assert (cfg.bind, cfg.port, cfg.backend) == ("192.0.2.5", 1701, "sx1302-gwmp")
    assert (cfg.forwarder, cfg.forwarder_timeout) == ("sx1302.local", 30.0)
    assert load("radio:\n  gwmp: {}\n").radio == GwmpRadioConfig("0.0.0.0", 1700)
    ssh = load("radio:\n  host: h\n  username: u\n  key_file: k\n  known_hosts: n\n").radio
    assert ssh.host == "h" and not hasattr(ssh, "backend")
    for bad in ("radio:\n  gwmp: {port: 70000}\n", "radio:\n  gwmp: {bind: ''}\n",
                "radio:\n  gwmp: {tx_ack_timeout: 0}\n",
                "radio:\n  gwmp: {forwarder_timeout: 0}\n", "radio:\n  gwmp: {forwarder: 5}\n", "radio:\n  gwmp: {secret: x}\n",
                "radio:\n  gwmp: {}\n  host: h\n"):
        with pytest.raises((ValueError, TypeError)):
            load(bad)
