# Management UI and sensor migration

The first management release adds an authenticated web UI to the existing
SX1302 bridge. It supports device search, live readings, individual adoption,
locate/refresh/reboot commands, activity, secret-free diagnostics, and persistent
key import with guided SSH extraction. It also includes a read-only demo without a radio or MQTT broker.

Automatic extraction targets the known ARMv7 Ubiquiti SuperLink gateway layout.
It does not run a Ubiquiti gateway backend or install as a Home Assistant
app/add-on. Those remain subsequent milestones. The existing
MQTT adapter continues to provide Home Assistant discovery.

## Install and preview

Use Python 3.11 or newer and install libsodium first (`libsodium-dev` on
Debian/Raspberry Pi OS, `libsodium` with Homebrew on macOS). From the repository:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/superlink-bridged --demo
```

Open `http://127.0.0.1:8099` and enter the temporary token printed in the terminal.
The demo only shows sample readings. Import preview works, but import application
and commands, SSH probes and extraction are disabled. Stop it with Ctrl-C. Set `SUPERLINK_WEB_TOKEN` before
starting to choose your own token (at least 32 characters).

The package includes the frontend and device profiles; Node.js is not needed to
install or run it. The native SX1302 library is only loaded in hardware mode.

For a container demo:

```sh
export SUPERLINK_WEB_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose -f compose.demo.yaml up --build
```

The compose file binds the published port to localhost. The Dockerfile also
provides a hardware-mode entry point, but does not bundle the board-specific
Semtech HAL or GPIO utilities. Hardware containers need a compatible HAL build,
the board's reset script and its dependencies, SPI/GPIO device permissions, and
a writable persistent `/data` directory for UID 10001. This container path has
not been qualified on a physical gateway; use the existing native Pi setup for
hardware operation until it is.

## Enable on an SX1302 gateway

Copy `tools/sx1302/superlink_bridge.yaml.example` to your own configuration.
Keep your existing gateway MAC, radio timing, device registry and MQTT settings.
Add:

```yaml
web:
  host: "127.0.0.1"
  port: 8099
  token_env: "SUPERLINK_WEB_TOKEN"
```

Generate a token, then start the bridge:

```sh
export SUPERLINK_WEB_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/superlink-bridged --config /path/to/superlink_bridge.yaml
```

Use the token's value to unlock the UI. Supply it through your service manager's
private environment file for unattended starts. Tokens are not placed in URLs,
logs, browser local storage or diagnostics. Unlocking is required after a page
reload. The token grants access to device data, commands and key imports.

For a Pi on another machine, an SSH tunnel keeps the default listener private:

```sh
ssh -L 8099:127.0.0.1:8099 your-user@your-gateway
```

Then open `http://127.0.0.1:8099` locally. For a shared LAN frontend, configure an
HTTPS reverse proxy and restrict access to the backend listener. The server
itself speaks HTTP. It rejects cross-origin API requests and requires a bearer
token on every API operation. A proxy must preserve the public Host header.

The paths `hal_library`, `spi_path` and `reset_script` are configurable in YAML.
Their defaults match the existing Pi installation. Changing them does not add
support for a new radio model or a different regional channel plan.

## Extract keys from an existing Ubiquiti gateway

Install the capture build dependency on the superlink2mqtt host:

```sh
.venv/bin/python -m pip install '.[extraction-build]'
```

The container image builds and bundles the ARM helper; it does not need a runtime
compiler. Native installs build it locally with pinned Zig before connecting or
restarting anything. The Zig package needs a supported host wheel (including
macOS and Linux x86-64/aarch64); a 32-bit host can use a helper built on another
machine. To prepare it, run `build_helper()` from
`superlink.bridge.extraction.build` and save its bytes as
`native/capture-armhf.so` beside that module before packaging. The source and
shell scripts are included in the Python wheel. No compiler runs on the source
gateway.

1. Enable SSH on the **SuperLink radio gateway**, using credentials for a device
   you control. Leave the original UniFi controller connected. Use the UI over
   localhost, an SSH tunnel, or an HTTPS reverse proxy when entering credentials.
2. In **Import sensors**, enter the gateway's private LAN IP and SSH port, then
   select **Check gateway**. This reads its host key without authenticating.
3. Compare the displayed SHA256 fingerprint with a trusted SSH host-key record.
   Confirm the fingerprint and the brief service interruption; enter the gateway
   SSH username and password, then choose **Extract sensor keys**. Root privileges
   are required. Credentials are sent only after the pinned host key matches.
4. The worker verifies the source layout, briefly stops its `lorabr` service,
   runs `lorabrd` with a narrow TLS capture helper for 45 seconds, then restores
   the normal service. Allow a few minutes including first-time helper compilation.
   **Do not reset or re-adopt sensors during capture.** Leaving the browser page
   does not stop the background job; its status is available when you return.
5. Select **Review extracted sensors**. Only complete persistent key pairs with
   a successful matching gateway `addDevice` response are eligible. Acknowledgement
   establishes controller provisioning, not a successful migration or physical
   sensor event. Conflicting, removed, pending, factory-key or incomplete records
   are excluded. Truncated/unsupported framing fails closed. No keys appear in the
   status API, diagnostics, browser export field, or normal logs.
6. Stop the old gateway from managing these sensors, check the migration
   confirmation, and import. The key bundle stays in server memory and expires
   after ten minutes, or is released after import/service shutdown. Review and
   import use an opaque job ID. Existing sensors are never overwritten.
7. Confirm reconnect and actual sensor readings on the new gateway, including
   after restarting both gateway and sensor. Keep the original installation
   available for recovery until this succeeds.

The supported source profile is ARMv7 (`armv7l`), OpenSSL 3,
`/usr/sbin/lorabrd --syslog /etc/persistent/cfg/lorabr.json`, and the procd-managed
`/etc/init.d/lorabr` service. Source SSH must provide a root shell, `timeout`,
`chrt`, `pidof` and ordinary BusyBox tools. Hostnames, public IPs, controllers
without this radio service, and other firmware layouts are not supported by
this extractor. The UI does not use the bench-specific research capture scripts.

A private remote directory and a lock prevent concurrent extraction. Captures
are bounded to 16 MiB; the helper only records TLS API bytes, preserving return
values and partial-write lengths. It does not override keys, randomness or
authentication. A detached watchdog attempts restoration and removes captured
plaintext even if SSH disconnects. Successful extraction additionally verifies
an unhooked normal process and remote-file cleanup over SSH before offering keys.

If either verification is uncertain, **no extracted keys can be imported**. The UI shows
**Recovery required** with an exact recovery command and a **Restore and verify
source gateway** action. Re-enter credentials to retry recovery against the same
pinned host key. It stops this job's worker, attempts restoration, checks the
normal source process, and removes remaining job files. A failed restoration
retains the lock rather than starting another capture. Raw capture files are
removed by the watchdog even on restoration failure. Gateway power loss, a
reboot, or an inaccessible host can still require operator recovery; software
cannot confirm remote restoration without a working connection.

This implementation has offline decoder, SSH orchestration, shell lifecycle and
native interception tests. The ARM helper cross-compiles, but this new automatic
flow has **not yet been qualified on physical Ubiquiti firmware**. The supported
profile reflects repository evidence, not a claim of universal UniFi support.

## Import an existing export manually

1. Obtain the current **committed** `addDevice.key` and `fallbackKey` for each
   sensor from an installation you control. Do not use transient session keys,
   factory pairing keys, or a stale pre-adoption `addDevice` request.
2. Create a JSON bundle in the format below. Include `networkId` if known and
   preserve the original gateway/network metadata separately for recovery.
3. Open **Import sensors**, choose the file or paste JSON, and select
   **Review import**. Validation checks MACs, key lengths, names, network IDs,
   duplicates and existing sessions. It does not verify keys against a sensor.
4. Stop the previous gateway from managing the selected sensors, confirm that
   step in the UI, then import. No factory-reset or remote gateway command is
   issued by the importer.
5. Wait for sensor-driven reconnect. Confirm actual readings and test a sensor
   event before considering migration successful. Confirm reconnect again after
   restarting the gateway and sensor. Preserve the original installation until
   this succeeds; no claim is made that arbitrary UniFi installations migrate
   without additional identity or firmware-specific work.

Example with **synthetic keys**, not credentials for a real device:

```json
{
  "version": 1,
  "devices": [
    {
      "mac": "02:00:00:00:00:04",
      "name": "Workshop door",
      "key": "1111111111111111111111111111111111111111111111111111111111111111",
      "fallbackKey": "2222222222222222222222222222222222222222222222222222222222222222",
      "networkId": 1167
    }
  ]
}
```

`name` and `networkId` are optional. Names are currently labels in this UI;
existing Home Assistant device/entity identities are unchanged. MACs accept
12 hex digits or colon-separated bytes. Keys require exactly 64 hex digits.
One import supports up to 100 devices and an HTTP request up to 128 KiB; the
file picker limits files to 100 KB to leave room for the request wrapper.

For one sensor, the importer also accepts a complete decoded controller pair:

```json
{
  "env": {"type": "request", "action": "addDevice"},
  "pay": {
    "mac": "020000000004",
    "key": "1111111111111111111111111111111111111111111111111111111111111111",
    "fallbackKey": "2222222222222222222222222222222222222222222222222222222222222222"
  }
}
```

This matches a single request record from `tools/mock_controller/server.py`.
Equivalent `envelope`/`payload` names and the normalized
`{"method":"addDevice","params":{...}}` shape are accepted. A raw keyhook log,
a full NDJSON capture, or a controller database is not accepted. The importer
does not guess which of several rotated key pairs is current.

`tools/keyhook/capture_key.sh` is still a research tool: it restarts `lorabrd`,
contains bench-specific connection settings, and exposes sensitive logs. The
web UI does not invoke it. Automatic extraction uses its own narrowly scoped helper and bounded lifecycle.

## Persistence and recovery

Import stores the primary key as both `primary_key` and `kdf_context`, and the
fallback as both `fallback_key` and `transport_key`. It creates adopted records
without importing temporary DH/session state or old frame counters. Sessions
perform a fresh handshake with the sensor. Imported network IDs and names
survive session persistence and restart.

All registry writes use a temporary file with mode 0600, fsync and atomic
replacement. An import into an existing JSON registry first creates a private
`<registry>.backup-<unique-id>` copy beside it. The entire import is validated
before persistence. Existing device/session identities are rejected, including
conflicts that appear after preview. Importing does not require a daemon restart.

Only one daemon may own a registry and radio. HTTP operations are serialized
onto that daemon's radio loop; do not edit the registry or run a second writer
while it is active. To recover from a migration, stop the new daemon, preserve
its current registry, restore the chosen backup, and restart the intended
gateway. Do not allow the old and new gateways to compete for the same sensors.
Backups contain keys and should be protected like the live registry.

## API and UI behavior

Standalone API requests require `Authorization: Bearer <token>` (except the
non-sensitive `/api/session` bootstrap). In the Home Assistant app, requests
must come through the verified Supervisor Ingress peer; Home Assistant handles
authentication. Do not enter an HA long-lived token into the standalone form.

| Route | Purpose |
| --- | --- |
| `GET /api/extraction` | Sanitized background job status (no keys) |
| `POST /api/extraction/probe` | Read SSH host identity with `{ "host": "192.168.1.20", "port": 22 }` |
| `POST /api/extraction/start` | Start using `probe_id`, `username`, `password`, and `confirmed: true` |
| `POST /api/extraction/preview` | Review the server-held bundle using `job_id` |
| `POST /api/extraction/import` | Import using `job_id` and `confirmed: true` |
| `POST /api/extraction/recover` | Retry source recovery with `job_id`, `username` and `password` |
| `GET /api/state` | Sanitized device snapshot, MQTT connection and activity |
| `POST /api/import/preview` | Validate `{ "document": <bundle> }` without writing |
| `POST /api/import` | Import `{ "document": <bundle>, "confirmed": true }` |
| `POST /api/devices/<mac>/command` | Submit `{ "command": "locate" }` |

Commands are `adopt`, `locate`, `refresh` and `reboot`. Adoption targets an
individually discovered sensor without an existing session. Web command success
means queued for delivery, not physical execution. Later sensor responses show
their message tag and status in Activity; the UI does not claim a response is
the confirmation of a specific browser request. Factory reset and arbitrary
raw property writes are intentionally absent from this initial UI.

Activity is bounded to the latest 100 entries and resets on daemon restart.
Readings are live projections and appear after telemetry arrives; unknown
battery, firmware, signal and last-seen values stay unknown. Diagnostics contain
device identifiers and readings but exclude keys and credentials.

The bridge can run independently of Home Assistant or as a Home Assistant app
with Ingress. The app uses the remote Pi SX1302 backend while retaining protocol
sessions and keys locally. See [app setup](../homeassistant/superlink2mqtt/DOCS.md).
Radio outages keep management available and retry automatically; interrupted
commands are discarded, and existing devices become unavailable until reconnect.
An unavailable MQTT broker also leaves management accessible. The web UI reports
HTTP and authentication failures without attempting to display raw proxy bodies.

## Validation

```sh
.venv/bin/python -m pytest tests/ -q
.venv/bin/python tools/check_lean.py
.venv/bin/python -m pip wheel . --no-deps --wheel-dir /tmp/superlink-wheels
```

`tests/test_management.py` covers simulated encrypted reconnect after import and
reload, atomic storage failure, backup permissions, concurrent imports, stale
preview conflicts, cancellation, HTTP authentication/origin checks and redaction.
`tests/test_extraction.py` covers framing, deflate, rotations, acknowledgements,
SSH pinning, source restoration, recovery, expiration and HTTP import.
`tests/test_extraction_native.py` exercises the compiled TLS wrappers with a
synthetic SSL library on Linux (skipped on macOS). These are offline tests; they do not establish physical migration, HA deployment
or support for additional gateways, sensors, firmware or radio hardware.
