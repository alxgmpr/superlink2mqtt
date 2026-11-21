"""Minimal radio bridge over SSH stdio or a TCP listener (--listen).

Only this process accesses the SX1302."""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import hmac
import json
import logging
import os
import secrets
import signal
import socket
import sys

from .hal import SX1302, DL_FREQ_HZ, BEACON_FREQ_HZ, BW_500KHZ

MAX_MESSAGE = 65536
PROTOCOL = 1
DEFAULT_PORT = 8572
log = logging.getLogger("superlink.radio")


def auth_proof(token, role, server_nonce, client_nonce):
    return hmac.new(token.encode(), f"{role}:{server_nonce}:{client_nonce}".encode(),
                    hashlib.sha256).hexdigest()


def authenticate(reader, writer, token):
    """TCP only: both ends prove they hold the token before any radio request.

    ponytail: this authenticates the connection, not each later message, and
    nothing is encrypted. Enough for a firewalled LAN; wrap the port in
    WireGuard (or use the SSH transport) if the path crosses anything untrusted.
    """
    ours = secrets.token_hex(16)
    writer.write(json.dumps({"nonce": ours}).encode() + b"\n")
    writer.flush()
    try:
        hello = json.loads(reader.readline(1024))
        theirs, proof = hello["nonce"], hello["proof"]
        if not (isinstance(theirs, str) and isinstance(proof, str) and theirs.isalnum()):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise PermissionError("Malformed radio authentication") from None
    if not hmac.compare_digest(proof.encode(), auth_proof(token, "client", ours, theirs).encode()):
        raise PermissionError("Wrong radio access token")
    writer.write(json.dumps({"proof": auth_proof(token, "server", ours, theirs)}).encode() + b"\n")
    writer.flush()


def dispatch(hal, request):
    op = request.get("op")
    if op == "receive":
        packets = [{**asdict(p), "payload": p.payload.hex()} for p in hal.receive()]
        return {"packets": packets}
    if op != "send":
        raise ValueError("Unknown radio operation")
    frequency = request.get("freq_hz")
    power = request.get("rf_power")
    timestamp = request.get("tx_timestamp_us")
    if type(frequency) is not int or frequency not in (*DL_FREQ_HZ, BEACON_FREQ_HZ):
        raise ValueError("Invalid SuperLink downlink frequency")
    if type(power) is not int or not 10 <= power <= 24:
        raise ValueError("Invalid TX power")
    if request.get("bandwidth") != BW_500KHZ:
        raise ValueError("Invalid downlink bandwidth")
    if type(timestamp) is not int or not 0 <= timestamp <= 0xFFFFFFFF:
        raise ValueError("Invalid hardware timestamp")
    if type(request.get("invert_pol")) is not bool:
        raise ValueError("Invalid IQ setting")
    if not isinstance(request.get("payload"), str) or len(request["payload"]) > 512:
        raise ValueError("Invalid TX payload")
    payload = bytes.fromhex(request["payload"])
    if not 1 <= len(payload) <= 256:
        raise ValueError("Invalid TX payload")
    hal.send(frequency, payload, rf_power=power, bandwidth=BW_500KHZ,
             tx_timestamp_us=timestamp, invert_pol=request["invert_pol"])
    return {}


def serve(reader, writer, hal):
    started = False
    try:
        while True:
            line = reader.readline(MAX_MESSAGE + 1)
            if not line:
                return
            if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
                raise ValueError("Invalid radio request size")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Expected a radio request object")
            try:
                if not started:
                    if request != {"op": "start", "protocol": PROTOCOL}:
                        raise ValueError("Expected protocol handshake")
                    hal.start()
                    started = True
                    result = {"protocol": PROTOCOL, "version": hal.version()}
                else:
                    result = dispatch(hal, request)
                response = {"ok": True, **result}
            except (ValueError, RuntimeError) as exc:
                response = {"ok": False, "error": str(exc)}
            writer.write(json.dumps(response, allow_nan=False).encode() + b"\n")
            writer.flush()
    finally:
        hal.stop()


@contextmanager
def exclusive():
    """One controller per physical concentrator, even during reconnects."""
    import fcntl
    with open(os.path.expanduser("~/.superlink-radio.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def listen(server, token, hal_factory):
    """Serve one authenticated controller at a time on a bound TCP socket."""
    while True:
        conn, peer = server.accept()
        with conn:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(5)
            reader, writer = conn.makefile("rb"), conn.makefile("wb")
            try:
                authenticate(reader, writer, token)
                # The bridge polls continuously; silence means it is gone.
                conn.settimeout(15)
                with exclusive():
                    log.info("Controller connected from %s", peer[0])
                    serve(reader, writer, hal_factory())
            except (OSError, ValueError, PermissionError) as exc:
                log.warning("Controller %s dropped: %s", peer[0], exc or type(exc).__name__)
            finally:
                for stream in (reader, writer):
                    try:
                        stream.close()
                    except OSError:
                        pass


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", metavar="HOST:PORT",
                        help=f"serve over TCP (e.g. 0.0.0.0:{DEFAULT_PORT}) instead of SSH stdio")
    parser.add_argument("--token-file", default="~/.superlink-radio.token",
                        help="shared access token for --listen")
    args = parser.parse_args(argv)
    if args.listen:
        logging.basicConfig(level=logging.INFO)
        with open(os.path.expanduser(args.token_file)) as f:
            token = f.read().strip()
        if len(token) < 32:
            parser.error("the access token must be at least 32 characters")
        host, _, port = args.listen.rpartition(":")
        # SystemExit unwinds serve(), which stops the concentrator.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        with socket.create_server((host, int(port))) as server:
            log.info("Radio listening on %s", args.listen)
            listen(server, token, SX1302)
        return

    def expired(*_):
        raise TimeoutError("Radio controller disconnected or idle")

    # Keep native printf/reset-script output off the JSON transport.
    output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    signal.signal(signal.SIGALRM, expired)
    signal.signal(signal.SIGTERM, expired)

    class WatchdogReader:
        def readline(self, limit):
            signal.alarm(15)
            line = sys.stdin.buffer.readline(limit)
            signal.alarm(0)
            return line

    with exclusive():
        try:
            serve(WatchdogReader(), output, SX1302())
        finally:
            signal.alarm(0)
            output.close()


if __name__ == "__main__":
    main()
