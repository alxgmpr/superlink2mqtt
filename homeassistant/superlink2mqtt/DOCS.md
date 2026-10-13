# superlink2mqtt

The app owns sensor keys, sessions, MQTT discovery, and the UI. The Pi only
forwards encrypted radio packets and schedules transmissions on its SX1302.

Open the UI from Home Assistant. Ingress handles login; no LAN web port is exposed.
The app uses the existing Mosquitto service automatically.

## Radio setup

Install `superlink/__init__.py`, `hal.py`, and `radio_server.py` on the Pi, keeping
its existing `~/sx1302_hal` library and reset script. No sensor keys or MQTT
credentials are needed by the radio process.

Use a dedicated SSH key and a pinned `known_hosts` file. Restrict its Pi
`authorized_keys` entry to the fixed command:

```
restrict,command="/usr/bin/python3 -m superlink.radio_server" ssh-ed25519 ...
```

Install the package on that command's Python path, or use a fixed wrapper that
sets `PYTHONPATH`. Only one controller can hold the radio lock. Stop the old
gateway daemon before starting the app.

In `/addon_configs/local_superlink2mqtt/`, place:

- `radio_ed25519`: the dedicated private SSH key (mode 0600).
- `known_hosts`: the Pi SSH host key, verified through an existing trusted connection.
- `bridge.yaml`: gateway MAC, adoption allowlist, and optional timing settings.
- `devices.json`: the old gateway's device store, copied after clean shutdown.

Set `radio_host`, `radio_user`, and `radio_port` in the app's Configuration tab.
Keep the same gateway MAC, MQTT topics, and device store when moving an existing
gateway. Do not run both gateway daemons against the same sensors.

## Install from a checkout

Run `python3 tools/homeassistant/stage.py /tmp/superlink2mqtt-app` and copy that
directory to Home Assistant's `/addons/local/superlink2mqtt`. Reload the app
store, install the local app, configure the files above, and start it. Enable
Start on boot and Watchdog. Repeat staging and use Rebuild for updates.

SSH preserves SX1302 hardware timestamps. Use a reliable LAN: each downlink
must reach the Pi before its one-second receive window. A broken connection
fails closed; Supervisor restarts the app and sensors reconnect using their
persisted keys. Transmissions are never replayed after a timeout.

Configuration and ingress follow the [Home Assistant app documentation](https://developers.home-assistant.io/docs/apps/configuration/)
and [ingress guidance](https://developers.home-assistant.io/docs/apps/presentation/).
