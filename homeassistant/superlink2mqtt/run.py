"""Home Assistant startup with a read-only setup UI until provisioning is ready."""
from concurrent.futures import Future
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import threading
import urllib.request

import yaml

from superlink.bridge.config import RuntimeConfig, WebConfig
from superlink.bridge.store import JsonDeviceStore
from superlink.bridge.web import ManagementServer


def mqtt_service():
    request = urllib.request.Request("http://supervisor/services/mqtt", headers={
        "Authorization": "Bearer " + os.environ["SUPERVISOR_TOKEN"]})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["data"]


def prepare(root, options_path, get_mqtt=mqtt_service):
    """Read prerequisites without generating identities or rewriting user files."""
    options = json.loads(options_path.read_text())
    missing = []
    if not options.get("radio_host"):
        missing.append("radio_host")
    for name in ("radio_ed25519", "known_hosts", "bridge.yaml"):
        if not (root / name).is_file():
            missing.append(name)
    if missing:
        return None, missing
    try:
        config = yaml.safe_load((root / "bridge.yaml").read_text())
        if not isinstance(config, dict) or len(bytes.fromhex(config.get("gw_mac", ""))) != 6:
            return None, ["bridge.yaml"]
    except (ValueError, TypeError, yaml.YAMLError):
        return None, ["bridge.yaml"]
    config["radio"] = {
        "host": options["radio_host"], "username": options.get("radio_user", "alex"),
        "port": options.get("radio_port", 22), "key_file": str(root / "radio_ed25519"),
        "known_hosts": str(root / "known_hosts"),
    }
    config["store_path"] = str(root / "devices.json")
    config["control_socket"] = None
    config["web"] = {"host": "0.0.0.0", "port": 8099, "ingress": True}
    try:
        mqtt = get_mqtt()
        existing = config.get("mqtt") or {}
        config["mqtt"] = {"host": mqtt["host"], "port": mqtt["port"],
                          "username": mqtt["username"], "password": mqtt["password"],
                          "tls": mqtt.get("ssl", False),
                          "base_topic": existing.get("base_topic", "superlink"),
                          "discovery_prefix": existing.get("discovery_prefix", "homeassistant")}
    except (OSError, ValueError, KeyError, TypeError):
        return None, ["mqtt"]
    return config, []


class PendingSetup:
    """Only status is available before startup; no radio or registry writes."""
    def __init__(self):
        self.state = {}

    def update(self, root, missing):
        blocked = False
        try:
            records = JsonDeviceStore(str(root / "devices.json")).load_all()
        except (OSError, ValueError, TypeError, AttributeError):
            records = []
            blocked = True
        self.state = {
            "demo": False, "backend": "sx1302-ssh", "gateway_mac": None,
            "radio": {"state": "configured" if missing == ["mqtt"] else "unconfigured",
                      "error": "Device store needs recovery; saved data has not been changed." if blocked else
                               "Finish the connection steps to start your bridge."},
            "mqtt": "disconnected", "activity": [],
            "setup": {"missing": missing, "blocked": blocked},
            "devices": [{"mac": r.mac.hex(), "name": r.name or f"SuperLink {r.mac.hex()[-6:]}",
                         "adopted": r.adopted, "has_session": False, "state": "awaiting_reconnect",
                         "last_seen": None, "rssi": None, "snr": None, "firmware": None,
                         "properties": []} for r in records],
        }
        return not blocked

    def snapshot(self):
        return self.state

    def command(self, *_):
        raise ValueError("Complete radio and MQTT setup before pairing sensors")

    def submit_management(self, operation):
        result = Future()
        try:
            result.set_result(operation())
        except Exception as error:
            result.set_exception(error)
        return result


def main(root=Path("/config"), options_path=Path("/data/options.json"),
         path=Path("/tmp/superlink-runtime.yaml")):
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO)
    token = secrets.token_urlsafe(32)
    os.environ["SUPERLINK_WEB_TOKEN"] = token
    pending = PendingSetup()
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    server = None
    try:
        while not stopped.is_set():
            try:
                config, missing = prepare(root, options_path)
            except (OSError, ValueError, TypeError, AttributeError):
                config, missing = None, ["bridge.yaml"]
            if config is not None:
                # Validate before replacing the setup UI with the real daemon.
                path.write_text(yaml.safe_dump(config))
                try:
                    RuntimeConfig.load(str(path))
                except (ValueError, TypeError, KeyError, AttributeError):
                    config, missing = None, ["bridge.yaml"]
            healthy_store = pending.update(root, missing)
            if config is not None and healthy_store:
                break
            if server is None:
                server = ManagementServer(pending, WebConfig(token, host="0.0.0.0", port=8099, ingress=True),
                                          management=pending)
                server.start()
                logging.info("Setup UI ready; waiting for radio and MQTT configuration")
            stopped.wait(5)
    finally:
        if server is not None:
            server.stop()
    if not stopped.is_set():
        os.execvp("superlink-bridged", ["superlink-bridged", "--config", str(path)])


if __name__ == "__main__":
    main()
