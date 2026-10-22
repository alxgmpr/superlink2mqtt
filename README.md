# superlink2mqtt

Ubiquiti SuperLink sensors → MQTT + Home Assistant. No UniFi controller.

Pairs sensors directly over the air. Door, motion, leak, tamper, battery, signal, button presses; locate/reboot/refresh/clear-tamper commands.

<img width="1028" height="781" alt="image" src="https://github.com/user-attachments/assets/e16ffbb9-7369-461f-b2e1-0df24a7cf506" />

## Hardware

Raspberry Pi + SX1302 concentrator (US 915 MHz). Optional: Heltec V3 for sniffing.

## Run

```bash
python -m pip install '.[mqtt]'
cp tools/sx1302/superlink_bridge.yaml.example superlink_bridge.yaml   # set broker + sensors
superlink-bridged --config superlink_bridge.yaml
```

Try without hardware: `superlink-bridged --demo`

## Docs

- [Home Assistant setup](homeassistant/superlink2mqtt/DOCS.md)
- [Management UI + pairing](docs/MANAGEMENT_UI.md)
- [MQTT + device store](docs/OPERATIONS.md)
- [Protocol](docs/protocol/frame_format.md)
- [Status / roadmap](docs/OPEN_GATEWAY_PLAN.md)
- [Contributing + tests](CONTRIBUTING.md)

## Legal

Interoperability research. MIT.
