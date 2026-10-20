"""Validated persistent-key imports. Session-key logs are not migration files."""
from __future__ import annotations

import re

from .store import DeviceRecord


def parse_mac(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"(?:[0-9a-fA-F]{12}|(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})", value):
        raise ValueError("MAC must contain six hexadecimal bytes")
    return bytes.fromhex(value.replace(":", ""))


def _key(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError(f"{field} must be a 32-byte hexadecimal persistent key")
    return bytes.fromhex(value)


def parse_import(document) -> list[DeviceRecord]:
    """Accept a versioned bundle or one complete Ubiquiti addDevice request.

    Never infer the latest key from an unordered capture: adoption may rotate
    keys several times. Exporters must select the current committed key pair.
    """
    if not isinstance(document, dict):
        raise ValueError("Expected a migration bundle or an addDevice request")
    envelope = document.get("envelope", document.get("env"))
    if (isinstance(envelope, dict) and envelope.get("type") == "request"
            and envelope.get("action") == "addDevice"):
        items = [document.get("payload", document.get("pay"))]
    elif document.get("method") == "addDevice":
        items = [document.get("params")]
    elif document.get("version") == 1 and type(document.get("version")) is int:
        items = document.get("devices")
    else:
        raise ValueError("Expected version 1 or method addDevice; session keys cannot be imported")
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError("Import must contain between 1 and 100 devices")
    records = []
    seen = set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Device {index}: expected an object")
        try:
            mac = parse_mac(item.get("mac"))
            primary = _key(item.get("key"), "key")
            fallback = _key(item.get("fallbackKey"), "fallbackKey")
            name = item.get("name")
            if name is not None and (not isinstance(name, str) or not name.strip()
                                     or len(name) > 80 or any(ord(c) < 32 for c in name)):
                raise ValueError("name must be 1–80 printable characters")
            network_id = item.get("networkId")
            if network_id is not None and (type(network_id) is not int
                                           or not 1 <= network_id <= 0xffffffff):
                raise ValueError("networkId must be an integer between 1 and 4294967295")
            if mac in seen:
                raise ValueError("duplicate MAC; select a single current key pair per device")
            seen.add(mac)
            records.append(DeviceRecord(
                mac=mac, primary_key=primary, fallback_key=fallback,
                kdf_context=primary, transport_key=fallback, adopted=True,
                name=name.strip() if name else None, network_id=network_id))
        except ValueError as exc:
            raise ValueError(f"Device {index}: {exc}") from None
    return records
