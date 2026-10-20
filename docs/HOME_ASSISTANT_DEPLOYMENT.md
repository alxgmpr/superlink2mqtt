# Home Assistant deployment — 2026-10-13

## Running installation

- Home Assistant: `10.1.3.4`, HAOS 18.3, amd64.
- App: `local_superlink2mqtt`, version `0.2.0`, protected mode, automatic boot,
  watchdog enabled. Local build source: `/addons/local/superlink2mqtt`.
- UI: Home Assistant sidebar → **superlink2mqtt** (admin access). Ingress-only;
  no web port published on the LAN. The panel URL is `/local_superlink2mqtt`.
- Persistent files: `/addon_configs/local_superlink2mqtt/` on Home Assistant,
  mounted as `/config` in the app. Device store and SSH key use mode 0600.
- MQTT: the existing Supervisor Mosquitto service, with generated service
  credentials. Existing `superlink` and `homeassistant` topic prefixes retained.
- Gateway MAC: `aabbccddee01`; sensor: `9041b22e9a53`.
- Pi: `10.1.10.80`, `/home/alex/superlink-radio/run`, invoked over SSH by the app.
  Only `python3 -m superlink.radio_server` runs there; the previous full gateway
  units are stopped. The installed HAL and reset script remain in `~/sx1302_hal`.

The dedicated SSH key is restricted to the radio wrapper and source
`10.1.3.4`. Host identity was pinned using the existing trusted SSH connection.
No shell, forwarding, or PTY is available to that key.

UniFi policy `bb3d3981-e476-4596-a4a8-47d4e9e82199`, named
**Allow Home Assistant to SuperLink radio SSH**, permits only IPv4 TCP
`10.1.3.4 → 10.1.10.80:22` plus established return traffic. It precedes the
existing SSH deny. Existing custom policy contents and relative order were
checked after the addition. Firewall snapshots are in the app config's
`migration/` directory.

## Verification

- 392 pytest cases passed; one Linux-native test skipped on macOS. Lean proofs
  and all 10,278 Python correspondence vectors passed.
- Container built and started on HAOS; MQTT and pinned SSH authentication passed.
- After the user woke the sensor, the HA-hosted daemon completed the radio
  handshake and decoded fresh telemetry. Existing HA entity IDs remained intact:
  battery 100%, battery voltage 3.186 V, entry detected, and changing RSSI.
- Pressing the existing HA **Refresh info** entity produced an MQTT command,
  timed radio downlink, device-info response, and persisted property metadata.
- Controlled app restart reopened the radio successfully. Terminating the Pi
  radio process caused the app to fail closed; Supervisor restarted it, and the
  sensor re-handshook and resumed telemetry without another physical wake.
- The HA sidebar panel registration was read back. Ingress authorization and
  bootstrap/API behavior passed HTTP tests. The actual HA browser session was
  logged out, so the authenticated panel was not visually verified.
- Partial HA backup `9bdd7834`, **superlink2mqtt migration 2026-10-13**, completed
  (62,126,080 bytes), including app version 0.2.0.

## Update and rollback

Use [the app setup guide](../homeassistant/superlink2mqtt/DOCS.md) to stage a new
build, copy it into the same local app directory, and rebuild through Supervisor.
Keep private `/addon_configs` files out of the build context and Git.

The previous Pi release remains at `/home/alex/superlink-releases/b58b1b4`, with
its config at `/home/alex/.config/superlink/bridge-b58b1b4.yaml` and original
device store at `/home/alex/.local/share/superlink/devices-b58b1b4.json`.
The initial migration snapshot is `devices-pre-ha-20261013.json` alongside it.

To roll back, stop the HA app first and verify its SSH radio process has exited.
Preserve the current HA store, then copy its device records to the Pi store,
filtering record fields against the old release's `DeviceRecord` dataclass
(the old release does not support the new `name` and `network_id` fields).
Start the old release's `start-bridge` under systemd as user/group `alex`, with
`KillSignal=SIGINT` and `UMask=0077`. Never run both controllers simultaneously.
For an immediate rollback before any key/inventory changes, the untouched Pi
store remains sufficient.

The narrow firewall rule can be removed by its ID if the HA app is retired.
Remove only the dedicated `superlink-ha-radio` authorized key from the Pi; the
original authorized-keys snapshot is `~/.ssh/authorized_keys.pre-superlink-ha-20261013`.
