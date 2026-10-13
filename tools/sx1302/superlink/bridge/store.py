"""Device registry persistence: interface + in-memory and JSON impls."""
from __future__ import annotations
import abc
import json
import os
import tempfile
import uuid
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
    name: str | None = None
    network_id: int | None = None


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

    def import_records(self, records: list[DeviceRecord]) -> None:
        """Atomically insert new devices, refusing existing identities."""
        raise NotImplementedError("This device store does not support imports")


class InMemoryDeviceStore(DeviceStore):
    def __init__(self):
        self._records: dict[bytes, DeviceRecord] = {}

    def load_all(self) -> list[DeviceRecord]:
        return list(self._records.values())

    def save(self, record: DeviceRecord) -> None:
        self._records[record.mac] = record

    def delete(self, mac: bytes) -> None:
        self._records.pop(mac, None)

    def import_records(self, records: list[DeviceRecord]) -> None:
        _check_new(records, self._records)
        self._records.update({r.mac: r for r in records})


def _check_new(records, existing):
    macs = [r.mac for r in records]
    if len(macs) != len(set(macs)) or any(mac in existing for mac in macs):
        raise ValueError("Import contains duplicate or existing devices")


class JsonDeviceStore(DeviceStore):
    def __init__(self, path: str):
        self.path = path

    def _read(self) -> dict[str, dict]:
        if not os.path.exists(self.path):
            return {}
        with open(self.path) as f:
            return json.load(f)

    def _write(self, data: dict[str, dict]) -> None:
        self._atomic_write(self.path, data)

    @staticmethod
    def _atomic_write(path, data):
        directory = os.path.dirname(os.path.abspath(path))
        fd, temporary = tempfile.mkstemp(prefix=".superlink-", dir=directory)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load_all(self) -> list[DeviceRecord]:
        return [_from_json(v) for v in self._read().values()]

    def save(self, record: DeviceRecord) -> None:
        data = self._read()
        data[record.mac.hex()] = _to_json(record)
        self._write(data)

    def delete(self, mac: bytes) -> None:
        data = self._read()
        data.pop(mac.hex(), None)
        self._write(data)

    def import_records(self, records: list[DeviceRecord]) -> None:
        data = self._read()
        _check_new(records, {bytes.fromhex(mac) for mac in data})
        if os.path.exists(self.path):
            self._atomic_write(f"{self.path}.backup-{uuid.uuid4().hex}", data)
        data.update({r.mac.hex(): _to_json(r) for r in records})
        self._write(data)
