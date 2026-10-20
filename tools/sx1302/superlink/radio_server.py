"""Minimal SSH stdio radio bridge. Only this process accesses the SX1302."""
from dataclasses import asdict
import json
import os
import signal
import sys

from .hal import SX1302, DL_FREQ_HZ, BEACON_FREQ_HZ, BW_500KHZ

MAX_MESSAGE = 65536
PROTOCOL = 1


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


def main():
    import fcntl

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

    # One SSH controller per physical concentrator, even during reconnects.
    with open(os.path.expanduser("~/.superlink-radio.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            serve(WatchdogReader(), output, SX1302())
        finally:
            signal.alarm(0)
            output.close()


if __name__ == "__main__":
    main()
