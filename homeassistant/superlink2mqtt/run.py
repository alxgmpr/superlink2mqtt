"""Home Assistant startup: persistent config, Supervisor MQTT, ingress UI."""
import json
import os
from pathlib import Path
import secrets
import urllib.request

import yaml


def main():
    os.umask(0o077)
    root = Path("/config")
    options = json.loads(Path("/data/options.json").read_text())
    if not options.get("radio_host"):
        raise ValueError("Set radio_host in the app configuration")
    for name in ("radio_ed25519", "known_hosts", "bridge.yaml"):
        if not (root / name).is_file():
            raise ValueError(f"Missing /config/{name}; complete radio setup first")
    config = yaml.safe_load((root / "bridge.yaml").read_text())
    config["radio"] = {
        "host": options["radio_host"], "username": options["radio_user"],
        "port": options["radio_port"], "key_file": str(root / "radio_ed25519"),
        "known_hosts": str(root / "known_hosts"),
    }
    config["store_path"] = str(root / "devices.json")
    config["control_socket"] = None
    config["web"] = {"host": "0.0.0.0", "port": 8099, "ingress": True}
    request = urllib.request.Request("http://supervisor/services/mqtt", headers={
        "Authorization": "Bearer " + os.environ["SUPERVISOR_TOKEN"]})
    with urllib.request.urlopen(request, timeout=15) as response:
        mqtt = json.load(response)["data"]
    existing = config.get("mqtt") or {}
    config["mqtt"] = {"host": mqtt["host"], "port": mqtt["port"],
                      "username": mqtt["username"], "password": mqtt["password"],
                      "tls": mqtt.get("ssl", False),
                      "base_topic": existing.get("base_topic", "superlink"),
                      "discovery_prefix": existing.get("discovery_prefix", "homeassistant")}
    # Runtime credentials stay in the private, ephemeral container filesystem.
    path = Path("/tmp/superlink-runtime.yaml")
    path.write_text(yaml.safe_dump(config))
    os.environ["SUPERLINK_WEB_TOKEN"] = secrets.token_urlsafe(32)
    os.execvp("superlink-bridged", ["superlink-bridged", "--config", str(path)])


if __name__ == "__main__":
    main()
