# GWMP radio backend (prototype)

**Status: prototype. Nothing here has been run against a real concentrator.**
The backend is tested only against a fake forwarder over loopback UDP
(`tests/test_gwmp_radio.py`). The default radio backend and the Home Assistant
app's SSH transport are unchanged.

`tools/sx1302/superlink/radio_gwmp.py` is a third radio backend that speaks the
Semtech UDP packet-forwarder protocol (GWMP v2). The concentrator host runs the
stock `lora_pkt_fwd` from [`sx1302_hal`](https://github.com/Lora-net/sx1302_hal)
and no project code; the bridge plays the "network server" role.

| | ctypes (`hal.py`) | SSH (`radio_remote.py`) | GWMP (`radio_gwmp.py`) |
| --- | --- | --- | --- |
| Project code on the Pi | bridge itself | `radio_server.py` + SSH wrapper | none |
| Onboarding | n/a | app key, `authorized_keys`, pinned host key | point `lora_pkt_fwd` at the bridge |
| Authentication / encryption | n/a | SSH | **none** |
| Who connects to whom | n/a | bridge → Pi :22 | Pi → bridge :1700/udp |

## Bridge configuration

```yaml
radio:
  gwmp:
    bind: 0.0.0.0        # default; use a specific interface address where possible
    port: 1700           # default
    forwarder: 192.0.2.10   # optional: only accept datagrams from this host
    forwarder_timeout: 30   # seconds without PULL_DATA before "disconnected" (default)
    tx_ack_timeout: 0.25    # seconds send() waits for TX_ACK (default)
```

Set `forwarder` (name or address, resolved at start) whenever the Pi has a
stable address. Keep `forwarder_timeout` at several times the forwarder's
`keepalive_interval`.

`radio.gwmp` cannot be combined with the SSH keys (`host`, `username`, …) in the
same `radio` block. Omitting `radio` still selects the local ctypes HAL, and a
`radio` block without `gwmp` still selects SSH.

## Behaviour

- PUSH_DATA is answered with PUSH_ACK; each `rxpk` becomes an `RxPacket`
  (`tmst` → `timestamp_us`, `stat == 1` → `crc_ok`, `datr`/`codr` parsed,
  `ul_channel` derived from the frequency, not the IF index). `stat` reports and
  malformed entries are dropped.
- `start()` binds the socket and waits up to `forwarder_timeout` for the first
  PULL_DATA, so the UI shows "connected" only once a forwarder is talking to
  the bridge. Until then the runtime shows it as unavailable and retries.
- PULL_DATA is answered with PULL_ACK and its source address becomes the
  downlink route. Once a route exists, PULL_DATA from a different host is
  ignored, and PUSH_DATA and TX_ACK are accepted only from the route's host.
- If no PULL_DATA arrives for `forwarder_timeout`, `receive()` raises
  `ConnectionError`; the runtime marks the radio disconnected, drops volatile
  sessions as it does for an SSH outage, and calls `start()` again.
- PULL_DATA from the same host on a new source port is treated as a forwarder
  restart (stock `lora_pkt_fwd` keeps one downlink socket for its lifetime, and
  a restart resets `tmst`). The new route is adopted and `receive()` raises
  `ConnectionError` once, forcing the same reconnect.
- `send()` emits one PULL_RESP `txpk` (`tmst`, or `imme: true` when no timestamp
  is given; `freq`, `rfch: 0`, `powe`, `datr: "SF5BW500"`, `codr: "4/5"`, `ipol`,
  `prea: 12`, base64 `data`) and waits up to `tx_ack_timeout` for the TX_ACK with
  the same token from the same address.
  - Empty TX_ACK or `"error": "NONE"`: success.
  - `"error": …` (`TOO_LATE`, `TX_FREQ`, `COLLISION_PACKET`, …): `RuntimeError`.
  - `"warn": …` (e.g. `TX_POWER`): logged, not raised; the frame was queued.
  - No TX_ACK in time: `RuntimeError` ("unconfirmed"). The frame is never resent,
    since the forwarder may have queued it.
  - No PULL_DATA seen yet: `RuntimeError`, nothing is sent.

  The runtime already logs `RuntimeError` from `send()` as "TX skipped".

## `lora_pkt_fwd` configuration

Derived from `tools/sx1302/superlink_us.json`. Differences from that file, each
because `lora_pkt_fwd` (checked against the `sx1302_hal` 2.1.0 source) needs it:

- `tx_freq_min` / `tx_freq_max` on `radio_0`: the forwarder rejects downlinks
  outside this range (`TX_FREQ`), and without the keys the range is empty. The
  range below covers the downlink channels and the beacon.
- `spreading_factor_enable` moved under `chan_multiSF_All`, which is the key the
  forwarder reads; at the top level of `sx1302_conf` it is ignored.
- `gateway_conf` added. Replace `server_address` with the bridge host and
  `gateway_ID` with any 16 hex digits (the bridge does not check it).
- `antenna_gain`, `rssi_tcomp`, and the TX gain LUT are copied unchanged.

```json
{
  "SX130x_conf": {
    "com_type": "SPI",
    "com_path": "/dev/spidev0.0",
    "lorawan_public": false,
    "clksrc": 0,
    "antenna_gain": 0,
    "full_duplex": false,
    "radio_0": {
      "enable": true,
      "type": "SX1250",
      "freq": 916000000,
      "rssi_offset": -215.4,
      "rssi_tcomp": { "coeff_a": 0, "coeff_b": 0, "coeff_c": 20.41, "coeff_d": 2162.56, "coeff_e": 0 },
      "tx_enable": true,
      "tx_freq_min": 920000000,
      "tx_freq_max": 928000000,
      "tx_gain_lut": [
        { "rf_power": 12, "pa_gain": 0, "pwr_idx": 15 },
        { "rf_power": 13, "pa_gain": 0, "pwr_idx": 16 },
        { "rf_power": 14, "pa_gain": 0, "pwr_idx": 17 },
        { "rf_power": 15, "pa_gain": 0, "pwr_idx": 19 },
        { "rf_power": 16, "pa_gain": 0, "pwr_idx": 20 },
        { "rf_power": 17, "pa_gain": 0, "pwr_idx": 22 },
        { "rf_power": 18, "pa_gain": 1, "pwr_idx": 1 },
        { "rf_power": 19, "pa_gain": 1, "pwr_idx": 2 },
        { "rf_power": 20, "pa_gain": 1, "pwr_idx": 3 },
        { "rf_power": 21, "pa_gain": 1, "pwr_idx": 4 },
        { "rf_power": 22, "pa_gain": 1, "pwr_idx": 5 },
        { "rf_power": 23, "pa_gain": 1, "pwr_idx": 6 },
        { "rf_power": 24, "pa_gain": 1, "pwr_idx": 7 },
        { "rf_power": 25, "pa_gain": 1, "pwr_idx": 9 },
        { "rf_power": 26, "pa_gain": 1, "pwr_idx": 11 },
        { "rf_power": 27, "pa_gain": 1, "pwr_idx": 14 }
      ]
    },
    "radio_1": {
      "enable": true,
      "type": "SX1250",
      "freq": 916600000,
      "rssi_offset": -215.4,
      "rssi_tcomp": { "coeff_a": 0, "coeff_b": 0, "coeff_c": 20.41, "coeff_d": 2162.56, "coeff_e": 0 },
      "tx_enable": false
    },
    "chan_multiSF_All": { "spreading_factor_enable": [ 5 ] },
    "chan_multiSF_0": { "enable": true, "radio": 0, "if": -400000 },
    "chan_multiSF_1": { "enable": true, "radio": 0, "if": -200000 },
    "chan_multiSF_2": { "enable": true, "radio": 0, "if": 0 },
    "chan_multiSF_3": { "enable": true, "radio": 0, "if": 200000 },
    "chan_multiSF_4": { "enable": true, "radio": 1, "if": -200000 },
    "chan_multiSF_5": { "enable": true, "radio": 1, "if": 0 },
    "chan_multiSF_6": { "enable": true, "radio": 1, "if": 200000 },
    "chan_multiSF_7": { "enable": true, "radio": 1, "if": 400000 },
    "chan_Lora_std": { "enable": false },
    "chan_FSK": { "enable": false }
  },
  "gateway_conf": {
    "gateway_ID": "AA555A0000000000",
    "server_address": "BRIDGE_HOST",
    "serv_port_up": 1700,
    "serv_port_down": 1700,
    "keepalive_interval": 5,
    "stat_interval": 30,
    "push_timeout_ms": 100,
    "forward_crc_valid": true,
    "forward_crc_error": false,
    "forward_crc_disabled": false,
    "beacon_period": 0
  }
}
```

The key names above (`SX130x_conf`, `freq`, `chan_multiSF_N`, `chan_Lora_std`,
`chan_FSK`, per-radio `enable`) are the ones `lora_pkt_fwd.c` 2.1.0 reads;
`superlink_us.json` uses `sx1302_conf`, `freq_hz`, `chan_multi_N`, which it
does not. The file was translated by hand and has not been loaded by a real
`lora_pkt_fwd`; diff it against a `global_conf.json.sx1250.*` example shipped
with `sx1302_hal` before first use.

## Not verified on hardware

Nothing below has been tested. "Source says" means a read of
`packet_forwarder/src/lora_pkt_fwd.c` and `jitqueue.c` at `sx1302_hal` 2.1.0,
which is evidence about intent, not about behaviour on the bench.

1. **SF5 uplinks.** Source says SF5 is accepted in
   `chan_multiSF_All.spreading_factor_enable` and reported as `"SF5BW125"`.
   Not confirmed that a stock build actually demodulates and forwards SuperLink
   SF5/125 kHz uplinks with the private sync word (`lorawan_public: false`) on
   this channel plan.
2. **SF5/BW500 downlinks.** Source says the `txpk` parser accepts `SF5` and
   `BW500`. Not confirmed that the resulting frame (preamble 12, CRC on,
   explicit header, `ipol` as configured) is one the sensor accepts, i.e. that
   it matches what the ctypes backend transmits.
3. **Non-LoRaWAN payloads.** Source says uplink payloads are base64-encoded
   whole and only peeked at for a log line (bytes 1-7 read as DevAddr/FCnt), and
   downlink `data` is copied to the radio untouched. Not confirmed end to end;
   other forwarders (Basics Station, ChirpStack Concentratord, vendor builds)
   may filter on the LoRaWAN MHDR and are out of scope.
4. **Downlink timing over UDP.** Downlinks are scheduled about 1 s after the
   uplink `tmst` (`downlink_delay_us`). Source says the forwarder returns
   `TOO_LATE` when a PULL_RESP arrives less than 42.5 ms before its `tmst`
   (`TX_START_DELAY` 1.5 ms + `TX_MARGIN_DELAY` 1 ms + `TX_JIT_DELAY` 40 ms).
   The budget is therefore 1 s minus: the forwarder's uplink batching and
   PUSH_DATA send, two network hops, the bridge's 10 ms poll loop plus crypto,
   and, for bursts, up to `tx_ack_timeout` of blocking per preceding frame.
   That looks comfortable on a wired LAN but has not been measured, and Wi-Fi
   or a loaded Pi may not hold it. `TOO_LATE` is surfaced as a `RuntimeError`
   and logged; the frame is lost.
5. **Burst queueing.** Multi-frame downlinks are spaced `burst_spacing_us`
   (500 ms) apart. Not confirmed that the JIT queue accepts them without
   `COLLISION_PACKET`.
6. **TX power.** The bridge requests `powe: 10` by default. The LUT in
   `superlink_us.json` starts at 12 dBm, so the forwarder will substitute
   another entry and return a `TX_POWER` warning on every downlink. Separately,
   that LUT disagrees with the one hard-coded in `hal.py` (`pwr_idx` 17 is
   10 dBm there, 14 dBm here). Which is right for this board is not established,
   so radiated power under GWMP is unknown.
7. **Beacon (927.6 MHz).** Assumed to go through the same `send()` path;
   `beacon_period: 0` disables the forwarder's own LoRaWAN beacon. Not tested.
8. **IF/channel layout.** The bridge maps uplink channel from `freq`, assuming
   the forwarder reports the nominal channel centre exactly (915.6 … 917.0 MHz
   in 200 kHz steps). If it reports anything else, `ul_channel` is 0 and the
   downlink goes to the wrong channel.
9. **Counter wrap and forwarder restarts.** `tmst` is a 32-bit microsecond
   counter (wraps every ~71.6 min). The bridge passes it through masked to
   32 bits, as the other backends do. Restart detection relies on the source
   port changing, which is how stock `lora_pkt_fwd` behaves by source reading;
   a NAT or a different forwarder that changes ports without restarting would
   cause spurious reconnects, and neither case has been observed on hardware.
10. **Liveness timing.** `forwarder_timeout` (30 s) against the forwarder's
    `keepalive_interval` is untested with a real forwarder, as is how sensors
    behave across the session drop that a reconnect causes.
11. **The `global_conf.json` above** has not been loaded by `lora_pkt_fwd`.

## Security

GWMP has **no authentication and no encryption**, unlike the SSH backend. The
backend narrows who can talk to it by source address, which stops accidents and
casual interference but not an attacker on the same network, because UDP source
addresses can be spoofed:

- With `forwarder` set, datagrams from any other address are dropped. Without
  it, the first host to send PULL_DATA after a (re)start becomes the forwarder,
  and other hosts are ignored only while it stays alive.
- A host that spoofs the forwarder's address can inject uplinks and forge
  TX_ACKs. Stealing the downlink route this way is visible (it looks like a
  restart and forces a reconnect) but still disrupts the link.
- Nothing in the bridge protects the forwarder itself: anyone who can reach its
  downlink socket with a spoofed bridge address can inject PULL_RESP and make
  the concentrator transmit arbitrary frames at any power in the LUT.
- All traffic is cleartext on the wire. SuperLink payloads are encrypted and
  MIC-protected at the protocol layer, so an injected uplink cannot forge sensor
  data, but pairing-phase traffic uses the well-known factory key, and injection
  and denial of service need no keys at all.

Use it only on a network segment you trust, set `forwarder`, bind to a specific
interface, and firewall the port to the Pi's address. If the path crosses
anything untrusted, tunnel it (WireGuard, or keep using the SSH backend).
