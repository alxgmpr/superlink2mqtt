# Enabling SSH on Ubiquiti Protect Devices via Controller API

Ubiquiti Protect devices (USL-Gateway, cameras, etc.) don't expose SSH directly.
SSH must be enabled through the UniFi Protect controller API, then the device rebooted.

## Prerequisites

- UniFi Protect controller (e.g. Cloud Key, UDM, self-hosted) with admin access
- Device adopted into the controller
- Network access to both controller and device

## Step 1: Authenticate to the Controller

```bash
curl -k -c cookies.txt -D headers.txt \
  -X POST https://<CONTROLLER_IP>/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<ADMIN_USER>","password":"<ADMIN_PASS>"}'
```

Extract the CSRF token from the response headers:
```bash
CSRF=$(grep -i 'x-updated-csrf-token' headers.txt | awk '{print $2}' | tr -d '\r')
```

## Step 2: Enable SSH on the Bridge (Device)

Find the bridge ID for your device. List all bridges:
```bash
curl -k -b cookies.txt \
  https://<CONTROLLER_IP>/proxy/protect/api/bridges
```

Enable SSH on the target bridge:
```bash
curl -k -b cookies.txt \
  -X PATCH https://<CONTROLLER_IP>/proxy/protect/api/bridges/<BRIDGE_ID> \
  -H 'Content-Type: application/json' \
  -H "x-csrf-token: $CSRF" \
  -d '{"isSshEnabled":true}'
```

## Step 3: Enable SSH on the NVR

The NVR also needs SSH enabled for the setting to propagate:
```bash
curl -k -b cookies.txt \
  -X PATCH https://<CONTROLLER_IP>/proxy/protect/api/nvr \
  -H 'Content-Type: application/json' \
  -H "x-csrf-token: $CSRF" \
  -d '{"isSshEnabled":true}'
```

## Step 4: Reboot the Device

The SSH setting takes effect after reboot. Trigger via the device's local web API:
```bash
curl -k https://<DEVICE_IP>/api/1.1/reboot
```

Wait ~60 seconds for reboot to complete.

## Step 5: SSH In

Default credentials are `ubnt` with the password shown in the device's local web UI
(accessible at `https://<DEVICE_IP>/` using the same admin credentials).

```bash
ssh ubnt@<DEVICE_IP>
```

## Re-enabling SSH on our bridge (working procedure)

Every bridge reset / re-adoption / firmware reprovision clears `isSshEnabled`, so this
is a recurring step. The curl flow above is the generic method; this is what works here.

- Controller = the **UCG-Fiber console** at `10.1.1.1` (UniFi OS). Root SSH to it via
  `~/.ssh/config` host `router` (key auth) — not needed for this procedure.
- Bridge id `6a612ad103c80a03e4027261`, MAC `9041B23483DC`, name "USL Gateway".
- The bridge IP drifts across resets (has been both `10.1.1.141` and `10.1.10.141`).
  Trust the `host` field from the API, not a hardcoded value.

**Shortcuts that don't work:**

- A Protect **Integration API key** (`X-API-KEY`, `/proxy/protect/integration/v1/...`)
  is read-only here: it can list bridges, but its PATCH schema rejects `isSshEnabled`
  ("must NOT have additional properties"). It is still useful for reading state
  (`GET /proxy/protect/integration/v1/bridges` to confirm the bridge is `CONNECTED`).
- Root on the console does not get a Protect session: the internal `/api/` returns 401
  for API-key, `Authorization: Bearer`, and the `unifi-core-direct` mTLS client cert.

The toggle exists only on the internal `/proxy/protect/api/...` path, which needs a
logged-in session cookie + CSRF token. The reliable way to get one is an
already-authenticated browser session.

### 1. Open a Protect session in the browser

Open the Network/Protect web UI at `https://10.1.1.1/` and accept the self-signed cert.
Run the snippets below from that page (devtools console, or Claude's
`claude-in-chrome` `javascript_tool`) so they are same-origin with cookies.

### 2. Read the bridge and the CSRF token

```js
const r = await fetch('/proxy/protect/api/bridges', {credentials:'include'});
const csrf = r.headers.get('x-csrf-token');
const b = (await r.json())[0];
({ id: b.id, host: b.host, mac: b.mac, isSshEnabled: b.isSshEnabled, csrf });
```

### 3. Enable SSH on the bridge AND the NVR

Both are required; the NVR-level flag gates propagation.

```js
const CSRF = '<csrf-from-step-2>';
async function patch(path){
  const r = await fetch(path, {method:'PATCH', credentials:'include',
    headers:{'Content-Type':'application/json','x-csrf-token':CSRF},
    body: JSON.stringify({isSshEnabled:true})});
  return {path, status:r.status, isSshEnabled:(await r.json()).isSshEnabled};
}
[ await patch('/proxy/protect/api/bridges/6a612ad103c80a03e4027261'),
  await patch('/proxy/protect/api/nvr') ];
```

Expect `status:200, isSshEnabled:true` on both. **No reboot needed**: SSH comes up within
a second or two.

### 4. Verify

User `ubnt`, password = the `webpass` in `tools/keyhook/capture_key.sh`. Use `host` from
step 2:

```bash
sshpass -p '<webpass>' ssh -o StrictHostKeyChecking=accept-new \
  ubnt@<bridge_host> 'echo SSH_OK; uname -a; pidof lorabrd && echo lorabrd_running'
```

`SSH_OK` + `lorabrd_running` means done. If it still refuses after ~10 s, re-check that
both PATCHes returned 200, then reboot the bridge (UI, or `reboot` over SSH once
reachable) and retry.

## Notes

- The controller may **re-disable SSH on reprovisioning**. If SSH stops working after
  a firmware update or re-adoption, repeat the process.
- The local web UI at `https://<DEVICE_IP>/` uses its own credentials (username `ubnt`,
  password visible in the Protect controller under device settings).
- The CSRF token comes from the `x-updated-csrf-token` response header on login.
  It may also be refreshed on subsequent API responses — always use the latest one.
- The device's local API uses `/api/1.1/` (not `/api/1.0/`).
