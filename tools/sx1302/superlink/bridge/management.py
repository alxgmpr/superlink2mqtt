"""Device-level management interface, executed on the runtime owner thread."""
from collections import deque
import time

from .events import (AdoptDevice, CommandStatus, DeviceDiscovered, DeviceInfoEvent,
                     DeviceRemoved, DeviceStateEvent, LinkSignal, Locate,
                     PropertyEvent, Reboot, RequestDeviceInfo)
from .importing import parse_import, parse_mac


class Management:
    def __init__(self, runtime):
        self.runtime = runtime
        self.observed = {}
        self.activity = deque(maxlen=100)
        runtime.add_event_sink(self.on_event)

    def on_event(self, event):
        # Explicit projection: raw messages, cryptographic state and credentials
        # never reach the management response or browser.
        if isinstance(event, DeviceRemoved):
            self.observed.pop(event.mac, None)
            self.activity.appendleft({"time": time.time(), "mac": event.mac.hex(),
                                      "message": "Device removed"})
            return
        if not isinstance(event, (DeviceDiscovered, DeviceStateEvent, DeviceInfoEvent,
                                  PropertyEvent, LinkSignal, CommandStatus)):
            return
        item = self.observed.setdefault(event.mac, {"properties": {}})
        if isinstance(event, DeviceDiscovered):
            item["state"] = "discovered"
        elif isinstance(event, DeviceStateEvent):
            item["state"] = event.state
        elif isinstance(event, DeviceInfoEvent):
            item["firmware"] = ".".join(map(str, event.fw_version))
            item["device_type"] = event.device_type
        elif isinstance(event, PropertyEvent) and event.decoded:
            item["properties"][f"{event.name}:{event.channel}"] = {
                "name": event.name, "channel": event.channel,
                "value": event.value, "unit": event.unit}
        elif isinstance(event, LinkSignal):
            item["rssi"] = event.rssi_dbm
            item["snr"] = event.snr
        elif isinstance(event, CommandStatus):
            self.activity.appendleft({"time": time.time(), "mac": event.mac.hex(),
                "message": f"Sensor response: tag {event.message_tag}, status {event.status_code}"})
        if isinstance(event, (PropertyEvent, DeviceInfoEvent, LinkSignal)):
            item["last_seen"] = time.time()

    def snapshot(self):
        records = {r.mac: r for r in self.runtime.store.load_all()}
        devices = []
        for mac in sorted(records.keys() | self.observed.keys()):
            record = records.get(mac)
            seen = self.observed.get(mac, {})
            devices.append({
                "mac": mac.hex(), "name": (record.name if record else None) or f"SuperLink {mac.hex()[-6:]}",
                "adopted": bool(record and record.adopted),
                "state": seen.get("state", "awaiting_reconnect"),
                "last_seen": seen.get("last_seen"),
                "rssi": seen.get("rssi"), "snr": seen.get("snr"),
                "firmware": seen.get("firmware"),
                "properties": list(seen.get("properties", {}).values()),
            })
        mqtt = self.runtime.mqtt_bridge
        return {"demo": self.runtime.demo,
                "backend": "sx1302-ssh" if self.runtime.config.radio else "sx1302",
                "gateway_mac": self.runtime.config.gw_mac.hex(),
                "mqtt": "connected" if mqtt and mqtt.client.is_connected() else
                        ("disconnected" if self.runtime.config.mqtt else "disabled"),
                "devices": devices, "activity": list(self.activity)}

    def preview(self, document):
        records = parse_import(document)
        existing = {r.mac for r in self.runtime.store.load_all()}
        rows = [{"mac": r.mac.hex(), "name": r.name or f"SuperLink {r.mac.hex()[-6:]}",
                 "conflict": r.mac in existing or self.runtime.core.has_session(r.mac),
                 "network_id": r.network_id} for r in records]
        return {"devices": rows, "can_import": not any(r["conflict"] for r in rows)}

    def import_devices(self, body):
        if self.runtime.demo:
            raise ValueError("Demo mode cannot import devices")
        if body.get("confirmed") is not True:
            raise ValueError("Confirm that the previous gateway has stopped managing these sensors")
        records = parse_import(body.get("document"))
        self.runtime.core.import_devices(records)
        self.activity.appendleft({"time": time.time(), "mac": None,
                                  "message": f"Imported {len(records)} devices; awaiting reconnect"})
        return {"imported": len(records), "state": "awaiting_reconnect"}

    def command(self, mac_text, command):
        if self.runtime.demo:
            raise ValueError("Demo mode cannot send commands")
        mac = parse_mac(mac_text)
        if not isinstance(command, str):
            raise ValueError("command must be a string")
        if command == "adopt":
            if self.runtime.core.has_session(mac) or mac not in self.observed:
                raise ValueError("Choose a discovered device without an existing session")
            action = AdoptDevice(mac)
        else:
            commands = {"locate": Locate, "refresh": RequestDeviceInfo, "reboot": Reboot}
            if command not in commands:
                raise ValueError("Unsupported command")
            if not self.runtime.core.has_session(mac):
                raise ValueError("Device has no active session")
            action = commands[command](mac)
        self.runtime.core.submit(action)
        self.activity.appendleft({"time": time.time(), "mac": mac.hex(),
                                  "message": f"{command.capitalize()} queued; waiting for sensor"})
        return {"state": "queued"}
