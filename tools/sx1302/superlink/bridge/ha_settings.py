"""Persistent HA connection settings shared by bootstrap and the running bridge."""
import base64
import copy
import dataclasses
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

from .config import DEFAULT_PAIRING_KEY, RuntimeConfig
from .store import JsonDeviceStore

DEFAULTS = {f.name: f.default for f in dataclasses.fields(RuntimeConfig)}
DEFAULT_KEY_HEX = DEFAULT_PAIRING_KEY.hex()

# One field definition drives both embedded forms and server-side validation.
FIELDS = {
    "radio": [
        {"name": "transport", "label": "Connection", "type": "select", "choices": [["ssh", "SSH"], ["tcp", "TCP with access token"]]},
        {"name": "host", "label": "Pi address", "required": True, "placeholder": "192.168.1.50"},
        {"name": "username", "label": "Pi account", "required": True, "when": {"transport": "ssh"}},
        {"name": "port", "label": "Port", "type": "number", "min": 1, "max": 65535, "required": True,
         "help": "SSH: usually 22. TCP: the radio service port, 8572 by default."},
        {"name": "host_key", "label": "Pi host public key", "required": True, "when": {"transport": "ssh"},
         "placeholder": "ssh-ed25519 AAAA…", "help": "On your Pi: cat /etc/ssh/ssh_host_ed25519_key.pub"},
        {"name": "token", "label": "Radio access token", "type": "password", "when": {"transport": "tcp"},
         "help": "On your Pi: cat ~/.superlink-radio.token. Leave blank to keep the saved token."},
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
    # Network-level knobs. Defaults reproduce the bridge's built-in behaviour.
    "network": [
        {"name": "gateway_mac", "label": "Gateway MAC", "placeholder": "Created when you first save radio settings",
         "help": "The identity sensors pair with. Changing it disconnects paired sensors until you pair them again."},
        {"name": "pairing_key", "label": "Pairing key", "type": "password", "placeholder": "Factory default",
         "help": "32 bytes as 64 hex digits. Factory-reset sensors only pair with the factory default. Leave blank to keep the current key."},
        {"name": "auto_pair", "label": "Pair new sensors", "type": "select",
         "choices": [["manual", "When I choose Pair"], ["all", "Automatically, any sensor in pairing mode"],
                     ["list", "Automatically, only the sensors listed"]]},
        {"name": "auto_pair_macs", "label": "Sensor MACs to pair automatically", "required": True, "when": {"auto_pair": "list"},
         "placeholder": "AA:BB:CC:DD:EE:FF, …", "help": "Separate MACs with commas or spaces."},
        {"name": "use_default_key", "label": "Use the factory default pairing key", "type": "checkbox", "advanced": True},
        {"name": "allow_repair", "label": "Allow changes that need sensors to be paired again", "type": "checkbox", "advanced": True},
        {"name": "log_level", "label": "Log level", "type": "select", "advanced": True,
         "choices": [["DEBUG", "Debug"], ["INFO", "Info"], ["WARNING", "Warning"], ["ERROR", "Error"]]},
        {"name": "invert_iq", "label": "Invert downlink IQ", "type": "checkbox", "advanced": True},
        {"name": "downlink_delay_us", "label": "Downlink delay (µs)", "type": "number", "min": 0, "max": 10_000_000,
         "required": True, "advanced": True, "help": f'Time from an uplink to the reply; {DEFAULTS["downlink_delay_us"]} by default.'},
        {"name": "burst_spacing_us", "label": "Burst spacing (µs)", "type": "number", "min": 0, "max": 5_000_000,
         "required": True, "advanced": True, "help": f'Gap between frames in one reply; {DEFAULTS["burst_spacing_us"]} by default.'},
        {"name": "link_lost_timeout", "label": "Link-lost timeout (s)", "type": "number", "min": 60, "max": 86400,
         "required": True, "advanced": True, "help": f'Silence before a sensor shows as disconnected; {DEFAULTS["link_lost_timeout"]:g} by default.'},
        {"name": "watchdog_timeout", "label": "Watchdog timeout (s)", "type": "number", "min": 0, "max": 86400,
         "required": True, "advanced": True, "help": f'Silence before the bridge forces a reconnect; {DEFAULTS["watchdog_timeout"]:g} by default, 0 turns it off.'},
        {"name": "watchdog_short_challenge_k", "label": "Watchdog rejected-reconnect limit", "type": "number", "min": 0, "max": 100,
         "required": True, "advanced": True, "help": f'Rejected reconnects before the watchdog acts; {DEFAULTS["watchdog_short_challenge_k"]} by default, 0 turns it off.'},
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


def mac_label(value):
    """Accept aa:bb:cc:dd:ee:ff / aa-bb-… / aabbccddeeff; return AA:BB:CC:DD:EE:FF (as the UI shows MACs) or None."""
    value = value.replace(":", "").replace("-", "").upper()
    return ":".join(re.findall("..", value)) if re.fullmatch(r"[0-9A-F]{12}", value) else None


def mac_hex(label):
    return label.replace(":", "").lower()


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
            if not isinstance(doc, dict) or any(not isinstance(doc.get(k, {}), dict) for k in FIELDS):
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
        radio = {"transport": "ssh", "host": options.get("radio_host", ""), "username": options.get("radio_user", "pi"),
                 "port": options.get("radio_port", 22), "host_key": "", "token": ""}
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
        adopt = base.get("adopt") or []
        auto_all = isinstance(adopt, str) and adopt.lower() == "all"
        network = {"gateway_mac": "", "pairing_key": str(base.get("pairing_key") or DEFAULT_KEY_HEX).lower(),
                   "use_default_key": False, "allow_repair": False,
                   "auto_pair": "all" if auto_all else "list" if adopt else "manual",
                   "auto_pair_macs": "" if auto_all else ", ".join(mac_label(m) or m for m in adopt),
                   "log_level": (base.get("log") or {}).get("level", DEFAULTS["log_level"]),
                   "invert_iq": bool(base.get("invert_iq", DEFAULTS["invert_iq"]))}
        for name in ("downlink_delay_us", "burst_spacing_us", "link_lost_timeout", "watchdog_timeout", "watchdog_short_challenge_k"):
            network[name] = int(base.get(name, DEFAULTS[name]))
        network.update(doc.get("network", {}))
        # A saved override wins; otherwise the identity bridge.yaml or the radio save created.
        inherited = base.get("gw_mac") or doc.get("gateway_mac", "")
        network["gateway_mac"] = network["gateway_mac"] or mac_label(inherited) or inherited
        return {"radio": radio, "mqtt": mqtt, "network": network}

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
            token_set = bool(values["radio"].pop("token", ""))
            pairing_key_set = values["network"].pop("pairing_key") != DEFAULT_KEY_HEX
            public_key = ""
            key_path = self.root / "radio_ed25519"
            if key_path.is_file() and values["radio"]["transport"] == "ssh":
                try:
                    key = paramiko.Ed25519Key.from_private_key_file(str(key_path))
                    public_key = f"{key.get_name()} {key.get_base64()} superlink-radio"
                except (OSError, ValueError, paramiko.SSHException):
                    pass
            return {"fields": FIELDS, "values": values, "revision": self.revision(),
                    "public_key": public_key, "password_set": password_set, "token_set": token_set,
                    "pairing_key_set": pairing_key_set}

    def validate(self, section, values):
        if not isinstance(section, str) or section not in FIELDS or not isinstance(values, dict):
            raise ValueError("Choose a settings section.")
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
                valid = isinstance(value, str) and len(value) <= (4096 if name in ("password", "host_key", "auto_pair_macs") else 255) and not any(ord(c) < 32 for c in value)
                if valid and kind != "password":
                    value = value.strip()
                if valid and kind == "select":
                    valid = value in [c[0] for c in field["choices"]]
            if not valid:
                raise ValueError(f'Check {field["label"].lower()}.')
            if kind != "password" or value:
                result[name] = value
        for field in FIELDS[section]:
            active = all(result.get(k) == v for k, v in field.get("when", {}).items())
            if active and field.get("required") and result.get(field["name"]) in (None, ""):
                raise ValueError(f'{field["label"]} is required.')
        if section == "radio" or result.get("mode") == "custom":
            if not re.fullmatch(r"[A-Za-z0-9_.:%-]+", result["host"]):
                raise ValueError("Enter a hostname or IP address without a scheme or path.")
        if section == "radio" and result["transport"] == "tcp":
            result["token"] = result["token"].strip()  # pasted from `cat`
            if len(result["token"]) < 32 or len(result["token"]) > 255:
                raise ValueError("Enter the radio access token from your Pi (32 to 255 characters).")
        elif section == "network":
            self.validate_network(result, previous, values)
        elif section == "radio":
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

    def validate_network(self, result, previous, values):
        if result["gateway_mac"] != previous["gateway_mac"]:
            mac = mac_label(result["gateway_mac"])
            if mac is None or int(mac[:2], 16) & 1 or mac == "00:00:00:00:00:00":
                raise ValueError("Enter the gateway MAC as 12 hex digits, for example 02:00:00:00:00:01.")
            result["gateway_mac"] = mac
        if result.pop("use_default_key"):
            if values.get("pairing_key"):
                raise ValueError("Enter a pairing key or choose the factory default, not both.")
            result["pairing_key"] = DEFAULT_KEY_HEX
        key = result["pairing_key"].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("Enter the pairing key as 64 hex digits (32 bytes).")
        result["pairing_key"] = key
        macs = []
        if result["auto_pair"] == "list":
            for part in re.split(r"[\s,]+", result["auto_pair_macs"].strip()):
                mac = mac_label(part)
                if mac is None:
                    raise ValueError(f"{part!r} is not a sensor MAC.")
                macs.append(mac)
        result["auto_pair_macs"] = ", ".join(macs)
        # Paired sensors hold keys bound to this identity; a change orphans them.
        if result.pop("allow_repair"):
            return
        if (result["gateway_mac"], key) != (previous["gateway_mac"], previous["pairing_key"]):
            paired = sum(r.adopted for r in JsonDeviceStore(str(self.root / "devices.json")).load_all())
            if paired:
                raise ValueError(f"{paired} paired sensor{'s' if paired > 1 else ''} would need to be paired again. "
                                 "Choose Allow changes that need sensors to be paired again under Advanced settings to continue.")

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
            restoring = (doc.get("network") or {}).get("gateway_mac") or (section == "network" and values["gateway_mac"])
            if not base and not doc.get("gateway_mac") and records and not restoring:
                raise ValueError("Restore the original gateway configuration before changing settings.")
            if section == "radio":
                if not base and not doc.get("gateway_mac"):
                    mac = bytearray(secrets.token_bytes(6)); mac[0] = (mac[0] | 2) & 254
                    doc["gateway_mac"] = mac.hex()
                key_path = self.root / "radio_ed25519"
                if values["transport"] == "ssh" and not key_path.exists():
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
    tcp = (radio or {}).get("transport") == "tcp"
    missing = []
    if not (radio or {}).get("host", options.get("radio_host")):
        missing.append("radio_host")
    for name in ("radio_ed25519", "known_hosts", "bridge.yaml"):
        if name == "known_hosts" and radio and radio.get("host_key"):
            continue
        if tcp and name != "bridge.yaml":
            continue
        if name == "bridge.yaml" and (doc.get("gateway_mac") or (doc.get("network") or {}).get("gateway_mac")):
            continue
        if not (root / name).is_file():
            missing.append(name)
    if missing:
        return None, missing
    try:
        config = copy.deepcopy(settings.base()) or {"gw_mac": doc.get("gateway_mac"), "adopt": []}
    except ValueError:
        return None, ["bridge.yaml"]
    network = doc.get("network")
    if network:  # only once saved: an untouched section leaves today's config as is
        config["gw_mac"] = mac_hex(network["gateway_mac"]) or config["gw_mac"]
        config["pairing_key"] = network["pairing_key"]
        config["adopt"] = {"manual": [], "all": "all", "list": [mac_hex(m) for m in network["auto_pair_macs"].split(", ")]}[network["auto_pair"]]
        config["log"] = {**(config.get("log") or {}), "level": network["log_level"]}
        for name in ("invert_iq", "downlink_delay_us", "burst_spacing_us", "link_lost_timeout", "watchdog_timeout", "watchdog_short_challenge_k"):
            config[name] = network[name]
    config["radio"] = {"host": options.get("radio_host", ""), "username": options.get("radio_user", "pi"),
                       "port": options.get("radio_port", 22), "key_file": str(root / "radio_ed25519"),
                       "known_hosts": str(root / "known_hosts")}
    if tcp:
        config["radio"] = {"tcp": {k: radio[k] for k in ("host", "port", "token")}}
    elif radio:
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
