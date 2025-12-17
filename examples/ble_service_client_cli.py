#!/usr/bin/env python3
"""
Interactive CLI client for August BLE WebSocket API. This intended for testing the ble_service

Example Usage: python ble_service_client_cli.py ws://10.0.3.13:8765 --token ws-shared-secret
"""

import argparse
import asyncio
import json
import logging
import shlex
import uuid
from typing import Callable, Dict, Optional, Any

from pprint import pprint
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

_LOGGER = logging.getLogger(__name__)


# ==========================================================
#  August WebSocket Client (auto-reconnect + event support)
# ==========================================================

class AugustBLEWebSocketClient:
    """
    Client interface for August BLE WebSocket API.

    Supports:
      - Sending commands (lock, unlock, get_state, list_locks)
      - Correlating responses using request_id
      - Receiving async lock_state events
      - Auto reconnect
      - Waits until initial connection is ready
    """

    def __init__(
        self,
        url: str,
        event_callback: Optional[Callable[[dict], Any]] = None,
        reconnect_delay: float = 5.0,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.url = url
        self.event_callback = event_callback
        self.reconnect_delay = reconnect_delay
        self.headers = headers or {}

        self._pending: Dict[str, asyncio.Future] = {}
        self._ws = None
        self._listener_task = None
        self._running = False

        # Fire when first connection is established
        self._connected_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self):
        """Start persistent connection loop."""
        if self._running:
            return

        self._running = True
        self._listener_task = asyncio.create_task(self._run_forever())

        # Wait until we are connected at least once
        print("Connecting to WebSocket server...")
        await self._connected_event.wait()
        print("Connected!\n")

    async def stop(self):
        """Gracefully stop the client."""
        self._running = False
        self._connected_event.clear()

        if self._ws:
            await self._ws.close()

        if self._listener_task:
            await self._listener_task

        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()

    async def lock(self, lock_name: str):
        return await self._send_command("lock", lock_name)

    async def unlock(self, lock_name: str):
        return await self._send_command("unlock", lock_name)

    async def get_state(self, lock_name: str):
        return await self._send_command("get_state", lock_name)

    async def list_locks(self):
        return await self._send_command("list_locks")

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------

    async def _run_forever(self):
        """Reconnect forever until stop()."""
        while self._running:
            try:
                _LOGGER.info("Connecting to %s", self.url)
                async with connect(self.url, additional_headers=self.headers) as ws:
                    self._ws = ws
                    _LOGGER.info("Connected")

                    # Signal ready state
                    self._connected_event.set()

                    await self._listen()

            except Exception:
                _LOGGER.exception("WebSocket connection error")

            # Clear ready event until next connection
            self._connected_event.clear()

            if self._running:
                _LOGGER.warning("Reconnecting in %.1f seconds...", self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)

        _LOGGER.info("Client fully stopped")

    # ------------------------------------------------------------------
    # Listener
    # ------------------------------------------------------------------

    async def _listen(self):
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "response":
                    req_id = msg.get("request_id")
                    fut = self._pending.pop(req_id, None)
                    if fut and not fut.done():
                        if msg["status"] == "ok":
                            fut.set_result(msg.get("data"))
                        else:
                            fut.set_exception(Exception(msg.get("error")))
                    continue

                if msg_type == "event":
                    if self.event_callback:
                        asyncio.create_task(self.event_callback(msg))
                    continue

                _LOGGER.warning("Unknown message type received: %s", msg)

        except ConnectionClosed:
            _LOGGER.warning("WebSocket disconnected")
        except Exception:
            _LOGGER.exception("Listener failure")

    # ------------------------------------------------------------------
    # Command Sending
    # ------------------------------------------------------------------

    async def _send_command(self, command: str, lock_name: Optional[str] = None):
        await self._connected_event.wait()  # NEW: ensure connection

        request_id = uuid.uuid4().hex
        future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future

        payload = {
            "type": "command",
            "request_id": request_id,
            "command": command,
        }
        if lock_name:
            payload["lock_name"] = lock_name

        await self._ws.send(json.dumps(payload))

        return await future  # Wait for server response


# ==========================================================
#   Interactive Shell (REPL)
# ==========================================================

BANNER = r"""
=========================================================
   August BLE WebSocket Interactive Shell
   Type "help" for commands. Press Ctrl+C to quit.
=========================================================
"""

HELP_TEXT = """
Available commands:

  list                   - List all lock IDs
  state <lock_name>        - Get lock state
  lock <lock_name>         - Lock a lock
  unlock <lock_name>       - Unlock a lock

Other:
  help                   - Show this help text
  quit / exit            - Exit the shell
"""


class InteractiveShell:
    def __init__(self, url: str, auth_token: Optional[str] = None):
        self.url = url

        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        self.client = AugustBLEWebSocketClient(
            url=url,
            event_callback=self._on_event,
            headers=headers,
        )
        self._running = True

    async def start(self):
        print(BANNER)
        await self.client.start()  # now guaranteed to connect

        await self._repl()

    async def _on_event(self, event: dict):
        print("\n🔔 EVENT RECEIVED:")
        pprint(event)
        print("> ", end="", flush=True)

    async def _repl(self):
        while self._running:
            try:
                line = await asyncio.to_thread(input, "> ")
                line = line.strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting...")
                break

            if not line:
                continue

            try:
                parts = shlex.split(line)
            except ValueError as e:
                print(f"❌ Parse error: {e}")
                continue

            cmd = parts[0].lower()

            try:
                if cmd in ("quit", "exit"):
                    print("Shutting down...")
                    await self.client.stop()
                    break

                if cmd == "help":
                    print(HELP_TEXT)
                    continue

                if cmd == "list":
                    pprint(await self.client.list_locks())
                    continue

                if cmd == "state":
                    if len(parts) < 2:
                        print("Usage: state <lock_name>")
                        continue
                    pprint(await self.client.get_state(parts[1]))
                    continue

                if cmd == "lock":
                    if len(parts) < 2:
                        print("Usage: lock <lock_name>")
                        continue
                    pprint(await self.client.lock(parts[1]))
                    continue

                if cmd == "unlock":
                    if len(parts) < 2:
                        print("Usage: unlock <lock_name>")
                        continue
                    pprint(await self.client.unlock(parts[1]))
                    continue

                print("Unknown command. Type 'help'.")

            except Exception as e:
                print(f"❌ Error: {e}")


# ==========================================================
#  Entry Point
# ==========================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hubitat WebSocket CLI client"
    )

    parser.add_argument(
        "url",
        help="WebSocket URL (e.g. ws://host:8765)",
    )

    parser.add_argument(
        "--token",
        "-t",
        dest="auth_token",
        help="Bearer token for WebSocket authentication",
        default=None,
    )

    return parser.parse_args()


async def main():
    args = parse_args()

    shell = InteractiveShell(
        url=args.url,
        auth_token=args.auth_token,
    )
    await shell.start()


if __name__ == "__main__":
    asyncio.run(main())
