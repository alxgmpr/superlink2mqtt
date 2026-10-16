# Management UI and sensor pairing

The management UI supports device search, live readings, individual pairing,
locate/refresh/reboot commands, activity and diagnostics without secrets. New
sensors generate fresh keys during pairing. A read-only demo runs without a
radio or MQTT broker.

## Install and preview

Use Python 3.11 or newer and install libsodium first (`libsodium-dev` on
Debian/Raspberry Pi OS, `libsodium` with Homebrew on macOS). From the repository:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/superlink-bridged --demo
```

Open `http://127.0.0.1:8099` and enter the temporary token printed in the terminal.
The demo only shows sample readings; pairing and commands are disabled.
Stop it with Ctrl-C. Set `SUPERLINK_WEB_TOKEN` before
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
reload. The token grants access to device data, pairing and commands.

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

## Pair sensors with fresh keys

1. Put a new or factory-reset sensor in pairing mode using its model's instructions.
   Moving a sensor from UniFi requires clearing its old pairing with a factory reset.
2. Open **Devices**, select the discovered sensor and press **Pair**. Use
   `adopt: []` in the bridge configuration for manual pairing; an explicit MAC
   allowlist pairs matching sensors automatically.
3. Wait for pairing to complete and fresh readings to appear. Verify a physical
   event and the matching MQTT/Home Assistant update.
4. Back up the device store and gateway identity. After a planned restart,
   confirm reconnect and another fresh event.

Pairing generates fresh persistent keys and saves them for future reconnects.
Existing paired devices retain their keys across updates and restarts. The app
has no key import or UniFi SSH extraction flow and needs no credentials for the
original gateway. The Home Assistant app still uses a dedicated SSH key for its
Pi radio connection.

## Persistence and recovery

Registry writes use a private temporary file, fsync and atomic replacement.
Only one daemon may own a registry and radio. HTTP operations are serialized
onto that daemon's radio loop; do not edit the registry or run a second writer
while it is active. Backups contain keys and need the same protection as the
live registry. To restore an existing bridge, stop it, preserve its current
files, restore the chosen backup and start only the intended controller.

## API and UI behavior

Standalone API requests require `Authorization: Bearer <token>` (except the
non-sensitive `/api/session` bootstrap). In the Home Assistant app, requests
must come through the verified Supervisor Ingress peer; Home Assistant handles
authentication. Do not enter an HA long-lived token into the standalone form.

| Route | Purpose |
| --- | --- |
| `GET /api/state` | Sanitized device snapshot, MQTT connection and activity |
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

`tests/test_management.py` covers pairing commands, rejected legacy import and
extraction routes, HTTP authentication/origin checks and redaction.
`tests/test_session_properties.py` covers generated fresh-key pairing sequences,
commit and restored reconnect. Historical extraction and import helpers remain
research code and are not exposed by the management server. These offline tests
do not establish physical sensor operation or Home Assistant deployment.
