"""Device registry persistence: interface + in-memory and JSON impls."""
from __future__ import annotations
import abc
import json
import os
import tempfile
import threading
from pathlib import Path
from dataclasses import dataclass, asdict, fields


@dataclass
class DeviceRecord:
    mac: bytes
    device_type: int | None = None
    primary_key: bytes | None = None
    fallback_key: bytes | None = None
    kdf_context: bytes | None = None
    transport_key: bytes | None = None
    adopted: bool = False
    tx_seq_hi: int = 0
    tx_seq_lo: int = 0
    ul_counter_offset: int = 5
    last_seen: float = 0.0
    # propertyId -> valueSize, learned from a DEVICE_INFO_REPORT. Persisted so a
    # bridge restart keeps decoding PROPERTY_REPORTs into typed events without
    # having to re-request device info on the next reconnect.
    prop_sizes: dict | None = None


_BYTES_FIELDS = ("mac", "primary_key", "fallback_key", "kdf_context", "transport_key")


def _to_json(rec: DeviceRecord) -> dict:
    d = asdict(rec)
    for k in _BYTES_FIELDS:
        d[k] = d[k].hex() if d[k] is not None else None
    return d


def _from_json(d: dict) -> DeviceRecord:
    allowed = {f.name for f in fields(DeviceRecord)}
    d = {k: v for k, v in d.items() if k in allowed}
    for k in _BYTES_FIELDS:
        if d.get(k) is not None:
            d[k] = bytes.fromhex(d[k])
    # JSON object keys are always strings; prop_sizes keys are integer
    # propertyIds, so restore them to int on load.
    if d.get("prop_sizes") is not None:
        d["prop_sizes"] = {int(k): v for k, v in d["prop_sizes"].items()}
    return DeviceRecord(**d)


class DeviceStore(abc.ABC):
    @abc.abstractmethod
    def load_all(self) -> list[DeviceRecord]: ...
    @abc.abstractmethod
    def save(self, record: DeviceRecord) -> None: ...
    @abc.abstractmethod
    def delete(self, mac: bytes) -> None: ...


class InMemoryDeviceStore(DeviceStore):
    def __init__(self):
        self._records: dict[bytes, DeviceRecord] = {}

    def load_all(self) -> list[DeviceRecord]:
        return list(self._records.values())

    def save(self, record: DeviceRecord) -> None:
        self._records[record.mac] = record

    def delete(self, mac: bytes) -> None:
        self._records.pop(mac, None)


class StoreError(ValueError):
    """Stored device keys cannot be loaded safely."""


def _validate_records(data: dict) -> None:
    if not isinstance(data, dict):
        raise ValueError("expected a device map")
    for mac, value in data.items():
        record = _from_json(value)
        if len(record.mac) != 6 or record.mac.hex() != mac:
            raise ValueError("invalid device identity")
        for field in _BYTES_FIELDS[1:]:
            key = getattr(record, field)
            if key is not None and len(key) != 32:
                raise ValueError("invalid key length")


class JsonDeviceStore(DeviceStore):
    """Atomic local store. One daemon owns the file; its threads share this instance.

    The .bak file is the previous validated snapshot. Recovery is explicit:
    automatically restoring it could resurrect a removed device or stale keys.
    """
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.backup_path = self.path + ".bak"
        self._lock = threading.RLock()

    def _read(self) -> dict[str, dict]:
        try:
            with open(self.path) as f:
                data = json.load(f)
            _validate_records(data)
            return data
        except FileNotFoundError:
            if os.path.exists(self.backup_path):
                raise StoreError(f"device store {self.path} missing; inspect backup "
                                 f"{self.backup_path} before restoring") from None
            return {}
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise StoreError(f"invalid device store {self.path}; preserve it and "
                             f"inspect backup {self.backup_path} before restoring") from exc

    @staticmethod
    def _atomic_write(path: str, data: dict) -> None:
        directory = os.path.dirname(path)
        temporary = None
        try:
            # NamedTemporaryFile creates the key-bearing file with mode 0600.
            with tempfile.NamedTemporaryFile(mode="w", dir=directory,
                                             prefix=".superlink-", delete=False) as f:
                temporary = f.name
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
            # Persist the rename as well as the contents on POSIX filesystems.
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def _write(self, data: dict[str, dict], previous: dict) -> None:
        _validate_records(data)
        if os.path.exists(self.path):
            self._atomic_write(self.backup_path, previous)
        self._atomic_write(self.path, data)

    def load_all(self) -> list[DeviceRecord]:
        with self._lock:
            return [_from_json(v) for v in self._read().values()]

    def save(self, record: DeviceRecord) -> None:
        with self._lock:
            previous = self._read()
            data = dict(previous)
            data[record.mac.hex()] = _to_json(record)
            self._write(data, previous)

    def delete(self, mac: bytes) -> None:
        with self._lock:
            previous = self._read()
            data = dict(previous)
            data.pop(mac.hex(), None)
            self._write(data, previous)
