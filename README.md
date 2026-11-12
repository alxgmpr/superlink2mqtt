# superlink2mqtt

Ubiquiti SuperLink sensors → MQTT → Home Assistant, with no UniFi controller or UniFi gateway.

SuperLink is Ubiquiti's proprietary LoRa-based sub-GHz sensor protocol. This project is an open gateway for it: a Raspberry Pi with an SX1302 LoRa concentrator pairs with the sensors directly over the air, decrypts their traffic, and publishes it to MQTT with Home Assistant discovery. Sensors show up in Home Assistant as normal MQTT devices.

<img width="1028" height="781" alt="superlink2mqtt management UI" src="https://github.com/user-attachments/assets/e16ffbb9-7369-461f-b2e1-0df24a7cf506" />

```text
SuperLink sensors ⇄ Pi + SX1302 ⇄ superlink2mqtt → MQTT broker → Home Assistant
                                        └ web UI (pairing, status, commands)
```

## What you get

- **Pairing** from a web UI. Sensors get fresh keys; nothing is needed from a UniFi console.
- **Entities**: door, motion, leak, tamper, glass break, smoke, temperature, humidity, ambient light, battery (% and volts), signal, button-press events, LED switch.
- **Commands**: locate, refresh, reboot, clear tamper, factory reset / unpair.
- **Recovery**: keys persist across restarts; radio and broker outages retry in the background.

Status: door, tamper, battery, signal and button are verified against real hardware. The other entity types are mapped from the firmware's property table and have not been tested on a physical sensor. US 915 MHz channel plan only.

## Try it without hardware

```bash
python3 -m venv .venv && .venv/bin/pip install .
```

```bash
.venv/bin/superlink-bridged --demo
```

Open <http://127.0.0.1:8099> and enter the token printed in the terminal. Sample readings only; pairing and commands are disabled. Needs Python 3.12+ and libsodium (`apt install libsodium-dev` / `brew install libsodium`).

## What you need

| | |
| --- | --- |
| Radio | Raspberry Pi + SX1302 CoreCell concentrator (US 915 MHz), SPI enabled |
| HAL | [Semtech `sx1302_hal`](https://github.com/Lora-net/sx1302_hal) cloned to `~/sx1302_hal` on the Pi |
| Python | 3.12+ (Raspberry Pi OS Trixie; Bookworm ships 3.11) |
| MQTT | A broker, plus Home Assistant's MQTT integration connected to it |
| Sensors | SuperLink sensors, factory-reset if they were previously adopted by UniFi |

An SX1262 board (e.g. Heltec V3) can sniff but cannot act as the gateway.

## Set up the Pi

On the Pi, with `sx1302_hal` already cloned to `~/sx1302_hal`:

```bash
git clone https://github.com/alxgmpr/superlink2mqtt.git && cd superlink2mqtt
```

```bash
tools/sx1302/setup_rpi.sh
```

This installs libsodium, creates `~/superlink-venv` with the package installed, and builds `libloragw.so`.

Then pick one of the two ways to run the bridge.

## Option A — run the bridge on the Pi

Simplest. The Pi does everything and talks to your broker over the network.

```bash
cp tools/sx1302/superlink_bridge.yaml.example ~/superlink_bridge.yaml
```

Edit `~/superlink_bridge.yaml`:

```yaml
gw_mac: "020000000001"     # pick any unique 6-byte MAC and never change it
adopt: []                  # [] = pair individually from the UI
store_path: "superlink_devices.json"

web:
  host: "127.0.0.1"
  port: 8099
  token_env: "SUPERLINK_WEB_TOKEN"

mqtt:
  host: "192.168.1.10"     # your broker (the HA host if you use the Mosquitto app)
  port: 1883
  username: "..."
  password: "..."
  base_topic: "superlink"
  discovery_prefix: "homeassistant"
```

Start it:

```bash
export SUPERLINK_WEB_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"; echo "$SUPERLINK_WEB_TOKEN"
```

```bash
~/superlink-venv/bin/superlink-bridged --config ~/superlink_bridge.yaml
```

The UI listens on localhost only. Reach it from your machine with a tunnel, then open <http://127.0.0.1:8099>:

```bash
ssh -L 8099:127.0.0.1:8099 your-user@your-pi
```

## Option B — run the bridge as a Home Assistant app

The bridge runs in a container on Home Assistant OS, with its UI in the HA sidebar and MQTT credentials supplied by Supervisor. The Pi only serves the radio over a restricted SSH key.

1. On your checkout, stage the app and copy it to Home Assistant:

   ```bash
   python3 tools/homeassistant/stage.py /tmp/superlink2mqtt-app
   ```

   Copy that directory to `/addons/local/superlink2mqtt` on Home Assistant, reload the app store, and install **superlink2mqtt**.

2. On the Pi, create a wrapper script (e.g. `~/superlink-radio/run`, `chmod +x`) that starts the radio server and prints nothing else:

   ```bash
   #!/bin/sh
   exec "$HOME/superlink-venv/bin/python" -m superlink.radio_server
   ```

3. Start the app and open it from the sidebar. In the Radio form enter the Pi's address, user, port and host key (`cat /etc/ssh/ssh_host_ed25519_key.pub` on the Pi). Save, then copy the app's public key from **Authorize this app on your Pi**.

4. Add that key to `~/.ssh/authorized_keys` on the Pi, locked to the wrapper:

   ```text
   restrict,command="/home/your-user/superlink-radio/run" ssh-ed25519 AAAA...
   ```

5. In the MQTT form choose the Home Assistant broker (or enter a custom one). **Gateway** should show *Radio connection: connected* and *MQTT: connected*.

Full walkthrough, manual provisioning and troubleshooting: [homeassistant/superlink2mqtt/DOCS.md](homeassistant/superlink2mqtt/DOCS.md).

## Pair a sensor

1. Factory-reset the sensor if it was ever adopted by UniFi, and put it in pairing mode (see its manual).
2. In the UI open **Devices**, select the discovered sensor, press **Pair**.
3. Wait for the first reading. The device then appears under Home Assistant **Settings → Devices & services → MQTT**.
4. Trigger the sensor physically and confirm the entity changes.

Things to know:

- Only one process may own the SX1302, and only one controller may own a sensor. Stop any sniffer, and never run two bridges against the same sensors.
- `gw_mac` and the device store (`superlink_devices.json` + `.bak`) hold the gateway identity and sensor keys. Back them up privately; losing them means re-pairing.
- Sensors sleep. After a bridge restart a sensor may need a button press before readings resume.

## Docs

- [Home Assistant app setup + troubleshooting](homeassistant/superlink2mqtt/DOCS.md)
- [Management UI, pairing, API](docs/MANAGEMENT_UI.md)
- [MQTT behavior + device store recovery](docs/OPERATIONS.md)
- [Protocol reference](docs/protocol/frame_format.md) — frame format, crypto, pairing, application layer
- [Status / roadmap](docs/OPEN_GATEWAY_PLAN.md)
- [Contributing + tests](CONTRIBUTING.md)

## Repository layout

| Path | Contents |
| --- | --- |
| `tools/sx1302/superlink/` | The Python package: radio HAL, decoder, crypto, bridge daemon, MQTT, web UI |
| `homeassistant/superlink2mqtt/` | Home Assistant app definition and setup guide |
| `docs/protocol/` | Reverse-engineered protocol specs |
| `tests/` | pytest suite (`pip install -e '.[mqtt,test]' && pytest`) |
| `tools/sniffer/`, `tools/sdr/`, `tools/keyhook/`, … | Research tooling used to reverse the protocol; not needed to run the bridge |
| `verification/lean/` | Lean proofs for parts of the protocol model |

## Legal

Independent interoperability research; not affiliated with or endorsed by Ubiquiti. No Ubiquiti firmware is distributed here. MIT licensed.
