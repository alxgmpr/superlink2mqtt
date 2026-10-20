"""Persistent HA connection settings shared by bootstrap and the running bridge."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import urllib.request

import paramiko
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .store import JsonDeviceStore

# One field definition drives both embedded forms and server-side validation.
FIELDS = {
    "radio": [
        {"name": "host", "label": "Pi address", "required": True, "placeholder": "192.168.1.50"},
        {"name": "username", "label": "Pi account", "required": True},
        {"name": "port", "label": "SSH port", "type": "number", "min": 1, "max": 65535, "required": True},
        {"name": "host_key", "label": "Pi host public key", "required": True,
         "placeholder": "ssh-ed25519 AAAA…", "help": "On your Pi: cat /etc/ssh/ssh_host_ed25519_key.pub"},
    ],
    "mqtt": [
        {"name": "mode", "label": "Broker", "type": "select", "choices": [["automatic", "Home Assistant broker"], ["custom", "Custom broker"]]},
        {"name": "host", "label": "Broker address", "required": True, "when": {"mode": "custom"}},
        {"name": "username", "label": "Username", "when": {"mode": "custom"}},
        {"name": "port", "label": "Broker port", "type": "number", "min": 1, "max": 65535, "required": True, "when": {"mode": "custom"}},
        {"name": "password", "label": "Password", "type": "password", "when": {"mode": "custom"}, "help": "Leave blank to keep the saved password."},
        {"name": "clear_password", "label": "Clear saved password", "type": "checkbox", "when": {"mode": "custom"}, "advanced": True},
        {"name": "tls", "label": "Use TLS", "type": "checkbox", "when": {"mode": "custom"}},
        {"name": "base_topic", "label": "Sensor topic", "required": True, "advanced": True},
        {"name": "discovery_prefix", "label": "Discovery prefix", "required": True, "advanced": True},
    ],
}


def mqtt_service():
    request = urllib.request.Request("http://supervisor/services/mqtt", headers={
        "Authorization": "Bearer " + os.environ["SUPERVISOR_TOKEN"]})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["data"]


def atomic_write(path, content):
    """Private same-directory replace; a failed write leaves the old file intact."""
    fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def host_key(value):
    parts = value.split()
    if len(parts) < 2:
        raise ValueError("Enter the Pi host public key, starting with ssh-ed25519, ssh-rsa or ecdsa-sha2-.")
    try:
        key = paramiko.PKey.from_type_string(parts[0], base64.b64decode(parts[1], validate=True))
    except Exception:
        raise ValueError("The Pi host public key is invalid.") from None
    return f"{key.get_name()} {key.get_base64()}"


class HASettings:
    def __init__(self, root, options_path, *, on_change=lambda: None):
        self.root = Path(root)
        self.options_path = Path(options_path)
        self.path = self.root / "settings.json"
        self.on_change = on_change
        self.lock = threading.RLock()
        self.restart_requested = False

    def read(self):
        try:
            doc = json.loads(self.path.read_text()) if self.path.exists() else {}
            if not isinstance(doc, dict) or any(not isinstance(doc.get(k, {}), dict) for k in ("radio", "mqtt")):
                raise ValueError()
            return doc
        except (ValueError, OSError):
            raise ValueError("Saved settings need recovery; no settings have been changed.") from None

    def base(self):
        path = self.root / "bridge.yaml"
        if not path.exists():
            return {}
        try:
            doc = yaml.safe_load(path.read_text())
            if not isinstance(doc, dict) or len(bytes.fromhex(doc.get("gw_mac", ""))) != 6:
                raise ValueError()
            return doc
        except (ValueError, TypeError, OSError, yaml.YAMLError):
            raise ValueError("Gateway configuration needs recovery; its identity has not been changed.") from None

    def values(self):
        doc, base = self.read(), self.base()
        options = json.loads(self.options_path.read_text())
        radio = {"host": options.get("radio_host", ""), "username": options.get("radio_user", "alex"),
                 "port": options.get("radio_port", 22), "host_key": ""}
        radio.update(doc.get("radio", {}))
        if not radio["host_key"] and (self.root / "known_hosts").is_file():
            try:
                keys = paramiko.HostKeys(str(self.root / "known_hosts"))
                name = radio["host"] if radio["port"] == 22 else f'[{radio["host"]}]:{radio["port"]}'
                matched = keys.lookup(name)
                if matched:
                    key = next(iter(matched.values()))
                    radio["host_key"] = f"{key.get_name()} {key.get_base64()}"
            except (OSError, ValueError, paramiko.SSHException):
                pass
        mqtt = {"mode": "automatic", "host": "", "port": 1883, "username": "", "password": "", "clear_password": False, "tls": False,
                "base_topic": "superlink", "discovery_prefix": "homeassistant"}
        for key in ("base_topic", "discovery_prefix"):
            if key in (base.get("mqtt") or {}):
                mqtt[key] = base["mqtt"][key]
        mqtt.update(doc.get("mqtt", {}))
        return {"radio": radio, "mqtt": mqtt}

    def revision(self):
        digest = hashlib.sha256()
        for path in (self.path, self.options_path, self.root / "bridge.yaml", self.root / "known_hosts", self.root / "radio_ed25519"):
            digest.update(path.name.encode())
            digest.update(path.read_bytes() if path.exists() else b"missing")
        return digest.hexdigest()

    def snapshot(self):
        with self.lock:
            values = self.values()
            password_set = bool(values["mqtt"].pop("password", ""))
            public_key = ""
            key_path = self.root / "radio_ed25519"
            if key_path.is_file():
                try:
                    key = paramiko.Ed25519Key.from_private_key_file(str(key_path))
                    public_key = f"{key.get_name()} {key.get_base64()} superlink-radio"
                except (OSError, ValueError, paramiko.SSHException):
                    pass
            return {"fields": FIELDS, "values": values, "revision": self.revision(),
                    "public_key": public_key, "password_set": password_set}

    def validate(self, section, values):
        if not isinstance(section, str) or section not in FIELDS or not isinstance(values, dict):
            raise ValueError("Choose radio or MQTT settings.")
        allowed = {f["name"] for f in FIELDS[section]}
        if set(values) - allowed:
            raise ValueError("Unknown settings field.")
        result = self.values()[section]
        previous = dict(result)
        for field in FIELDS[section]:
            name, kind = field["name"], field.get("type", "text")
            if name not in values:
                continue
            value = values[name]
            if kind == "checkbox":
                valid = type(value) is bool
            elif kind == "number":
                valid = type(value) is int and field["min"] <= value <= field["max"]
            else:
                valid = isinstance(value, str) and len(value) <= (4096 if name in ("password", "host_key") else 255) and not any(ord(c) < 32 for c in value)
                if valid and kind != "password":
                    value = value.strip()
                if valid and kind == "select":
                    valid = value in [c[0] for c in field["choices"]]
            if not valid:
                raise ValueError(f'Check {field["label"].lower()}.')
            if name != "password" or value:
                result[name] = value
        for field in FIELDS[section]:
            active = all(result.get(k) == v for k, v in field.get("when", {}).items())
            if active and field.get("required") and result.get(field["name"]) in (None, ""):
                raise ValueError(f'{field["label"]} is required.')
        if section == "radio" or result["mode"] == "custom":
            if not re.fullmatch(r"[A-Za-z0-9_.:%-]+", result["host"]):
                raise ValueError("Enter a hostname or IP address without a scheme or path.")
        if section == "radio":
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]*[$]?", result["username"]):
                raise ValueError("Enter a valid Pi account name.")
            result["host_key"] = host_key(result["host_key"])
        else:
            clear_password = result.pop("clear_password", False)
            if clear_password:
                if values.get("password"):
                    raise ValueError("Enter a new password or clear the saved password, not both.")
                result["password"] = ""
            elif previous.get("password") and not values.get("password") and any(result[k] != previous[k] for k in ("host", "port", "username", "tls")):
                raise ValueError("For a different broker or account, enter a password or choose Clear saved password.")
            for name in ("base_topic", "discovery_prefix"):
                if any(c in result[name] for c in ("#", "+", "\x00")) or result[name].startswith("/") or result[name].endswith("/"):
                    raise ValueError("MQTT topics cannot use wildcards or leading/trailing slashes.")
        return result

    def save(self, body):
        with self.lock:
            if set(body) != {"section", "values", "revision"}:
                raise ValueError("Expected a settings section, values and revision.")
            if body["revision"] != self.revision():
                raise ValueError("Settings changed elsewhere. Reload settings before saving.")
            # Never generate a new gateway identity over a damaged store/config.
            records = JsonDeviceStore(str(self.root / "devices.json")).load_all()
            base, doc = self.base(), self.read()
            section = body["section"]
            values = self.validate(section, body["values"])
            if not base and not doc.get("gateway_mac") and records:
                raise ValueError("Restore the original gateway configuration before changing settings.")
            if section == "radio":
                if not base and not doc.get("gateway_mac"):
                    mac = bytearray(secrets.token_bytes(6)); mac[0] = (mac[0] | 2) & 254
                    doc["gateway_mac"] = mac.hex()
                key_path = self.root / "radio_ed25519"
                if not key_path.exists():
                    key = Ed25519PrivateKey.generate()
                    data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption())
                    # Exclusive creation: existing operator credentials are never replaced.
                    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data); stream.flush(); os.fsync(stream.fileno())
            changed = doc.get(section) != values
            if changed:
                doc[section] = values
                content = json.dumps(doc, indent=2) + "\n"
                if self.path.exists():
                    atomic_write(self.root / "settings.json.bak", self.path.read_text())
                atomic_write(self.path, content)
                self.restart_requested = True
            result = self.snapshot()
            result["changed"] = changed
            if changed:
                self.on_change()
            return result


def prepare(root, options_path, get_mqtt=mqtt_service):
    root = Path(root)
    settings = HASettings(root, options_path)
    doc = settings.read()
    options = json.loads(Path(options_path).read_text())
    radio = doc.get("radio")
    missing = []
    if not (radio or {}).get("host", options.get("radio_host")):
        missing.append("radio_host")
    for name in ("radio_ed25519", "known_hosts", "bridge.yaml"):
        if name == "known_hosts" and radio and radio.get("host_key"):
            continue
        if name == "bridge.yaml" and doc.get("gateway_mac"):
            continue
        if not (root / name).is_file():
            missing.append(name)
    if missing:
        return None, missing
    try:
        config = copy.deepcopy(settings.base()) or {"gw_mac": doc["gateway_mac"], "adopt": []}
    except (ValueError, KeyError):
        return None, ["bridge.yaml"]
    config["radio"] = {"host": options.get("radio_host", ""), "username": options.get("radio_user", "alex"),
                       "port": options.get("radio_port", 22), "key_file": str(root / "radio_ed25519"),
                       "known_hosts": str(root / "known_hosts")}
    if radio:
        config["radio"].update({k: radio[k] for k in ("host", "username", "port")})
        name = radio["host"] if radio["port"] == 22 else f'[{radio["host"]}]:{radio["port"]}'
        pinned = root / "radio_known_hosts"
        content = f'{name} {host_key(radio["host_key"])}\n'
        if not pinned.exists() or pinned.read_text() != content:
            atomic_write(pinned, content)
        config["radio"]["known_hosts"] = str(pinned)
    config["store_path"] = str(root / "devices.json")
    config["control_socket"] = None
    config["web"] = {"host": "0.0.0.0", "port": 8099, "ingress": True}
    saved = doc.get("mqtt", {})
    try:
        mqtt = saved if saved.get("mode") == "custom" else get_mqtt()
        existing = config.get("mqtt") or {}
        config["mqtt"] = {"host": mqtt["host"], "port": mqtt["port"],
                          "username": mqtt.get("username") or None, "password": mqtt.get("password") or None,
                          "tls": mqtt.get("tls", mqtt.get("ssl", False)),
                          "base_topic": saved.get("base_topic", existing.get("base_topic", "superlink")),
                          "discovery_prefix": saved.get("discovery_prefix", existing.get("discovery_prefix", "homeassistant"))}
    except (OSError, ValueError, KeyError, TypeError):
        return None, ["mqtt"]
    return config, []
