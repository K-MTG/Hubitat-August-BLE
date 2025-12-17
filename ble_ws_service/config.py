from __future__ import annotations

import yaml

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class LockConfig:
    lock_name: str     # Unique identifier for the lock (e.g. "Front Door")
    serial: str        # August/Yale serial, e.g. "L3045P9"
    address: str       # BLE MAC (or UUID on macOS)
    key: str           # hex key (32 chars)
    slot: int          # key index slot
    always_connected: bool = False # Whether to keep the lock always connected, note this uses more power

@dataclass
class WebSocketConfig:
    host: str = "0.0.0.0"
    port: int = 8765
    auth_token: Optional[str] = None # Optional auth token for clients

@dataclass
class ServiceConfig:
    websocket: WebSocketConfig
    locks: List[LockConfig]

def load_config(path: str | Path) -> ServiceConfig:
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open("r") as f:
        raw = yaml.safe_load(f) or {}

    service = raw.get("service", {})

    # --- WebSocket config ---
    ws_raw = service.get("websocket", {})
    websocket = WebSocketConfig(
        host=ws_raw.get("host", "0.0.0.0"),
        port=int(ws_raw.get("port", 8765)),
        auth_token=ws_raw.get("auth_token"),
    )

    # --- Locks ---
    locks: List[LockConfig] = []
    for entry in service.get("locks", []):
        locks.append(
            LockConfig(
                lock_name=entry["lock_name"],
                serial=entry["serial"],
                address=entry["address"],
                key=entry["key"],
                slot=int(entry["slot"]),
                always_connected=bool(entry.get("always_connected", False)),
            )
        )

    if not locks:
        raise ValueError("No locks configured")

    return ServiceConfig(
        websocket=websocket,
        locks=locks,
    )
