# superlink2mqtt setup

superlink2mqtt is an independent SuperLink-to-MQTT bridge. Home Assistant is an
MQTT consumer; installing this app runs the bridge in its own container beside
Home Assistant Core. The Pi provides the SX1302 radio over SSH. Sensor keys,
sessions, MQTT discovery and the web UI live in the bridge container.

```text
SuperLink sensors ↔ Pi / SX1302 ↔ superlink2mqtt → MQTT broker → Home Assistant
                                  ↑ Ingress UI
```

## Before starting

- A Pi with a working SX1302 CoreCell, the Semtech HAL, and reliable LAN access
  from Home Assistant. An SX1262 sniffer is not a substitute for this backend.
- A Supervisor MQTT service (for example, the Mosquitto broker app), configured
  and running. The bridge obtains its service credentials automatically.
- Home Assistant's MQTT integration connected to that same broker. Installing a
  broker or this app alone does not configure Home Assistant's MQTT integration.
- For an existing superlink2mqtt installation, preserve its gateway identity,
  device store and MQTT topic prefixes when moving or updating the bridge.

## Install and provision the radio

1. From the checkout, run
   `python3 tools/homeassistant/stage.py /tmp/superlink2mqtt-app` using a new
   destination directory. Copy it to `/addons/local/superlink2mqtt` on Home
   Assistant. Reload the app store and install the local app.
2. On the Pi, install the `superlink` package or the radio-only files
   `superlink/__init__.py`, `hal.py`, and `radio_server.py` on Python's module
   path. Keep the working HAL at `~/sx1302_hal`, including its reset script.
3. Create a dedicated SSH key for the app. Pin the Pi's SSH host key in
   `known_hosts`, verifying it through an existing trusted connection. Do not
   disable host-key checking. The Pi account needs access to its radio hardware.
4. Restrict that key in the Pi account's `authorized_keys` to a fixed wrapper:

   ```text
   restrict,command="/home/alex/superlink-radio/run" ssh-ed25519 ...
   ```

   Substitute your account and path. The wrapper should set any required
   `PYTHONPATH`, then `exec /usr/bin/python3 -m superlink.radio_server`. It must
   not write startup text to stdout, which carries the radio protocol. The
   restricted SSH key cannot be used as a normal interactive shell.
5. Allow Home Assistant to reach this Pi on its configured SSH port. Stop any
   previous sniffer/gateway process using the SX1302; only one process may own it.

## Configure persistent storage

Place these files in `/addon_configs/local_superlink2mqtt/` on Home Assistant
(the app sees that directory as `/config`):

| File | Purpose |
| --- | --- |
| `radio_ed25519` | Dedicated private SSH key, mode 0600 |
| `known_hosts` | Verified Pi SSH host key; include `[host]:port` for a non-default port |
| `bridge.yaml` | Stable gateway identity and optional bridge settings |
| `devices.json` | Existing bridge store after clean shutdown, or omit for a new installation |

Minimal **new-installation example** for `bridge.yaml`:

```yaml
gw_mac: "020000000001"
adopt: []
mqtt:
  base_topic: superlink
  discovery_prefix: homeassistant
```

Choose a unique, stable six-byte gateway MAC. For an existing installation, use
its original value, not this example. `adopt: []` enables individual pairing from
the UI; an explicit list of sensor MACs automatically adopts those sensors.
Avoid `adopt: all` unless intentionally enrolling every discovered sensor.

Do not create an empty `devices.json` file. Omit it for a new installation so the
bridge can create its valid store. New sensors get fresh keys during pairing;
those keys are saved for reconnects and restarts.

Set `radio_host`, `radio_user`, and `radio_port` in the app's **Configuration**
tab. Start the app, then enable **Start on boot** and **Watchdog**. Updates use
the same staging procedure followed by **Rebuild**. Keep all private files out
of the build directory and Git. Back up the app configuration before updates.

If radio files or MQTT service details are missing, the app opens its setup UI
and checks again every five seconds. It does not generate a replacement gateway
identity or change saved sensor keys. Provision the listed files and app options;
the bridge starts when configuration is ready. Invalid stores require recovery.
A configured but unreachable radio retries without closing the UI.

## Open the UI and check readiness

Open **superlink2mqtt** in the Home Assistant sidebar or **Open web UI**. Ingress
handles authentication; no Home Assistant long-lived access token is needed and
no app web port needs to be published on the LAN. If Ingress expires, reopen the
sidebar panel. A standalone installation uses its own `SUPERLINK_WEB_TOKEN`.

In **Gateway**, check **Radio connection: connected** and **MQTT: connected**.
These are independent checks. MQTT connectivity alone does not prove the Pi or
sensor link works. With no paired sensors, a four-step modal opens automatically:
radio, MQTT, pairing and a first reading. Each step shows a diagram and live
status. Pair a discovered sensor inside the modal; completion requires persisted
adoption and a decoded reading. Close it at any time and use **Get started** in
the empty Devices view to resume. Saved sensors suppress automatic onboarding.

## Pair sensors with fresh keys

New sensors and sensors previously managed by UniFi use the same pairing flow.
For an existing UniFi sensor, factory-reset it according to its model's manual
to clear the old pairing, then pair it here. No credentials for the original
UniFi gateway are needed. The Pi radio still uses its dedicated SSH key.

1. Put a new/unpaired sensor in pairing mode according to its model's manual.
   Factory-reset an existing sensor only when you intend to discard its pairing.
2. Open **Devices**, select the discovered sensor, and press **Pair**. With a
   configured MAC allowlist, matching sensors pair automatically.
3. Wait for pairing and fresh readings. Commands remain unavailable until pairing
   completes. Open **Activity** to see state changes and sensor responses.
4. Find the device under Home Assistant **Settings → Devices & services → MQTT**.
5. Trigger a physical event and confirm both a fresh bridge reading and the
   corresponding Home Assistant entity change.

## Troubleshooting and recovery

| Symptom | Check |
| --- | --- |
| Radio disconnected / connecting | Pi power, IP address, network path from HA, SSH port, pinned host key, dedicated key and the single radio owner |
| MQTT disconnected | Broker app and Supervisor MQTT service; for standalone use, broker address, credentials and TLS |
| 502 / bridge unavailable | App logs and configuration; restart or rebuild status; retry after startup |
| Access denied | Reopen the HA sidebar panel; for standalone, use the bridge token, not an HA token |
| No discovered sensor | Pairing mode, radio readiness, compatible hardware/channel plan, antenna and range |
| Awaiting reconnect | Wake sensor, check gateway identity and keys, and stop the old controller; do not repeatedly reset |
| No Home Assistant entities | Same broker, MQTT integration enabled, matching discovery prefix, and actual sensor telemetry |
| No search results | Clear the search; it matches device name and MAC |

During a radio outage the app stays up, reports the fault, marks existing devices
offline, and retries with a 1–30 second backoff. Persistent keys survive; volatile
sessions and pending commands are discarded. Transmissions are never replayed
after a timeout because the sensor might already have executed them. Check the
sensor's state before sending another command. Reconnection starts a fresh radio
handshake; the sensor may need to wake before readings resume.

A broker outage also leaves the UI available. MQTT reconnects in the background.
**Queued** means accepted for delivery, not confirmed by the sensor. Activity is
bounded to 100 entries since startup. Diagnostics exclude credentials and keys
but contain device identifiers and readings.

Back up `/config`, including the store and gateway identity. To restore, stop the
bridge, preserve the current files, restore the chosen backup and start only the
intended controller. Never run two controllers against the same sensors.

## Reference practices

The separation of bridge, broker and consumer and the setup checks follow
[Zigbee2MQTT's getting-started flow](https://www.zigbee2mqtt.io/guide/getting-started/)
and [Home Assistant integration guidance](https://www.zigbee2mqtt.io/guide/usage/integrations/home_assistant.html).
Authentication and app storage follow
[Home Assistant app presentation](https://developers.home-assistant.io/docs/apps/presentation/)
and [configuration](https://developers.home-assistant.io/docs/apps/configuration/).
Z-Wave JS UI also separates radio management from consumers; its preferred Home
Assistant path uses the Z-Wave JS integration/WebSocket server, while this
bridge's consumer interface remains MQTT.
