from __future__ import annotations

import asyncio
import logging
from typing import Callable, List, Optional, Coroutine, Any

from yalexs_ble import (
    PushLock,
    LockState,
    serial_to_local_name,
    LockInfo,
    ConnectionInfo,
    AuthError,
    YaleXSBLEError,
    close_stale_connections_by_address,
)

_LOGGER = logging.getLogger(__name__)

StateListener = Callable[[str, LockState, LockInfo, ConnectionInfo], Coroutine[Any, Any, None]]


class BleLock:
    """
    High-level wrapper around a single PushLock instance.
    """
    DEVICE_TIMEOUT = 55  # seconds

    def __init__(
        self,
        lock_name: str,
        serial: str,
        address: str,
        key: str,
        slot: int,
        always_connected: bool = False,
    ) -> None:
        self.lock_name = lock_name
        self.address = address
        self.serial = serial
        self._push_lock = PushLock(
            local_name=serial_to_local_name(self.serial),
            address=address,
            key=key,
            key_index=slot,
            always_connected=always_connected,
        )
        self._shutdown_cb: Optional[Callable[[], None]] = None
        self._listeners: List[StateListener] = []
        self._first_update_waited = False

    def register_state_listener(self, listener: StateListener) -> None:
        self._listeners.append(listener)

    async def start(self) -> None:
        """
        Prepare the lock for use:
        - close stale connections
        - start PushLock background tasks
        - wait for first update (or timeout)
        """
        _LOGGER.info("[%s] closing stale connections at %s", self.lock_name, self.address)
        await close_stale_connections_by_address(self.address)

        _LOGGER.info("[%s] starting PushLock", self.lock_name)

        # Register callback into PushLock; this is a sync callback
        def _state_changed(
            new_state: LockState, lock_info: LockInfo, conn_info: ConnectionInfo
        ) -> None:
            # fan out to async listeners
            for listener in self._listeners:
                asyncio.create_task(listener(self.lock_name, new_state, lock_info, conn_info))

        self._push_lock.register_callback(_state_changed)

        # Start push mode (BLE notifications / periodic updates inside yalexs_ble)
        self._shutdown_cb = await self._push_lock.start()

        # Wait for first update (so we have initial state)
        try:
            await self._push_lock.wait_for_first_update(self.DEVICE_TIMEOUT)
            _LOGGER.info("[%s] received first state update", self.lock_name)
            self._first_update_waited = True
        except AuthError as ex:
            _LOGGER.error("[%s] auth error while waiting for first update: %s", self.lock_name, ex)
        except (YaleXSBLEError, TimeoutError) as ex:
            _LOGGER.warning(
                "[%s] failed to get first update in time: %s; lock might be out of range",
                self.lock_name,
                ex,
            )

    async def stop(self) -> None:
        """
        Shut down background tasks and connections cleanly.
        """
        if self._shutdown_cb:
            _LOGGER.info("[%s] shutting down PushLock", self.lock_name)
            try:
                self._shutdown_cb()
            except Exception:
                _LOGGER.exception("[%s] error during shutdown callback", self.lock_name)
            self._shutdown_cb = None

    async def lock(self) -> None:
        _LOGGER.info("[%s] lock()", self.lock_name)
        await self._push_lock.lock()

    async def unlock(self) -> None:
        _LOGGER.info("[%s] unlock()", self.lock_name)
        await self._push_lock.unlock()

    async def refresh(self) -> None:
        """
        Ask the lock to refresh state (yalexs_ble will handle connection / polling).
        """
        _LOGGER.info("[%s] refresh()", self.lock_name)
        await self._push_lock.update()

    def snapshot(self) -> dict:
        """
        Return a snapshot of current known state.
        Note: this is not guaranteed to be "live" without a refresh().
        """
        state = self._push_lock.lock_state
        info = self._push_lock.lock_info
        conn = self._push_lock.connection_info

        return {
            "lock_name": self.lock_name,
            "address": self.address,
            "serial": self.serial,
            "locked": state.lock.name if state and state.lock is not None else None,
            "door": state.door.name if state and state.door is not None else None,
            "battery_pct": state.battery.percentage if state and state.battery else None,
            "rssi": conn.rssi if conn else None,
            "manufacturer": info.manufacturer if info else None,
            "model": info.model if info else None,
            "is_connected": self._push_lock.is_connected,
        }

    @property
    def push_lock(self) -> PushLock:
        """Expose underlying PushLock so LockManager can call update_advertisement."""
        return self._push_lock
