from __future__ import annotations

import asyncio
import logging
import signal

from version import __version__
from config import load_config
from ble_lock import BleLock
from lock_manager import LockManager
from ws_server import WebSocketServer


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("august_ble_ws_service")


async def main() -> None:
    cfg = load_config("config.yaml")

    lock_manager = LockManager()

    # Create BleLock instances from config
    for lc in cfg.locks:
        lock = BleLock(
            lock_name=lc.lock_name,
            serial=lc.serial,
            address=lc.address,
            key=lc.key,
            slot=lc.slot,
            always_connected=lc.always_connected,
        )
        lock_manager.add_lock(lock)

    # Create WebSocket server
    ws_server = WebSocketServer(
        lock_manager=lock_manager,
        host=cfg.websocket.host,
        port=cfg.websocket.port,
        auth_token=cfg.websocket.auth_token,
    )

    # Start everything
    await lock_manager.start()
    await ws_server.start()

    # Handle shutdown signals
    stop_event = asyncio.Event()

    def _handle_signal(signame):
        _LOGGER.info("Received signal %s: shutting down", signame)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig.name)
        except NotImplementedError:
            # Signal handling may not be available on some platforms (e.g. Windows)
            pass

    await stop_event.wait()

    # graceful shutdown
    await ws_server.stop()
    await lock_manager.stop()


if __name__ == "__main__":
    _LOGGER.info("Starting BLE WS Service version %s", __version__)
    asyncio.run(main())
