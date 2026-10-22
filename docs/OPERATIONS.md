# Operations

## MQTT

- Connects async, retries in background; radio polling continues during broker outages.
- On reconnect: restores subscriptions, discovery, and retained values from this run. Button events not replayed.
- Retained inbound commands ignored. Send commands non-retained; buttons take `PRESS`.
- Discovery requires bridge + device availability. Restored devices start offline until their session recovers.
- Shutdown waits ≤2 s for bridge-offline ack; unexpected disconnect uses last will.
- TLS: `mqtt.tls: true` + port 8883. System trust store, hostname verified, no insecure bypass. Custom CA/client certs not exposed yet.
- Limits: retained cache is in-memory; queued commands and button events aren't durable; removal can't retract topics from earlier runs.

## Device store

- JSON, written atomically, mode `0600`, previous snapshot kept as `<store_path>.bak`.
- One daemon per store. Don't run probe and bridge on the same store.
- Malformed store, or missing store with a backup present → startup fails (no silent forgetting).
- Recovery: stop daemon, keep both files, inspect the backup, restore it to the store path with `0600`. Backup is one version old; may need re-pairing.
- Back both files up privately. Never post them in issues.
