# tools/sx1302/superlink/bridge/mqtt.py
"""MQTT / Home Assistant adapter: publishes events, handles commands."""
from __future__ import annotations
import json
import logging
import re
import threading

from .config import MqttConfig
from .entities import (
    entity_for, discovery_config, button_discovery_config, COMMAND_BUTTONS,
    PRESS_ENTITY, PRESS_ENTITY_NAME,
)
from .events import (
    Event, PropertyEvent, DeviceInfoEvent, DeviceDiscovered, DeviceStateEvent,
    ButtonPressed, LinkSignal, SetProperty, SetPropertyRaw, AdoptDevice, Locate,
    Reboot, RequestDeviceInfo, FactoryReset, DeviceRemoved,
)

# Command-button name -> factory building the Action from a device MAC.
_BUTTON_ACTIONS = {
    "locate": lambda mac: Locate(mac=mac),
    "reboot": lambda mac: Reboot(mac=mac),
    "refresh": lambda mac: RequestDeviceInfo(mac=mac),
    # "Clear tamper" = PROPERTY_SET TAMPER_CLEAR(id22)=1 (mirrors the UniFi
    # dashboard action). Verbatim raw so it needs no profile type for id22.
    "clear_tamper": lambda mac: SetPropertyRaw(mac=mac, property_id=22,
                                               channel=0, raw=b"\x01"),
    # Unpairs the sensor. The bridge forgets the device once the sensor
    # confirms with a tag-matched status reply (BridgeCore._intercept).
    "factory_reset": lambda mac: FactoryReset(mac=mac),
}

log = logging.getLogger("superlink.mqtt")


class MqttBridge:
    def __init__(self, config: MqttConfig, runtime, client):
        self.config = config
        self.runtime = runtime
        self.client = client
        self.base = config.base_topic
        self.prefix = config.discovery_prefix
        self._discovery_done: set[str] = set()
        self._buttons_done: set[bytes] = set()   # macs with button discovery sent
        # mac -> every retained topic published for that device (discovery
        # configs and state). Retraction has to clear exactly these, so they
        # are recorded at publish time rather than reconstructed later.
        self._retained: dict[bytes, set[str]] = {}
        # Radio events and paho callbacks run on different threads. Serialize
        # replay with updates/removal so stale snapshots cannot resurrect state.
        self._lock = threading.RLock()
        self._connected = False
        self._stopping = False
        self._retained_payloads: dict[str, str] = {}
        for record in runtime.store.load_all():
            topic = f"{self.base}/{record.mac.hex()}/availability"
            self._retained.setdefault(record.mac, set()).add(topic)
            self._retained_payloads[topic] = "offline"
        runtime.add_event_sink(self.on_event)

    # --- lifecycle ---
    def start(self) -> None:
        avail = f"{self.base}/bridge/availability"
        self.client.will_set(avail, "offline", retain=True, qos=1)
        if self.config.username:
            self.client.username_pw_set(self.config.username, self.config.password)
        if self.config.tls:
            self.client.tls_set()
        self.client.on_message = self._paho_on_message
        self.client.on_connect = self._paho_on_connect
        self.client.on_disconnect = self._paho_on_disconnect
        self.client.on_connect_fail = self._paho_on_connect_fail
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.connect_async(self.config.host, self.config.port)
        self.client.loop_start()

    def _paho_on_connect(self, client, userdata, flags, reason_code,
                         properties=None):
        with self._lock:
            if self._stopping or reason_code != 0:
                log.warning("MQTT connection not accepted (rc=%s)", reason_code)
                return
            self._connected = True
            client.subscribe(f"{self.base}/+/+/set")
            client.subscribe(f"{self.base}/+/+/press")
            client.subscribe(f"{self.base}/adopt")
            # Rebuild a broker that lost its retained data, using the newest
            # value of each topic. Non-retained button events are never replayed.
            for topic, payload in self._retained_payloads.items():
                self._publish_retained(topic, payload)
            self._publish_retained(f"{self.base}/bridge/availability", "online")
            log.info("MQTT connected; restored subscriptions and retained state")

    def _paho_on_disconnect(self, client, userdata, disconnect_flags,
                            reason_code, properties=None):
        with self._lock:
            self._connected = False
        if not self._stopping:
            log.warning("MQTT disconnected (rc=%s); awaiting reconnect", reason_code)

    def _paho_on_connect_fail(self, client, userdata):
        log.warning("MQTT broker unavailable; retrying in background")

    def stop(self) -> None:
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            info = (self._publish_retained(f"{self.base}/bridge/availability", "offline", qos=1)
                    if self._connected else None)
        try:
            if info is not None:
                info.wait_for_publish(timeout=2.0)
                if not info.is_published():
                    log.warning("MQTT offline publish was not confirmed before shutdown")
        except (RuntimeError, ValueError):
            log.warning("MQTT disconnected before offline publish was confirmed")
        finally:
            self.client.disconnect()
            self.client.loop_stop()

    # --- outbound: events -> MQTT ---
    def on_event(self, event: Event) -> None:
        with self._lock:
            self._on_event(event)

    def _on_event(self, event: Event) -> None:
        if isinstance(event, PropertyEvent):
            self._publish_property(event)
        elif isinstance(event, ButtonPressed):
            self._publish_button_press(event)
        elif isinstance(event, LinkSignal):
            self._publish_link_signal(event)
        elif isinstance(event, DeviceDiscovered):
            self._pub_retained(event.mac,
                               f"{self.base}/discovered/{event.mac.hex()}",
                               json.dumps({"channel": event.channel,
                                           "first_seen": event.first_seen}))
        elif isinstance(event, DeviceStateEvent):
            state = "online" if event.state in ("adopted", "active") else "offline"
            if event.state in ("adopted", "active"):
                self._publish_buttons(event.mac)
            self._pub_retained(event.mac,
                               f"{self.base}/{event.mac.hex()}/availability",
                               state)
        elif isinstance(event, DeviceRemoved):
            self._retract_device(event.mac)

    def _pub_retained(self, mac: bytes, topic: str, payload) -> None:
        """Publish a retained, device-scoped topic and remember it so
        _retract_device can clear it later."""
        self._retained.setdefault(mac, set()).add(topic)
        self._retained_payloads[topic] = payload
        if self._connected:
            self._publish_retained(topic, payload)

    def _publish_retained(self, topic: str, payload, qos: int = 0):
        # Coalesce retained snapshots ourselves. QoS 1 telemetry can be replayed
        # by paho AFTER on_connect and overwrite our newer snapshot with an old
        # unacknowledged value. Only the final offline publish needs a PUBACK.
        info = self.client.publish(topic, payload, retain=True, qos=qos)
        if info.rc != 0:
            log.warning("MQTT publish not queued for %s (rc=%s)", topic, info.rc)
        return info

    def _retract_device(self, mac: bytes) -> None:
        """Clear every retained topic published for a device, so its HA
        entities disappear instead of lingering as stale retained configs."""
        topics = self._retained.pop(mac, set())
        for topic in sorted(topics):
            # Keep deletion markers through outages, too.
            self._retained_payloads[topic] = ""
            if self._connected:
                self._publish_retained(topic, "")
        # Drop the once-only guards so a later re-adopt republishes discovery.
        self._buttons_done.discard(mac)
        machex = mac.hex()
        for key in [k for k in self._discovery_done
                    if k.startswith(f"{machex}_")]:
            self._discovery_done.discard(key)
        if topics:
            log.info("retracted %d retained topics for %s", len(topics), machex)

    def _publish_buttons(self, mac: bytes) -> None:
        """Publish LOCATE/REBOOT/refresh button discovery once per device."""
        if mac in self._buttons_done:
            return
        self._buttons_done.add(mac)
        for name in COMMAND_BUTTONS:
            topic, payload = button_discovery_config(
                mac, name, self.base, self.prefix)
            self._pub_retained(mac, topic, json.dumps(payload))

    def _publish_link_signal(self, ev: LinkSignal) -> None:
        """Publish the gateway-measured RSSI (real dBm) as the SIGNAL sensor.
        Discovery once per device; state each frame."""
        machex = ev.mac.hex()
        key = f"{machex}_SIGNAL"
        if key not in self._discovery_done:
            entity = entity_for("SIGNAL")
            topic, payload = discovery_config(
                ev.mac, "SIGNAL", entity, self.base, self.prefix, unit="dBm")
            self._pub_retained(ev.mac, topic, json.dumps(payload))
            self._discovery_done.add(key)
        self._pub_retained(ev.mac, f"{self.base}/{machex}/SIGNAL",
                           str(int(round(ev.rssi_dbm))))

    def _publish_button_press(self, ev: ButtonPressed) -> None:
        """Fire an HA `event` (a press trigger HA timestamps). No fake "off":
        the sensor never reports a button release. Discovery once per device."""
        machex = ev.mac.hex()
        key = f"{machex}_{PRESS_ENTITY_NAME}"
        if key not in self._discovery_done:
            topic, payload = discovery_config(
                ev.mac, PRESS_ENTITY_NAME, PRESS_ENTITY, self.base, self.prefix,
                unit=None)
            self._pub_retained(ev.mac, topic, json.dumps(payload))
            self._discovery_done.add(key)
        if self._connected:
            self.client.publish(f"{self.base}/{machex}/{PRESS_ENTITY_NAME}",
                                json.dumps({"event_type": "press"}))

    def _publish_property(self, ev: PropertyEvent) -> None:
        machex = ev.mac.hex()
        entity = entity_for(ev.name)
        if entity is not None:
            key = f"{machex}_{ev.name}"
            if key not in self._discovery_done:
                topic, payload = discovery_config(ev.mac, ev.name, entity,
                                                  self.base, self.prefix, ev.unit)
                self._pub_retained(ev.mac, topic, json.dumps(payload))
                self._discovery_done.add(key)
        if ev.decoded and isinstance(ev.value, bool):
            value = "ON" if ev.value else "OFF"
        elif ev.decoded and ev.value is not None:
            value = str(ev.value)
        else:
            value = ev.raw.hex()
        self._pub_retained(ev.mac, f"{self.base}/{machex}/{ev.name}", value)

    # --- inbound: MQTT -> actions ---
    def _paho_on_message(self, client, userdata, msg):
        # A retained reset/reboot must never execute again on resubscription.
        if msg.retain:
            log.warning("ignoring retained command on %s", msg.topic)
            return
        try:
            payload = msg.payload.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("ignoring non-UTF-8 command on %s", msg.topic)
            return
        log.info("MQTT RX %s", msg.topic)
        self._on_message(msg.topic, payload)

    def _on_message(self, topic: str, payload: str) -> None:
        if not topic.startswith(f"{self.base}/"):
            return
        parts = topic[len(self.base) + 1:].split("/")
        if topic == f"{self.base}/adopt":
            try:
                mac = _parse_mac(payload.strip())
            except ValueError:
                log.warning("bad adopt payload: %r", payload)
                return
            self.runtime.submit_action(AdoptDevice(mac=mac))
            return
        # <base>/<machex>/<name>/{set,press}
        if len(parts) == 3:
            try:
                mac = _parse_mac(parts[0])
            except ValueError:
                return
            name, action = parts[1], parts[2]
            if action == "set":
                self.runtime.submit_action(SetProperty(
                    mac=mac, name_or_id=name, value=_parse_value(payload)))
            elif action == "press":
                if payload.strip() != "PRESS":
                    log.warning("ignoring invalid button payload on %s", topic)
                    return
                factory = _BUTTON_ACTIONS.get(name)
                if factory is None:
                    log.warning("unknown button %r", name)
                    return
                self.runtime.submit_action(factory(mac))


def _parse_mac(value: str) -> bytes:
    if not re.fullmatch(r"[0-9a-fA-F]{12}", value):
        raise ValueError("expected a 12-digit sensor MAC")
    return bytes.fromhex(value)


def _parse_value(payload: str):
    p = payload.strip()
    if p.upper() in ("ON", "TRUE", "1"):
        return True
    if p.upper() in ("OFF", "FALSE", "0"):
        return False
    try:
        return int(p)
    except ValueError:
        try:
            return float(p)
        except ValueError:
            return p
