"""Exercise the GWMP backend against a fake packet forwarder on loopback UDP."""
import base64
import json
import socket
import threading

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


@pytest.fixture
def link():
    radio = GwmpSX1302(GwmpRadioConfig(bind="127.0.0.1", port=0, tx_ack_timeout=1))
    radio.start()
    forwarder = Forwarder(radio.sock.getsockname()[1])
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
    forwarder.sock.sendto(b"\x02", forwarder.server)                      # runt
    forwarder.sock.sendto(b"\x01\x00\x00\x00" + EUI + b"{}", forwarder.server)  # v1
    forwarder.push(b"\x00\x01", {"stat": {"rxnb": 0}})                    # stat only
    assert poll(radio, forwarder) == ([], b"\x02\x00\x01\x01")
    forwarder.sock.sendto(b"\x02\x00\x02\x00" + EUI + b"not json", forwarder.server)
    assert poll(radio, forwarder) == ([], b"\x02\x00\x02\x01")
    forwarder.push(b"\x00\x03", {"rxpk": [
        rxpk(data="!!!"), rxpk(datr=50000), rxpk(datr="SF5BW999"), {"tmst": 1},
        "junk", rxpk(data="AQ=="),
    ]})
    packets, _ = poll(radio, forwarder)
    assert [p.payload for p in packets] == [b"\x01"]


def test_send_before_any_pull_data_is_refused(link):
    radio, forwarder = link
    forwarder.push(b"\x00\x01", {"rxpk": [rxpk()]})   # uplinks alone give no TX route
    poll(radio, forwarder)
    with pytest.raises(RuntimeError, match="PULL_DATA"):
        radio.send(920400000, b"\x01", tx_timestamp_us=1000000)
    forwarder.sock.settimeout(0.05)
    with pytest.raises(TimeoutError):
        forwarder.recv()
    assert "no forwarder" in radio.version()


def test_downlink_is_a_timestamped_pull_resp_to_the_latest_pull_data(link):
    radio, forwarder = link
    stale = Forwarder(forwarder.server[1])
    stale.pull()
    assert poll(radio, stale) == ([], b"\x02\x00\x07\x04")
    forwarder.pull(b"\x12\x34")
    assert poll(radio, forwarder) == ([], b"\x02\x12\x34\x04")
    thread, seen = forwarder.answer_tx(b'{"txpk_ack":{"error":"NONE"}}')
    radio.send(920400000, b"\x00\xff", tx_timestamp_us=0xFFFFFFF0 + 1000000, invert_pol=True)
    thread.join(2)
    stale.sock.close()
    data = seen[0]
    assert data[0] == 2 and data[3] == 3
    assert json.loads(data[4:]) == {"txpk": {
        "tmst": 999984, "freq": 920.4, "rfch": 0, "powe": 10, "modu": "LORA",
        "datr": "SF5BW500", "codr": "4/5", "ipol": True, "prea": 12, "ncrc": False,
        "size": 2, "data": "AP8="}}
    assert str(forwarder.sock.getsockname()[1]) in radio.version()


def test_untimed_send_is_immediate_and_empty_tx_ack_is_success(link):
    radio, forwarder = link
    forwarder.pull()
    poll(radio, forwarder)
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
    forwarder.pull()
    poll(radio, forwarder)
    thread, _ = forwarder.answer_tx(body)
    with pytest.raises(RuntimeError, match=match):
        radio.send(920400000, b"\x01", tx_timestamp_us=42)
    thread.join(2)


def test_tx_ack_warning_is_logged_not_raised(link, caplog):
    radio, forwarder = link
    forwarder.pull()
    poll(radio, forwarder)
    thread, _ = forwarder.answer_tx(b'{"txpk_ack":{"warn":"TX_POWER","value":12}}')
    radio.send(920400000, b"\x01", tx_timestamp_us=42)
    thread.join(2)
    assert "TX_POWER" in caplog.text


def test_missing_or_mismatched_tx_ack_is_unconfirmed_and_uplinks_survive_the_wait(link):
    radio, forwarder = link
    radio.config.tx_ack_timeout = 0.2
    forwarder.pull()
    poll(radio, forwarder)
    forwarder.tx_ack(b"\xee\xee")                     # stale ack, wrong token
    forwarder.push(b"\x00\x09", {"rxpk": [rxpk(data="AQ==")]})
    with pytest.raises(RuntimeError, match="unconfirmed"):
        radio.send(920400000, b"\x01", tx_timestamp_us=42)
    assert [p.payload for p in radio.receive()] == [b"\x01"]


def test_tx_ack_from_another_host_is_ignored(link):
    radio, forwarder = link
    radio.config.tx_ack_timeout = 0.2
    forwarder.pull()
    poll(radio, forwarder)
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
    forwarder.pull()
    poll(radio, forwarder)
    for payload in (b"", bytes(257)):
        with pytest.raises(ValueError):
            radio.send(920400000, payload)


def test_lifecycle_errors_are_connection_errors():
    radio = GwmpSX1302(GwmpRadioConfig(bind="127.0.0.1", port=0))
    with pytest.raises(ConnectionError):
        radio.receive()
    radio.start()
    try:
        clash = GwmpSX1302(GwmpRadioConfig(bind="127.0.0.1",
                                           port=radio.sock.getsockname()[1]))
        with pytest.raises(ConnectionError, match="bind"):
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
    cfg = load("radio:\n  gwmp:\n    bind: 192.0.2.5\n    port: 1701\n").radio
    assert (cfg.bind, cfg.port, cfg.backend) == ("192.0.2.5", 1701, "sx1302-gwmp")
    assert load("radio:\n  gwmp: {}\n").radio == GwmpRadioConfig("0.0.0.0", 1700)
    ssh = load("radio:\n  host: h\n  username: u\n  key_file: k\n  known_hosts: n\n").radio
    assert ssh.host == "h" and not hasattr(ssh, "backend")
    for bad in ("radio:\n  gwmp: {port: 70000}\n", "radio:\n  gwmp: {bind: ''}\n",
                "radio:\n  gwmp: {tx_ack_timeout: 0}\n", "radio:\n  gwmp: {secret: x}\n",
                "radio:\n  gwmp: {}\n  host: h\n"):
        with pytest.raises((ValueError, TypeError)):
            load(bad)
