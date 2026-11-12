"""SX1302 HAL over the Semtech UDP packet-forwarder protocol (GWMP v2).

The concentrator host runs a stock ``lora_pkt_fwd`` pointed at this socket;
no project code runs there. GWMP has no authentication or encryption: any
host that can spoof the forwarder's UDP source address can inject uplinks or
take over the downlink route. See docs/GWMP_BACKEND.md. Not verified on hardware.
"""
import base64
from dataclasses import dataclass
import json
import logging
import re
import select
import socket
import time

from .hal import (RxPacket, BW_125KHZ, BW_500KHZ, CR_LORA_4_5, DR_LORA_SF5,
                  UL_FREQ_HZ)

log = logging.getLogger(__name__)

VERSION = 2
PUSH_DATA, PUSH_ACK, PULL_DATA, PULL_RESP, PULL_ACK, TX_ACK = range(6)
HEADER = 12  # version, token(2), type, gateway EUI(8)

BW_250KHZ = 0x05  # loragw_hal.h; unused by SuperLink, kept so RX reports it faithfully
_BW_CODE = {125: BW_125KHZ, 250: BW_250KHZ, 500: BW_500KHZ}
_BW_KHZ = {code: khz for khz, code in _BW_CODE.items()}
_CODERATE = {"4/5": CR_LORA_4_5, "4/6": 2, "4/7": 3, "4/8": 4}
_DATR = re.compile(r"SF(\d{1,2})BW(\d{3})")
_UL_CHANNEL = {freq: i + 1 for i, freq in enumerate(sorted(UL_FREQ_HZ))}


@dataclass
class GwmpRadioConfig:
    bind: str = "0.0.0.0"
    port: int = 1700
    # Host or address of the packet forwarder. When set, datagrams from any
    # other source address are dropped. Not authentication: UDP sources spoof.
    forwarder: str | None = None
    # Silence (no PULL_DATA) after which the forwarder is declared gone. Keep
    # it a few multiples of lora_pkt_fwd's keepalive_interval.
    forwarder_timeout: float = 30.0
    # How long send() waits for the forwarder's TX_ACK. It comes straight out
    # of the ~1 s downlink budget when a burst is queued, so keep it short.
    tx_ack_timeout: float = 0.25

    backend = "sx1302-gwmp"

    def __post_init__(self):
        if not isinstance(self.bind, str) or not self.bind:
            raise ValueError("radio.gwmp.bind is required")
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError("radio.gwmp.port must be between 0 and 65535")
        if self.forwarder is not None and (not isinstance(self.forwarder, str)
                                           or not self.forwarder):
            raise ValueError("radio.gwmp.forwarder must be a host name or address")
        for name, limit in (("tx_ack_timeout", 5), ("forwarder_timeout", 3600)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not 0 < value <= limit:
                raise ValueError(f"radio.gwmp.{name} must be in (0, {limit}] seconds")


def parse_rxpk(rx) -> RxPacket:
    """One GWMP ``rxpk`` object -> RxPacket. Raises on anything malformed."""
    sf, bw = _DATR.fullmatch(rx["datr"]).groups()  # FSK datr is an int: TypeError
    freq_hz = round(rx["freq"] * 1e6)
    return RxPacket(
        freq_hz=freq_hz,
        if_chain=int(rx.get("chan", 0)),
        # By frequency, not IF index: the IF layout is the forwarder's business.
        ul_channel=_UL_CHANNEL.get(freq_hz, 0),
        rssi=float(rx.get("rssi", 0)),
        snr=float(rx.get("lsnr", 0)),
        crc_ok=rx.get("stat") == 1,
        payload=base64.b64decode(rx["data"], validate=True),
        timestamp_us=int(rx["tmst"]) & 0xFFFFFFFF,
        datarate=int(sf),
        bandwidth=_BW_CODE[int(bw)],
        coderate=_CODERATE.get(rx.get("codr"), 0),
    )


class GwmpSX1302:
    def __init__(self, config):
        self.config = config
        self.sock = None
        self.forwarder = None  # downlink route: source of the accepted PULL_DATA
        self._peer_ip = None   # config.forwarder, resolved
        self._last_pull = 0.0
        self._restarted = False
        self._pending = []
        self._token = 0
        self._acks = {}

    def _bind(self):
        self.stop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if self.config.forwarder:
                self._peer_ip = socket.gethostbyname(self.config.forwarder)
            sock.bind((self.config.bind, self.config.port))
        except OSError as exc:
            sock.close()
            raise ConnectionError(f"Cannot open GWMP socket: {exc}") from exc
        sock.setblocking(False)
        self.sock = sock

    def start(self):
        """Bind, then wait for a forwarder: "connected" means one is talking to us."""
        self._bind()
        deadline = time.monotonic() + self.config.forwarder_timeout
        while self.forwarder is None:
            remaining = deadline - time.monotonic()
            sock = self.sock  # stop() from another thread ends the wait
            if remaining <= 0 or sock is None:
                self.stop()
                raise ConnectionError("No PULL_DATA from a packet forwarder")
            try:
                select.select([sock], [], [], min(remaining, 0.5))
                self._pump()
            except (OSError, ValueError) as exc:  # closed under us
                self.stop()
                raise ConnectionError("GWMP socket closed while waiting") from exc

    def stop(self):
        if self.sock is not None:
            self.sock.close()
        self.sock = self.forwarder = self._peer_ip = None
        self._restarted = False
        self._pending = []
        self._acks = {}

    def version(self):
        peer = "%s:%d" % self.forwarder[:2] if self.forwarder else "no forwarder yet"
        return f"GWMP v{VERSION} / {peer}"

    def _pump(self):
        """Handle every queued datagram without blocking."""
        if self.sock is None:
            raise ConnectionError("GWMP socket is not open")
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except BlockingIOError:
                return
            except OSError as exc:
                self.stop()
                raise ConnectionError(f"GWMP socket failed: {exc}") from exc
            if len(data) < HEADER or data[0] != VERSION:
                continue
            if self._peer_ip and addr[0] != self._peer_ip:
                continue
            token, kind = data[1:3], data[3]
            try:
                if kind == PULL_DATA and self._route(addr):
                    self.sock.sendto(bytes([VERSION]) + token + bytes([PULL_ACK]), addr)
                elif self.forwarder is None or addr[0] != self.forwarder[0]:
                    continue  # uplinks and acks only from the forwarder's host
                elif kind == PUSH_DATA:
                    self.sock.sendto(bytes([VERSION]) + token + bytes([PUSH_ACK]), addr)
                    self._uplinks(data[HEADER:])
                elif kind == TX_ACK and addr == self.forwarder:
                    self._acks[token] = data[HEADER:]
            except OSError as exc:  # e.g. ICMP unreachable surfaced on sendto
                log.debug("GWMP reply to %s failed: %s", addr, exc)

    def _route(self, addr):
        """Accept a PULL_DATA source as the downlink route, or refuse it."""
        if self.forwarder is not None and addr != self.forwarder:
            if addr[0] != self.forwarder[0]:
                # A live forwarder keeps the route; another host cannot take it.
                log.debug("Ignoring PULL_DATA from second host %s", addr[0])
                return False
            # lora_pkt_fwd keeps one downlink socket for its lifetime, so a new
            # source port means it restarted and its tmst counter reset.
            self._restarted = True
        self.forwarder = addr
        self._last_pull = time.monotonic()
        return True

    def _uplinks(self, body):
        try:
            rxpk = json.loads(body).get("rxpk", [])  # stat-only reports have none
            if not isinstance(rxpk, list):
                raise ValueError("rxpk is not a list")
        except (ValueError, AttributeError, RecursionError) as exc:
            log.debug("Ignoring malformed PUSH_DATA: %s", exc)
            return
        for rx in rxpk:
            try:
                self._pending.append(parse_rxpk(rx))
            except (KeyError, TypeError, ValueError, AttributeError,
                    ArithmeticError) as exc:  # e.g. "freq": Infinity
                log.debug("Ignoring malformed rxpk: %s", exc)

    def receive(self):
        self._pump()
        # ConnectionError makes the runtime drop sessions and call start() again.
        if self._restarted:
            raise ConnectionError("Packet forwarder restarted")
        if time.monotonic() - self._last_pull > self.config.forwarder_timeout:
            raise ConnectionError("Packet forwarder stopped sending PULL_DATA")
        packets, self._pending = self._pending, []
        return packets

    def send(self, freq_hz, payload, rf_power=10, bandwidth=BW_500KHZ,
             tx_timestamp_us=0, invert_pol=False):
        if not 1 <= len(payload) <= 256:
            raise ValueError(f"payload size out of range: {len(payload)}")
        if bandwidth not in _BW_KHZ:
            raise ValueError(f"unsupported bandwidth code: {bandwidth}")
        self._pump()
        if self.forwarder is None:
            raise RuntimeError("No packet forwarder has sent PULL_DATA yet")
        txpk = {
            "freq": freq_hz / 1e6, "rfch": 0, "powe": rf_power, "modu": "LORA",
            "datr": f"SF{DR_LORA_SF5}BW{_BW_KHZ[bandwidth]}", "codr": "4/5",
            "ipol": bool(invert_pol), "prea": 12, "ncrc": False,
            "size": len(payload), "data": base64.b64encode(payload).decode(),
        }
        if tx_timestamp_us:
            txpk["tmst"] = tx_timestamp_us & 0xFFFFFFFF
        else:
            txpk["imme"] = True
        self._token = (self._token + 1) & 0xFFFF
        token = self._token.to_bytes(2, "big")
        self._acks.clear()
        try:
            self.sock.sendto(bytes([VERSION]) + token + bytes([PULL_RESP])
                             + json.dumps({"txpk": txpk}).encode(), self.forwarder)
        except OSError as exc:
            raise RuntimeError(f"PULL_RESP not sent: {exc}") from exc
        deadline = time.monotonic() + self.config.tx_ack_timeout
        while token not in self._acks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Never retry: the forwarder may already have queued the frame.
                raise RuntimeError("No TX_ACK from packet forwarder; transmission unconfirmed")
            select.select([self.sock], [], [], remaining)
            self._pump()
        body = self._acks.pop(token)
        if not body.strip(b"\x00 "):
            return  # an empty TX_ACK is an acknowledgement without error
        try:
            ack = json.loads(body)["txpk_ack"]
            error, warn = ack.get("error", "NONE"), ack.get("warn")
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise RuntimeError("Malformed TX_ACK from packet forwarder") from exc
        if error != "NONE":
            raise RuntimeError(f"Packet forwarder rejected TX: {error}")
        if warn:
            log.warning("Packet forwarder TX warning: %s (%s)", warn, ack.get("value"))
