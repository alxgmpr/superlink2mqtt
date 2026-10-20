"""Home Assistant startup with settings available before radio provisioning."""
from concurrent.futures import Future
import logging
import os
from pathlib import Path
import secrets
import signal
import threading

import yaml

from superlink.bridge.config import RuntimeConfig, WebConfig
from superlink.bridge.store import JsonDeviceStore
from superlink.bridge.web import ManagementServer

from superlink.bridge.ha_settings import HASettings, prepare


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
    os.environ["SUPERLINK_HA_ROOT"] = str(root)
    os.environ["SUPERLINK_HA_OPTIONS"] = str(options_path)
    os.environ["SUPERLINK_HA_LAUNCHER"] = str(Path(__file__).resolve())
    settings = HASettings(root, options_path)
    pending = PendingSetup()
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    server = None
    try:
        while not stopped.is_set():
            prepared_revision = settings.revision()
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
            pending.state["settings_revision"] = settings.revision()
            if config is not None and healthy_store:
                break
            if server is None:
                server = ManagementServer(pending, WebConfig(token, host="0.0.0.0", port=8099, ingress=True),
                                          management=pending, settings=settings)
                server.start()
                logging.info("Setup UI ready; waiting for radio and MQTT configuration")
            stopped.wait(5)
    finally:
        if server is not None:
            server.stop()
    if not stopped.is_set():
        os.environ["SUPERLINK_HA_REVISION"] = prepared_revision
        os.execvp("superlink-bridged", ["superlink-bridged", "--config", str(path)])


if __name__ == "__main__":
    main()
