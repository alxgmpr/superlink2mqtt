"""SX1302 HAL over a pinned SSH connection or a token-authenticated TCP socket.

No sensor keys live on the Pi."""
from dataclasses import dataclass, field
import hmac
import json
import secrets
import socket
import threading

import paramiko

from .hal import RxPacket, BW_500KHZ
from .radio_server import DEFAULT_PORT, auth_proof

MAX_MESSAGE = 65536
PROTOCOL = 1


@dataclass
class RemoteRadioConfig:
    host: str
    username: str
    key_file: str
    known_hosts: str
    port: int = 22

    def __post_init__(self):
        for name in ("host", "username", "key_file", "known_hosts"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"radio.{name} is required")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("radio.port must be between 1 and 65535")


@dataclass
class TcpRadioConfig:
    host: str
    token: str = field(repr=False)
    port: int = DEFAULT_PORT

    backend = "sx1302-tcp"

    def __post_init__(self):
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("radio.tcp.host is required")
        if not isinstance(self.token, str) or len(self.token) < 32:
            raise ValueError("radio.tcp.token must be at least 32 characters")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("radio.tcp.port must be between 1 and 65535")


class RemoteSX1302:
    transport = "SSH"

    def __init__(self, config):
        self.config = config
        self.client = None
        self.channel = None
        self.reader = None
        self._version = "remote SX1302"

    def start(self):
        cfg = self.config
        self.client = paramiko.SSHClient()
        try:
            self.client.load_host_keys(cfg.known_hosts)
            self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
            self.client.connect(cfg.host, port=cfg.port, username=cfg.username,
                                key_filename=cfg.key_file, look_for_keys=False,
                                allow_agent=False, timeout=10, auth_timeout=10,
                                banner_timeout=10)
            transport = self.client.get_transport()
            transport.set_keepalive(5)
            self.channel = transport.open_session(timeout=10)
            self.channel.settimeout(20)
            self.channel.exec_command("superlink-radio")
            self.reader = self.channel.makefile("rb")
            # Native HAL diagnostics go to stderr, never into the wire protocol.
            threading.Thread(target=self._drain_stderr, args=(self.channel,),
                             daemon=True, name="radio-stderr").start()
            self._handshake()
        except Exception:
            self.stop()
            raise

    def _handshake(self):
        result = self._rpc({"op": "start", "protocol": PROTOCOL})
        if result.get("protocol") != PROTOCOL:
            raise ConnectionError("Unsupported radio bridge protocol")
        self._version = result["version"]
        self.channel.settimeout(5)

    @staticmethod
    def _drain_stderr(channel):
        try:
            while channel.recv_stderr(4096):
                pass
        except (OSError, EOFError):
            pass

    def _rpc(self, request):
        try:
            self.channel.sendall(json.dumps(request).encode() + b"\n")
            line = self.reader.readline(MAX_MESSAGE + 1)
            if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
                raise ValueError("Incomplete or oversized radio response")
            response = json.loads(line)
            if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                raise ValueError("Invalid radio response")
        except (OSError, EOFError, ValueError, paramiko.SSHException) as exc:
            self.stop()
            # Never retry TX: it may already have reached the concentrator.
            raise ConnectionError("Radio connection lost; restarting session") from exc
        if not response["ok"]:
            raise RuntimeError(response.get("error", "Radio operation failed"))
        return response

    def receive(self):
        try:
            result = self._rpc({"op": "receive"})
        except RuntimeError as exc:
            self.stop()
            raise ConnectionError("Radio receive failed") from exc
        try:
            packets = result["packets"]
            if not isinstance(packets, list) or len(packets) > 16:
                raise ValueError("Invalid RX batch")
            return [RxPacket(**{**p, "payload": bytes.fromhex(p["payload"])})
                    for p in packets]
        except (KeyError, TypeError, ValueError) as exc:
            self.stop()
            raise ConnectionError("Invalid radio RX response") from exc

    def send(self, freq_hz, payload, rf_power=10, bandwidth=BW_500KHZ,
             tx_timestamp_us=0, invert_pol=False):
        self._rpc({"op": "send", "freq_hz": freq_hz, "payload": payload.hex(),
                   "rf_power": rf_power, "bandwidth": bandwidth,
                   "tx_timestamp_us": tx_timestamp_us & 0xFFFFFFFF,
                   "invert_pol": invert_pol})

    def version(self):
        return f"{self.transport} / {self._version}"

    def stop(self):
        if self.channel is not None:
            self.channel.close()
        if self.reader is not None:
            self.reader.close()
        if self.client is not None:
            self.client.close()
        self.channel = self.reader = self.client = None


class TcpSX1302(RemoteSX1302):
    """Same JSON-lines protocol, straight to `radio_server --listen`."""
    transport = "TCP"

    def start(self):
        cfg = self.config
        try:
            self.channel = socket.create_connection((cfg.host, cfg.port), timeout=10)
            # One small request per poll: Nagle would stall each by ~40 ms.
            self.channel.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.channel.settimeout(20)
            self.reader = self.channel.makefile("rb")
            try:
                theirs = json.loads(self.reader.readline(1024))["nonce"]
                ours = secrets.token_hex(16)
                self.channel.sendall(json.dumps({
                    "nonce": ours,
                    "proof": auth_proof(cfg.token, "client", theirs, ours)}).encode() + b"\n")
                # The server just closes on a wrong token, so this read fails.
                proof = json.loads(self.reader.readline(1024))["proof"]
                expected = auth_proof(cfg.token, "server", theirs, ours)
                if not (isinstance(proof, str)
                        and hmac.compare_digest(proof.encode(), expected.encode())):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise ConnectionError("Radio authentication failed; check the access token") from None
            self._handshake()
        except Exception:
            self.stop()
            raise
