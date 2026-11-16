# AGENTS.md

Context for AI coding agents (and humans) working on this repo. Read this before changing code.

## What this is

Local-only control of August / Yale Bluetooth locks from Hubitat, with no August cloud involved.

```
Lock (BLE) ──► ble_ws_service (Docker, Raspberry Pi, BlueZ) ──WebSocket :8765──► Hubitat parent driver ──► child lock devices
```

Two halves ship together and share a JSON protocol:

| Path | What |
|---|---|
| `ble_ws_service/` | Python 3.13 asyncio service in Docker (`python:3.13-slim`). Talks BLE via `yalexs-ble` + `bleak`, serves the WebSocket API. |
| `drivers/august-ble-bridge-parent.groovy` | Hubitat parent: holds the WS connection, health loop, discovers locks, routes commands/events. |
| `drivers/august-ble-lock-child.groovy` | Hubitat child: `Lock`, `ContactSensor`, `Battery`, `Refresh` + `rssi`/metadata attributes. |
| `examples/august_cli.py` | One-off tool (uses `yalexs` cloud API) to fetch a lock's offline key, slot, serial, MAC. |
| `examples/ble_service_client_cli.py` | Interactive WS client for testing the service by hand. |
| `dev/` | Local scratch, **gitignored**. Not part of the product. |

## Service internals (`ble_ws_service/`)

- `main.py`: loads `config.yaml`, builds one `BleLock` per lock, starts `LockManager` then `WebSocketServer`. Exits **1** if the BLE scanner can't be recovered, so Docker's `restart: unless-stopped` brings it back with a fresh BlueZ connection. Startup has to survive a missing adapter or D-Bus: after a power cut, Docker may start the container before `bluetoothd` is ready. Today an exception during startup exits non-zero and Docker retries.
- `ble_lock.py` (`BleLock`): wraps one `yalexs_ble.PushLock`. Turns its **sync** state callback into async listener tasks. `snapshot()` builds the state dict sent to Hubitat.
- `lock_manager.py` (`LockManager`):
  - Owns the single `BleakScanner` and feeds every advertisement to every `PushLock` (yalexs-ble filters by name/address).
  - Debounces state: lock 2.0s, door 0.5s. It only commits **stable** states (`LOCKED`/`UNLOCKED`, `OPENED`/`CLOSED`). Transitional and other states (LOCKING, JAMMED, SECUREMODE, UNLATCHED…) do **not** emit events.
  - After an event settles, it schedules a `refresh` `refresh_after_seconds` later (default 8s) to reconcile.
  - A global `asyncio.Semaphore(1)` serializes lock/unlock/refresh across all locks.
  - Scanner watchdog: if there are no advertisements for 120s, restart the scanner; if they're still silent after another 120s, signal failure and the process exits 1. This exists because `bluetoothd` restarts underneath us. In practice that's mostly BlueZ 5.72 **segfaulting** (seen every 1–3 days on one deployment) with systemd restarting it, not package upgrades. bleak ≥ 3.1.0 also detects a lost D-Bus connection, which matters because `/var/run/dbus` is mounted from the host.
- `ws_server.py` (`WebSocketServer`): `websockets.asyncio.server`. Optional bearer-token auth in `_process_request`. That function **must return a `Response` from `connection.respond(...)` or `None`**; a tuple breaks the handshake. Messages on one connection are handled **sequentially**, so a slow BLE op blocks later commands on that socket.
- `config.py`: dataclasses + YAML loader. See `config_example.yaml`. Per-lock `always_connected` (more battery) and service-level `refresh_after_seconds`.
- `version.py`: service version. Bump it in release commits (`0.1.x - <summary>` style).

## WebSocket protocol (contract with the Groovy drivers)

Changing any of this requires matching driver changes. Users must then re-import the drivers on their hub, so keep changes backward compatible where possible.

- Client → server: `{"type":"command","request_id":"…","command":"heartbeat|list_locks|get_state|lock|unlock","lock_name":"…"}` (`lock_name` is required for get_state/lock/unlock).
- Server → client response: `{"type":"response","request_id":…,"status":"ok","data":{…}}` or `{"type":"response","request_id":…,"status":"error","error":"<message>"}`.
- Server → client event: `{"type":"event","event":"lock_state","lock_name":"…","state":{snapshot}}`.
- Snapshot keys: `lock_name, address, serial, locked, door, battery_pct, rssi, manufacturer, model, is_connected`. `locked`/`door` are yalexs-ble enum **names** (`LOCKED`, `UNLOCKED`, `OPENED`, `CLOSED`, but `get_state` can also return raw names like `LOCKING`/`JAMMED`/`UNKNOWN`).
- Auth: `Authorization: Bearer <token>` during the handshake. Missing token → 401, wrong token → 403.

Driver behaviour to keep in mind:
- Parent runs `healthTick` every minute. It sends an app-level `heartbeat` after 55s with no received message, resets the socket if a heartbeat goes 75s without a reply or a connect is stuck for 90s, and drops all pending requests whenever the socket isn't open.
- Child DNI is `<parentId>:<lock_name>`, so **renaming a lock in `config.yaml` creates a new child device**.
- Child maps `LOCKED/UNLOCKED` → `lock` and `OPENED/CLOSED` → `contact`; anything else is logged as a warning and ignored.

## Dependencies: yalexs-ble notes (v10)

- `PushLock.lock()`/`unlock()` return only after the lock's **op-response** (motor stopped), with a budget of up to 12s per attempt and up to 4 attempts with reconnects. LOCKING is shown at write-success and LOCKED at completion.
- A motor fault raises `OperationFailedError` (e.g. `MECH_TIMEOUT`), and the lock state shows `JAMMED` for 30s. Today that reaches Hubitat only as a WS error string.
- The library floors update polls for 6.1s after any operation/transitional state and schedules its own post-op poll. Our `refresh_after_seconds` refresh is therefore probably redundant; only change it after testing on hardware.
- Activity log (`register_log_record_callback`) is available but **not used**. Draining it consumes the lock's unread-log queue (possibly hiding history from the August app), costs extra BLE traffic, and callbacks run under the PushLock operation lock (never await a lock command inside one).
- yalexs-ble releases often and bumps major versions liberally. Diff the source of `push.py`, `lock.py`, `const.py` and `__init__.py` between versions instead of trusting release notes.

## Working on it

- No automated test suite yet. To verify changes:
  1. Create a Python 3.13 venv and run `pip install -r ble_ws_service/requirements.txt`.
  2. Import every module with `python -W error::DeprecationWarning`.
  3. Drive `LockManager` + `WebSocketServer` offline. Patch `PushLock.start`, `wait_for_first_update` and `register_callback` on the `BleLock`, fire synthetic `LockState`s, and connect with `websockets.asyncio.client`.
- Real BLE testing happens only on the Pi with a lock in range. Say plainly when a change hasn't been hardware-tested.
- On macOS, bleak uses CoreBluetooth and the lock "address" is a UUID, not a MAC.
- Deployment targets are Linux BLE hosts: Pi CM5 / Pi 4 (aarch64) and an x86_64 VM, on Ubuntu 24.04–26.04 with BlueZ 5.72–5.85. The image is `python:3.13-slim` and has no compiler, so when bumping dependencies confirm prebuilt wheels exist for **both** aarch64 and x86_64 on Python 3.13.
- Deploy is `cd <repo>/ble_ws_service && sudo docker compose up -d --build`. The container uses host networking, privileged, with `/var/run/dbus` mounted and `config.yaml` mounted read-only. Deployments are managed **by hand** per checkout (no config-management tool). The container is `august-ble-ws-service` with `restart: unless-stopped`. Docker live-restore is off, so a Docker upgrade/restart also restarts the service.
- Branches: `fix/…`, `chore/…`, `feat/…`. PR into `main`.

## Sibling project: hubitat-snooze-ble

`hubitat-snooze-ble` (separate repo, SNOOZ white-noise machines via `pysnooz`) was copied from this service and has the same layout: `ble_ws_service/`, `ws_server.py`, Docker compose with host networking. A bug in shared code is probably in both (e.g. the `_process_request` tuple bug was fixed in both). Some hosts run **both** services, so:
- **Port:** both default to `8765` with `network_mode: host`. Where they share a host, August uses `8765` and Snooz `8766`. Keep it that way.
- **Compose project name clash:** neither compose file sets `name:`, so both projects are named `ble_ws_service` after the directory. On a shared host, `--remove-orphans` in one checkout removes the **other** service's container. The fix is a unique top-level `name:` in each compose file. Changing it means recreating the running container once on every host, because `container_name` is fixed, so it's the owner's call. **Not done yet.**
- **Bluetooth adapter:** each service serializes only its own BLE ops. Scans and connects from the two containers can overlap on one adapter and cause intermittent connect failures.

## Operating on deployed hosts

- Reading (logs, `docker ps`, `journalctl`) is fine. **Any write to a host** (deploy, restart, config edit) needs the owner's go-ahead for that specific change.
- Never restart Docker or recreate containers as a side effect of something else.
- Never print secrets. Refer to where they live without opening them.
- This repo is **public**: keep host names, IPs and personal paths out of commits, PRs and this file.

## Secrets: never commit or echo these

`**/config.yaml` (offline keys, WS token), `**/auth.txt` (cloud token cache), `.pi_password`, `.pi_hosts`. All are gitignored. Offline keys are equivalent to a physical key.

## Deferred ideas (discussed, intentionally not done yet)

1. Show jams to Hubitat (`JAMMED`/`OperationFailedError` → `lock: unknown`).
2. Handle WS commands concurrently so heartbeats/get_state aren't stuck behind BLE ops.
3. Treat `SECUREMODE` as locked (Yale Linus / EU locks only).
4. Opt-in "who unlocked it" via the activity log → `lastCodeName`.
5. Re-tune or drop `refresh_after_seconds` after hardware testing.
6. Give the compose file a unique `name:` (needs a one-time container recreate on each host).
