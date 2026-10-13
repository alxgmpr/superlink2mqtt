# Management UI and sensor migration

The first management release adds an authenticated web UI to the existing
SX1302 bridge. It supports device search, live readings, individual adoption,
locate/refresh/reboot commands, activity, secret-free diagnostics, and persistent
key import. It also includes a read-only demo without a radio or MQTT broker.

This release imports keys **already exported by an operator**. It does not
automatically extract keys from UniFi, run a Ubiquiti gateway backend, or install
as a Home Assistant app/add-on. Those are subsequent milestones. The existing
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
and commands are disabled. Stop it with Ctrl-C. Set `SUPERLINK_WEB_TOKEN` before
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

## Import existing sensors

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
web UI does not invoke it. A guided, firmware-qualified exporter remains open
work.

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

All API routes require `Authorization: Bearer <token>`.

| Route | Purpose |
| --- | --- |
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

The management API provides the interface for future remote clients and a
Ubiquiti backend. The current implementation still runs alongside the SX1302
runtime: all radio timing remains on that host. Home Assistant can run elsewhere
and consume MQTT. An HA Ingress package and a Ubiquiti application-message backend
are not implemented by this change.

## Validation

```sh
.venv/bin/python -m pytest tests/ -q
.venv/bin/python tools/check_lean.py
.venv/bin/python -m pip wheel . --no-deps --wheel-dir /tmp/superlink-wheels
```

`tests/test_management.py` covers simulated encrypted reconnect after import and
reload, atomic storage failure, backup permissions, concurrent imports, stale
preview conflicts, cancellation, HTTP authentication/origin checks and redaction.
These are offline tests; they do not establish physical migration, HA deployment
or support for additional gateways, sensors, firmware or radio hardware.
