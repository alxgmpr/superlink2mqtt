"""BridgeCore: pure multi-device orchestrator over DeviceSessions."""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Callable, Protocol

from ..decoder import parse_frame
from .events import (
    Event, Action, AdoptDevice, DeviceDiscovered, DeviceStateEvent,
    CommandStatus, DeviceRemoved, FactoryReset, Locate, StopLocate,
    PropertyEvent, Reboot, SetPropertyRaw,
)
from .mapping import action_to_body
from .profiles import ProfileRegistry
from .store import DeviceStore, DeviceRecord

log = logging.getLogger("superlink.core")

# One LOCATE flashes the LED for about a minute, then the sensor stops by
# itself (measured on usl-entry fw 1.2.0; Protect's UI says 30 s).
LOCATE_SECONDS = 60
# Commands only reach a sensor right after one of its uplinks. While locating,
# the bridge drops REPORT_INTERVAL to this so a Stop lands within seconds
# instead of waiting out the normal 300 s cadence.
LOCATE_POLL_S = 5
# A queued LOCATE waits for the sensor's next uplink, up to a full report
# interval away. Give up holding the locate state if it never confirms.
LOCATE_DELIVERY_S = 900
REPORT_INTERVAL_ID = 13
DEFAULT_REPORT_INTERVAL_S = 300


@dataclass
class OutgoingFrame:
    data: bytes
    freq_hz: int
    channel: int


class SessionProtocol(Protocol):
    mac: bytes
    state: str
    def feed(self, frame, channel: int, now: float,
             rssi: float | None = ..., snr: float | None = ...): ...
    def tick(self, now: float): ...
    def queue_body(self, body: bytes) -> None: ...


class InvalidCommand(ValueError):
    """An operator action cannot be encoded for the sensor."""


class BridgeCore:
    def __init__(self, store: DeviceStore, profiles: ProfileRegistry,
                 session_factory: Callable[[DeviceRecord], SessionProtocol],
                 auto_adopt: bool = False):
        self.store = store
        self.profiles = profiles
        self._factory = session_factory
        self.auto_adopt = auto_adopt
        self._sessions: dict[bytes, SessionProtocol] = {}
        self._discovered: dict[bytes, float] = {}
        self._subscribers: list[Callable[[Event], None]] = []
        # Running command messageTag. The sensor REJECTS tag 0 (a tag-0
        # FACTORY_RESET is silently ignored — no 0x74 ACK, no reset; verified
        # on hardware 2026-07-25). Real controllers use an incrementing non-zero
        # tag which the sensor echoes in its status reply. Start at 0 so the
        # first command uses tag 1.
        self._cmd_tag = 0
        # mac -> messageTag of a FACTORY_RESET awaiting its status reply. The
        # sensor answers in a later window, so the teardown is event-driven:
        # only a tag-matched status with code 0 removes the device.
        self._pending_reset: dict[bytes, int] = {}
        # mac -> time the sensor's own locate timeout ends; present only while
        # the bridge holds that sensor on the fast report interval.
        self._locating: dict[bytes, float] = {}
        # mac -> messageTag of a LOCATE the sensor has not confirmed yet. Its
        # status reply is when the flashing (and the 60 s clock) starts.
        self._locate_tag: dict[bytes, int] = {}
        self._last_uptime: dict[bytes, int] = {}
        self._now = 0.0
        for record in store.load_all():
            self._sessions[record.mac] = session_factory(record)

    def subscribe(self, cb: Callable[[Event], None]) -> None:
        self._subscribers.append(cb)

    def import_devices(self, records: list[DeviceRecord]) -> None:
        """Called only by the runtime owner, never by an HTTP worker."""
        if any(r.mac in self._sessions for r in records):
            raise ValueError("A device already has a session; existing devices cannot be overwritten")
        self.store.import_records(records)
        for record in records:
            self._discovered.pop(record.mac, None)
            self._sessions[record.mac] = self._factory(record)

    def has_session(self, mac: bytes) -> bool:
        return mac in self._sessions

    def _emit(self, events) -> None:
        for ev in events:
            for cb in self._subscribers:
                cb(ev)

    def feed(self, raw: bytes, channel: int, now: float,
             rssi: float | None = None, snr: float | None = None
             ) -> list[OutgoingFrame]:
        self._now = now
        frame = parse_frame(raw)
        if frame is None:
            return []
        mac = frame.mac
        session = self._sessions.get(mac)
        if session is not None:
            frames, events = session.feed(frame, channel, now, rssi=rssi, snr=snr)
            # _emit before _intercept: subscribers must see the CommandStatus
            # (and any event sharing its batch, e.g. a LinkSignal) before the
            # DeviceRemoved it triggers. Otherwise MqttBridge retracts the
            # device's topics and clears its guards first, then republishes
            # discovery/state for the very LinkSignal that arrives "after" —
            # a ghost entity nothing will ever retract.
            self._emit(events)
            self._intercept(events)
            self._watch_report_interval(events)
            return list(frames)
        if mac in self._discovered:
            return []  # already announced; awaiting AdoptDevice / auto_adopt
        self._discovered[mac] = now
        self._emit([DeviceDiscovered(mac=mac, channel=channel, first_seen=now),
                    DeviceStateEvent(mac=mac, state="discovered")])
        if self.auto_adopt:
            self._adopt(mac)
            session = self._sessions[mac]
            frames, events = session.feed(frame, channel, now, rssi=rssi, snr=snr)
            self._emit(events)
            return list(frames)
        return []

    def _adopt(self, mac: bytes) -> None:
        self._discovered.pop(mac, None)
        record = DeviceRecord(mac=mac)
        self._sessions[mac] = self._factory(record)
        self._emit([DeviceStateEvent(mac=mac, state="adopting")])

    def _intercept(self, events) -> None:
        """Act on events before they reach subscribers.

        A FACTORY_RESET is confirmed by a REQUEST_STATUS_RESPONSE echoing the
        command's messageTag with status 0 — the same signal the real
        controller waits for before issuing removeDevice. Anything else (wrong
        tag, non-zero status) leaves the record alone: a stale record is
        recoverable, a wrongly-deleted one needs a physical re-pair.
        """
        for ev in events:
            if not isinstance(ev, CommandStatus):
                continue
            pending = self._pending_reset.get(ev.mac)
            if pending is None or ev.message_tag != pending:
                continue
            if ev.status_code != 0:
                log.warning("factory reset on %s failed: status=%d "
                            "(keeping device record)",
                            ev.mac.hex(), ev.status_code)
                self._pending_reset.pop(ev.mac, None)
                continue
            self._remove_device(ev.mac, "factory_reset")

    def _remove_device(self, mac: bytes, reason: str) -> None:
        """Forget a device entirely: session and stored record."""
        self._sessions.pop(mac, None)
        self._discovered.pop(mac, None)
        self._pending_reset.pop(mac, None)
        self.store.delete(mac)
        log.info("removed device %s (%s)", mac.hex(), reason)
        self._emit([DeviceRemoved(mac=mac, reason=reason)])

    def _report_interval(self, mac: bytes, seconds: int) -> SetPropertyRaw:
        return SetPropertyRaw(mac=mac, property_id=REPORT_INTERVAL_ID,
                              channel=0, raw=seconds.to_bytes(2, "big"))

    def _normal_report_interval(self, mac: bytes) -> SetPropertyRaw:
        """The interval the bridge configured at adoption (profiles.yaml)."""
        for pid, ch, raw in self.profiles.post_adoption(None):
            if pid == REPORT_INTERVAL_ID:
                return SetPropertyRaw(mac=mac, property_id=pid, channel=ch, raw=raw)
        return self._report_interval(mac, DEFAULT_REPORT_INTERVAL_S)

    def _expand(self, action: Action) -> list[Action]:
        """Locate start/stop are several commands on the wire."""
        mac = action.mac
        if isinstance(action, Locate):
            self._locating[mac] = self._now + LOCATE_DELIVERY_S
            return [self._report_interval(mac, LOCATE_POLL_S), action]
        if isinstance(action, StopLocate):
            if mac not in self._locating:
                # Nothing to stop, and a reboot queued now would land minutes
                # later on the next uplink for no visible reason.
                log.info("%s is not locating; stop ignored", mac.hex())
                return []
            # The protocol has no stop message (Protect 7.4.96 sends LOCATE
            # once and lets it time out) and fw 1.2.0 rejects LED_PATTERN(61),
            # so the one thing known to end the flashing is a reboot. The
            # sensor reconnects by itself about 30 s later.
            del self._locating[mac]
            self._locate_tag.pop(mac, None)
            return [Reboot(mac=mac), self._normal_report_interval(mac)]
        return [action]

    def _watch_report_interval(self, events) -> None:
        """Put a sensor back on its normal interval if it reports at the
        locate rate while no locate is running (bridge restarted mid-locate,
        or the restore command was lost)."""
        for ev in events:
            if (isinstance(ev, CommandStatus)
                    and self._locate_tag.get(ev.mac) == ev.message_tag):
                del self._locate_tag[ev.mac]
                self._locating[ev.mac] = self._now + LOCATE_SECONDS
            if not (isinstance(ev, PropertyEvent) and ev.name == "UPTIME"
                    and isinstance(ev.value, int)):
                continue
            last = self._last_uptime.get(ev.mac)
            self._last_uptime[ev.mac] = ev.value
            if (last is not None and ev.mac not in self._locating
                    and 0 < ev.value - last <= 2 * LOCATE_POLL_S):
                log.info("%s still on the locate report interval; restoring",
                         ev.mac.hex())
                self.submit(self._normal_report_interval(ev.mac))

    def tick(self, now: float) -> list[OutgoingFrame]:
        self._now = now
        for mac, until in list(self._locating.items()):
            if now >= until:
                del self._locating[mac]
                self._locate_tag.pop(mac, None)
                self.submit(self._normal_report_interval(mac))
        out: list[OutgoingFrame] = []
        for mac, session in list(self._sessions.items()):
            frames, events = session.tick(now)
            out.extend(frames)
            self._emit(events)
            self._intercept(events)
        return out

    def submit(self, action: Action) -> None:
        if isinstance(action, AdoptDevice):
            if action.mac in self._discovered:
                self._adopt(action.mac)
            return
        session = self._sessions.get(action.mac)
        if session is None:
            return
        for step in self._expand(action):
            self._queue(session, step)
            if isinstance(step, Locate):
                self._locate_tag[step.mac] = self._cmd_tag

    def _queue(self, session, action: Action) -> None:
        tag = (self._cmd_tag % 0xFF) + 1  # 1..255, never 0
        try:
            body = action_to_body(action, self.profiles, tag=tag)
        except (KeyError, ValueError, TypeError, PermissionError, OverflowError) as exc:
            raise InvalidCommand(str(exc)) from exc
        self._cmd_tag = tag
        session.queue_body(body)
        # The tag space is shared across all devices and wraps at 255, so a
        # stale pending-reset tag could otherwise be "confirmed" by an
        # unrelated later command that happens to land on the same tag. Only
        # invalidate the pending reset when the new command actually reuses
        # its tag -- that's the one case an aliased status would wrongly
        # confirm it. A pending reset's status can take a full sensor window
        # (seconds to minutes) to arrive, so invalidating on *every* later
        # submit (a second button press, a Locate/Refresh) would silently
        # disarm a reset the sensor still goes on to perform.
        if self._pending_reset.get(action.mac) == self._cmd_tag:
            log.warning("pending factory reset on %s disarmed: tag %d reused "
                        "by a later command before its status arrived",
                        action.mac.hex(), self._cmd_tag)
            self._pending_reset.pop(action.mac)
        if isinstance(action, FactoryReset):
            self._pending_reset[action.mac] = self._cmd_tag
