from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from yalexs_ble import LockState
from yalexs_ble.const import LockInfo, ConnectionInfo, LockStatus, DoorStatus

from ble_lock import BleLock

_LOGGER = logging.getLogger(__name__)

EventListener = Callable[[dict], Awaitable[None]]


class LockManager:
    """
    Owns:
      - BleLock instances
      - a single BleakScanner
      - event listeners (e.g. WebSocket server)

    Emits debounced, authoritative lock_state events.
      {"type": "lock_state", "lock_name": "...", "state": {...}}
    """

    LOCK_DEBOUNCE_SECONDS = 2.0
    DOOR_DEBOUNCE_SECONDS = 0.5
    REFRESH_AFTER_SECONDS = 8.0

    def __init__(self) -> None:
        self._locks: Dict[str, BleLock] = {}
        self._scanner: Optional[BleakScanner] = None
        self._event_listeners: List[EventListener] = []

        # Track last commited stable lock + door independently
        self._critical_state: Dict[str, Tuple[Optional[LockStatus], Optional[DoorStatus]]] = {}

        # Pending debounce task per lock
        self._pending_tasks: Dict[str, asyncio.Task] = {}

        # Last observed candidate state per lock
        self._pending_state: Dict[str, Tuple[Optional[LockStatus], Optional[DoorStatus]]] = {}

        # Why we are debouncing (door vs lock)
        self._pending_reason: Dict[str, str] = {}

        # Global BLE op semaphore:
        # Ensures we never run overlapping BLE operations across multiple locks.
        self._ble_op_sem = asyncio.Semaphore(1)

    # ------------------------------------------------------------------
    # Event listener registration
    # ------------------------------------------------------------------
    def register_event_listener(self, listener: EventListener) -> None:
        """
        Listener signature: async def listener(event: dict) -> None
        Events produced: {"type": "lock_state", "lock_name": ..., "state": {...}}
        """
        self._event_listeners.append(listener)

    # ------------------------------------------------------------------
    # Lock registration
    # ------------------------------------------------------------------
    def add_lock(self, lock: BleLock) -> None:
        """
        Register a BleLock with this manager and hook up its state listener.
        """
        if lock.lock_name in self._locks:
            raise ValueError(f"Duplicate lock_name: {lock.lock_name}")

        self._locks[lock.lock_name] = lock

        # Connect BleLock's state events into our event bus.
        # NOTE: BleLock.register_state_listener is expected to call this as:
        #   listener(new_state, lock_info, conn_info)
        async def _on_state(
            lock_name: str,
            new_state: LockState,
            lock_info: LockInfo,
            conn_info: ConnectionInfo,
        ) -> None:
            lock_status = new_state.lock
            door_status = new_state.door

            stable_lock = (
                lock_status if lock_status in (LockStatus.LOCKED, LockStatus.UNLOCKED) else None
            )
            stable_door = (
                door_status if door_status in (DoorStatus.OPENED, DoorStatus.CLOSED) else None
            )

            # compare against pending state first
            prev_lock, prev_door = self._pending_state.get(
                lock_name,
                self._critical_state.get(lock_name, (None, None)),
            )

            lock_changed = stable_lock is not None and stable_lock != prev_lock
            door_changed = stable_door is not None and stable_door != prev_door

            if not lock_changed and not door_changed:
                _LOGGER.debug(
                    "[%s] Ignoring duplicate candidate state (lock=%s, door=%s)",
                    lock_name,
                    lock_status,
                    door_status,
                )
                return

            # Update candidate state
            self._pending_state[lock_name] = (
                stable_lock if stable_lock is not None else prev_lock,
                stable_door if stable_door is not None else prev_door,
            )

            # Door wins debounce priority
            if door_changed:
                self._pending_reason[lock_name] = "door"
            elif lock_changed:
                self._pending_reason[lock_name] = "lock"

            self._schedule_state_settle(lock)

        lock.register_state_listener(_on_state)

    # ------------------------------------------------------------------
    # BLE command serialization helpers
    # ------------------------------------------------------------------
    async def _with_ble_lock(self, lock_name: str, op_name: str, coro_factory):
        """
        Run a BLE operation under the global semaphore.
        coro_factory must be a 0-arg callable returning an awaitable.
        """
        async with self._ble_op_sem:
            _LOGGER.debug("[%s] BLE op start: %s", lock_name, op_name)
            try:
                return await coro_factory()
            finally:
                _LOGGER.debug("[%s] BLE op end: %s", lock_name, op_name)

    async def cmd_lock(self, lock_name: str) -> None:
        lock = self.get_lock(lock_name)
        await self._with_ble_lock(lock_name, "lock", lock.lock)

    async def cmd_unlock(self, lock_name: str) -> None:
        lock = self.get_lock(lock_name)
        await self._with_ble_lock(lock_name, "unlock", lock.unlock)

    async def cmd_refresh(self, lock_name: str) -> None:
        lock = self.get_lock(lock_name)
        await self._with_ble_lock(lock_name, "refresh", lock.refresh)

    # ------------------------------------------------------------------
    # Unified debounce + refresh pipeline
    # ------------------------------------------------------------------
    def _schedule_state_settle(self, lock: BleLock) -> None:
        lock_name = lock.lock_name

        # Cancel any existing settle task
        old_task: Optional[asyncio.Task] = self._pending_tasks.get(lock_name)
        if old_task and not old_task.done():
            old_task.cancel()

        settle_task: Optional[asyncio.Task] = None  # will be assigned after create_task

        async def _settle():
            nonlocal settle_task
            try:
                reason = self._pending_reason.get(lock_name)
                delay = (
                    self.DOOR_DEBOUNCE_SECONDS
                    if reason == "door"
                    else self.LOCK_DEBOUNCE_SECONDS
                )

                # 1) Debounce
                await asyncio.sleep(delay)

                stable_lock, stable_door = self._pending_state.get(lock_name, (None, None))
                self._critical_state[lock_name] = (stable_lock, stable_door)

                snapshot = lock.snapshot()

                if stable_lock is not None:
                    snapshot["locked"] = stable_lock.name
                if stable_door is not None:
                    snapshot["door"] = stable_door.name

                event = {
                    "type": "lock_state",
                    "lock_name": lock_name,
                    "state": snapshot,
                }

                _LOGGER.info(
                    "[%s] State settled -> lock=%s, door=%s",
                    lock_name,
                    snapshot.get("locked"),
                    snapshot.get("door"),
                )

                await self._broadcast(event)

                # 2) Reconcile with refresh (serialized across all locks)
                await asyncio.sleep(self.REFRESH_AFTER_SECONDS)
                _LOGGER.info("[%s] Refreshing lock after settle", lock_name)
                await self.cmd_refresh(lock_name)

            except asyncio.CancelledError:
                _LOGGER.debug("[%s] Settle cancelled due to new activity", lock_name)
            except Exception:
                _LOGGER.exception("[%s] Error during settle/refresh", lock_name)
            finally:
                # IMPORTANT: only the currently-tracked task is allowed to clean up
                if self._pending_tasks.get(lock_name) is settle_task:
                    self._pending_tasks.pop(lock_name, None)
                    self._pending_reason.pop(lock_name, None)

        settle_task = asyncio.create_task(_settle())
        self._pending_tasks[lock_name] = settle_task

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    def get_lock_names(self) -> List[str]:
        return list(self._locks.keys())

    def get_lock(self, lock_name: str) -> BleLock:
        return self._locks[lock_name]

    # ------------------------------------------------------------------
    # Lifecycle: start / stop
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """
        Start BLE scanning and all locks.
        """
        if self._scanner:
            return

        _LOGGER.info("Starting BleakScanner")
        self._scanner = BleakScanner(detection_callback=self._on_advertisement)
        await self._scanner.start()
        _LOGGER.info("BLE scanner started")

        # Start all locks
        for lock in self._locks.values():
            await lock.start()

    async def stop(self) -> None:
        """
        Stop BLE scanning and all locks.
        """
        _LOGGER.info("Stopping LockManager")

        # Stop locks first so they stop scheduling work
        for lock in self._locks.values():
            await lock.stop()

        if self._scanner:
            _LOGGER.info("Stopping BLE scanner")
            await self._scanner.stop()
            self._scanner = None

    # ------------------------------------------------------------------
    # BLE advertisement fan-out
    # ------------------------------------------------------------------
    def _on_advertisement(
        self,
        device: BLEDevice,
        adv: AdvertisementData,
    ) -> None:
        """
        Called by BleakScanner when any BLE advertisement is seen.
        We feed them to all locks; yalexs_ble internally filters by name/address.
        """
        for lock in self._locks.values():
            lock.push_lock.update_advertisement(device, adv)

    # ------------------------------------------------------------------
    # Event broadcast
    # ------------------------------------------------------------------
    async def _broadcast(self, event: dict) -> None:
        """
        Fan out an event to all registered listeners.
        """
        if not self._event_listeners:
            return

        for listener in self._event_listeners:
            asyncio.create_task(self._run_listener(listener, event))

    async def _run_listener(self, listener: EventListener, event: dict) -> None:
        try:
            await listener(event)
        except Exception:
            _LOGGER.exception("Error in event listener")
