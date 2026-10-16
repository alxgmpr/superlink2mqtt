# superlink2mqtt

**An open gateway that puts Ubiquiti SuperLink sensors on MQTT** — pair them
directly, publish their state, and get Home Assistant entities via MQTT
discovery, with no UniFi controller anywhere in the loop.

It runs on a Raspberry Pi with an SX1302 concentrator: the bridge pairs with a
factory-default sensor over the air, decodes its application-layer reports, and
maps them to HA entities (door/motion/leak/tamper, battery, signal, button
presses) plus command buttons (locate, reboot, refresh, clear tamper). See
**[docs/OPEN_GATEWAY_PLAN.md](docs/OPEN_GATEWAY_PLAN.md)** for current state and
open work.

The protocol underneath is Ubiquiti's proprietary SuperLink — a LoRa-based
sub-GHz radio system on 915 MHz (US ISM band), reverse engineered from firmware
and over-the-air captures. The rest of this README is that protocol
documentation; packet sniffing also works standalone on a Heltec v3.

<img width="1028" height="781" alt="image" src="https://github.com/user-attachments/assets/e16ffbb9-7369-461f-b2e1-0df24a7cf506" />

## Bridge installation

Requires Python 3.12+, libsodium, and a configured SX1302 HAL on the Pi.
From a clone of this repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[mqtt]'
cp tools/sx1302/superlink_bridge.yaml.example superlink_bridge.yaml
# Edit gateway identity, allowed sensors, store path, and MQTT broker.
superlink-bridged --config superlink_bridge.yaml
```

For the existing Pi HAL checkout, `tools/sx1302/setup_rpi.sh` installs the
bridge and MQTT dependencies into `~/superlink-venv` and builds the HAL.
The HAL checkout must already exist at `~/sx1302_hal`.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the test environment.

## MQTT recovery

MQTT connects asynchronously and retries in the background, so a broker outage
does not prevent radio polling. On reconnect, the bridge restores subscriptions,
discovery, and the latest retained values observed during this daemon run.
Physical button events are not replayed. Retained inbound commands are ignored;
publish commands without retention, with `PRESS` for command buttons.

Discovery requires both bridge and device availability. Restored devices start
offline until their radio sessions recover. Graceful shutdown waits up to two
seconds for the broker to acknowledge bridge-offline; an unexpected disconnect
uses the last will. These follow the
[paho connection lifecycle](https://eclipse.dev/paho/files/paho.mqtt.python/html/client.html)
and [HA availability rules](https://www.home-assistant.io/integrations/sensor.mqtt/).

Set `mqtt.tls: true` and the broker's TLS port (usually 8883) to use certificate
and hostname verification with the system trust store. There is no insecure
certificate bypass. Custom CA/client-certificate configuration is not yet exposed.

The retained-value cache is in memory: a simultaneous daemon and broker state
loss requires fresh sensor reports to rebuild property discovery. Removal also
still cannot retract property topics published only by an earlier daemon run.
Neither queued sensor commands nor transient button events are durable.

## Device-key persistence

The bridge writes its JSON device store atomically with owner-only permissions,
flushes the file and directory, and keeps the previous validated snapshot as
`<store_path>.bak`. One daemon must own a store; do not run the probe and bridge
against the same store concurrently. Back up both files privately.

A malformed store (or a missing store with a backup still present) stops startup
rather than silently forgetting sensors. To recover, stop the daemon, preserve
both files, inspect the backup's device identities and adoption state, then
restore it to the configured store path with mode `0600`. The backup is one
version old and may contain removed devices or superseded keys; restoring it
may require re-pairing. Never post either file in an issue.

## What is SuperLink?

SuperLink is Ubiquiti's proprietary long-range wireless protocol used in UniFi Access, UniFi Connect, UniFi SmartPower, and gateway products. It uses standard Semtech LoRa chirp spread spectrum modulation but a completely proprietary MAC layer with custom framing, encryption, and device management. It is not LoRaWAN.


## Hardware

- Hub side: Semtech SX1302 digital baseband (8 simultaneous RX channels)
- Peripheral side: Semtech SX1262 single-channel LoRa transceiver (+22 dBm)
- RF front-end: Skyworks SKY66420-11 (860–930 MHz PA/LNA)

## LoRa PHY Parameters

All confirmed over-the-air:

| Parameter | Value |
|-----------|-------|
| Spreading Factor | SF5 |
| Bandwidth (UL) | 125 kHz |
| Bandwidth (DL) | 500 kHz |
| Coding Rate | 4/5 |
| Sync Word | 0x1424 (private LoRa) |
| Preamble | 12 symbols (SF5/SF6 requirement) |
| Header Mode | Explicit |
| Byte Order | Big-endian |

8 UL channels: 915.6–917.0 MHz (125 kHz, SF5)
8 DL channels: 920.4–924.6 MHz (500 kHz, SF5)
Beacon channel: 927.6 MHz

## Frame Format

```
Offset  Size  Field
------  ----  -----
0       10    Cleartext header (always unencrypted)
10      4     Integrity check (BLAKE2b truncated to 4 bytes)
14      N     Payload (XSalsa20-encrypted for SecureHeader frames)
```

Cleartext header (10 bytes):
```
Offset  Size  Field
------  ----  -----
0       1     Mctrl (management/message control)
1       1     Dctrl (data control)
2       6     MAC address (source or destination)
8       1     SeqHi (frame counter, increments per packet)
9       1     SeqLo (nonce component, pseudo-random)
```

Observed frame types by Dctrl:
- 0x54: standard UL data (19 bytes total: 10 header + 4 MIC + 5 payload)
- 0x44: variant UL data (20 bytes total: 10 header + 4 MIC + 6 payload)
- 0x63: standard DL response (16 bytes total: 10 header + 4 MIC + 2 payload)

All observed data frames use Mctrl=0xE0 (SecureHeader).

## Application-Layer Messages

Once the LoRa frame is decrypted, the payload is an application message:
`[messageId:1][messageTag:1][body…]`. The full `MessageId` enum, recovered
from UniFi Protect's `messages.ts` (webpack module 41118), is:

| ID | Hex | Name | Direction |
|----|------|------|-----------|
| 1 | `0x01` | REQUEST_STATUS_RESPONSE | device → gateway |
| 2 | `0x02` | ADOPT_REQUEST | gateway → device |
| 3 | `0x03` | ADOPT_RESPONSE | device → gateway |
| 4 | `0x04` | PING_REQUEST | gateway → device |
| 5 | `0x05` | PING_RESPONSE | device → gateway |
| 6 | `0x06` | REBOOT | gateway → device |
| 7 | `0x07` | FACTORY_RESET | gateway → device |
| 8 | `0x08` | LOCATE | gateway → device |
| 9 | `0x09` | DEVICE_INFO_REQUEST | gateway → device |
| 10 | `0x0a` | DEVICE_INFO_REPORT | device → gateway |
| 11 | `0x0b` | PROPERTY_REQUEST | gateway → device |
| 12 | `0x0c` | PROPERTY_REPORT | device → gateway |
| — | `0x0d` | *(undefined — gap in enum)* | — |
| 14 | `0x0e` | PROPERTY_SET | gateway → device |
| 15 | `0x0f` | FIRMWARE_UPDATE_START | gateway → device |
| 16 | `0x10` | FIRMWARE_CHUNK_REQUEST | device → gateway |
| 17 | `0x11` | FIRMWARE_CHUNK_RESPONSE | gateway → device |

`FACTORY_RESET` (`0x07`) is what the controller sends over the air to remotely
reset a sensor before re-adoption. `PING_REQUEST`/`PING_RESPONSE` (`0x04`/`0x05`)
is the liveness keep-alive. `PROPERTY_REQUEST` (`0x0b`) does **not** validate
property IDs — sending undefined IDs (0–255) is a memory-disclosure primitive.
Full message encodings: [`docs/protocol/superlink_application_layer.md`](docs/protocol/superlink_application_layer.md).
The end-to-end adoption/commit mechanism is documented in
[`docs/protocol/adoption_commit_mechanism.md`](docs/protocol/adoption_commit_mechanism.md).

## Decrypted Payload (USL-Entry door sensor)

Standard 5-byte UL payload:
```
Byte 0: 0x0C  type (sensor report)
Byte 1: 0x00  flags
Byte 2: 0x0F  command (door state)
Byte 3: 0x00  sub-command
Byte 4: 0x00/0x01  door state (0x00=open, 0x01=closed)
```

Extended 22-byte UL payload (sent periodically every ~16 frames):
contains sensor metadata including what appears to be battery level (0x64 = 100%),
uptime counter, and possibly temperature readings.

## Crypto

- Session keys: Curve25519 ECDH, renegotiated each time lorabrd restarts
- Data encryption: XSalsa20 (stream cipher via crypto_stream_xor)
- Authenticated encryption: XSalsa20-Poly1305 for important frames
- Integrity: BLAKE2b (4-byte truncated MAC, covers header + payload)
- Nonce: 24 bytes, derived from frame header fields; last byte increments per packet
- Pairing: hardcoded default key used during initial device adoption (see docs/protocol/crypto_and_pairing.md)

## Channel Hopping

The sensor hops sequentially through all 8 UL channels (CH1→CH2→...→CH8→repeat).
One frame per channel, ~2s TX interval, full 8-channel cycle takes ~16 seconds.
The gateway listens on all 8 UL channels simultaneously and responds on the paired DL channel.
A single-channel SX1262 sniffer captures roughly 1/8 of packets when parked on one channel.

## Gateway Emulator

A standalone Python gateway running on a Raspberry Pi + SX1302 concentrator
board. It implements the SuperLink MAC well enough to pair with a
factory-default sensor over the air — Curve25519 DH exchange, BLAKE2b session
key derivation, XSalsa20-Poly1305 authenticated DL frames, and a connection
state machine (beacon → ConnReq → DH → challenge → active). Lives in
`tools/sx1302/superlink/` as a Python package with modules for the HAL,
decoder/encoder, crypto, and the gateway state machine, plus TX and delay
sweep harnesses for tuning DL responses.

## Repository Structure

```
superlink2mqtt/
├── docs/protocol/          — frame format, crypto, channel plan, OTA captures
├── docs/teardowns/         — hardware component identification
├── tools/sniffer/          — PlatformIO project: Heltec V3 + SX1262 packet sniffer
├── tools/sx1302/           — SX1302-based Pi gateway: sniffer, emulator, sweeps
│   └── superlink/          — Python package (hal, decoder, crypto, gateway, cli)
│       └── bridge/         — MQTT bridge: sessions, entities, HA discovery
├── tools/emulator/         — sensor-side emulator scaffolding
├── tools/keyhook/          — runtime key capture helper for lorabrd
├── tools/decoder/          — placeholder for Wireshark dissector
├── tools/sdr/              — GNU Radio capture notes
├── firmware/dumps/         — extracted firmware images (gitignored)
├── src/                    — reserved for protocol library (currently empty)
└── research/               — research tracking
```

See [docs/RE_PLAN.md](docs/RE_PLAN.md) for the phased reverse engineering roadmap.

## Testing

With libsodium installed and the Python virtual environment active:

```bash
python -m pip install -r requirements-test.txt
python -m pytest tests/ -q
python tools/check_lean.py
```

The Python suite includes Hypothesis tests for malformed packets, pairing,
handshake retries, queued commands, and reconnect/watchdog timing. It uses
synthetic sensor frames, real cryptography, and a virtual clock with no radio
I/O. For a larger local run or a reproducible CI-style run:

```bash
HYPOTHESIS_PROFILE=extended python -m pytest tests/test_packet_properties.py tests/test_session_properties.py -q
HYPOTHESIS_PROFILE=ci python -m pytest tests/ -q --hypothesis-show-statistics
```

See [Python property tests](docs/TESTING.md) for invariants, replay instructions,
and limits.

The Lean command requires elan and checks formal wire-format proofs plus
10,278 model-to-Python vectors. Both Python tests and Lean verification run in
CI. See [Lean verification](verification/lean/README.md) for installation,
coverage, and proof boundaries.

## Legal

Interoperability research under applicable reverse engineering exemptions. Based on publicly available FCC filings, over-the-air captures (legal under FCC Part 15), and firmware analysis for interoperability purposes.

## License

MIT
