from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, Set, Optional

from http import HTTPStatus
from websockets.asyncio.server import serve, ServerConnection, Server
from websockets.server import Request
from websockets.exceptions import ConnectionClosed

from lock_manager import LockManager

_LOGGER = logging.getLogger(__name__)


class WebSocketServer:
    """
    WebSocket API for August Locks.

    Authentication:
      - Optional, controlled by server configuration.
      - If an auth token is configured, clients MUST include the following
        HTTP header during the WebSocket handshake:

            Authorization: Bearer <token>

      - If authentication fails:
          * Missing or malformed token → HTTP 401 (Unauthorized)
          * Invalid token               → HTTP 403 (Forbidden)

      - If no auth token is configured, all connections are accepted.

    Protocol (JSON messages):

    From client (command):
      {
        "type": "command",
        "request_id": "abc123",   # optional, echoed in response
        "command": "lock" | "unlock" | "get_state" | "list_locks",
        "lock_name": "garage_entry"   # required for lock/unlock/get_state
      }

    To client (responses/events):
      - Command response:
        {
          "type": "response",
          "request_id": "abc123",
          "status": "ok" | "error",
          "data": {...},            # command-specific
          "error": "message"        # only on error
        }

      - Lock event:
        {
          "type": "event",
          "event": "lock_state",
          "lock_name": "garage_entry",
          "state": {...}            # lock snapshot
        }
    """

    def __init__(self, lock_manager: LockManager, host: str, port: int, auth_token: Optional[str] = None) -> None:
        self._lock_manager = lock_manager
        self._host = host
        self._port = port
        self._auth_token = auth_token

        # Track connected clients
        self._clients: Set[ServerConnection] = set()

        # Server handle from websockets.asyncio.server.serve
        self._server: Optional[Server] = None

        # Subscribe to lock events from LockManager
        self._lock_manager.register_event_listener(self._handle_lock_event)

    async def _process_request(
            self,
            _connection: ServerConnection,
            request: Request,
    ):
        if not self._auth_token:
            return None  # auth disabled

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            _LOGGER.warning("Missing Authorization header")

            return (
                HTTPStatus.UNAUTHORIZED,
                [("WWW-Authenticate", "Bearer")],
                b"Missing Authorization header\n",
            )

        token = auth_header.removeprefix("Bearer ").strip()
        if token != self._auth_token:
            _LOGGER.warning("Invalid auth token")

            return (
                HTTPStatus.FORBIDDEN,
                [],
                b"Invalid auth token\n",
            )

        return None

    async def start(self) -> None:
        """
        Start the WebSocket server.

        Caller is responsible for keeping the event loop running
        (e.g. by awaiting a forever Future elsewhere).
        """
        self._server = await serve(self._handler, self._host, self._port, process_request=self._process_request,
                                   ping_interval=30, ping_timeout=10)
        _LOGGER.info("WebSocket server listening on ws://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        """
        Gracefully shut down the server and disconnect all clients.
        """
        _LOGGER.info("Shutting down WebSocket server")

        # Stop accepting new connections.
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Close all existing clients.
        if self._clients:
            # Copy to list to avoid modification during iteration.
            clients = list(self._clients)
            self._clients.clear()

            close_coros = [
                self._safe_close(ws, code=1001, reason="Server shutting down")
                for ws in clients
            ]
            await asyncio.gather(*close_coros, return_exceptions=True)

        _LOGGER.info("WebSocket server shut down complete")

    async def _handler(self, websocket: ServerConnection) -> None:
        """
        Handle a single client connection.

        In websockets.asyncio.server, the handler receives a ServerConnection.
        Path is available as websocket.request.path if needed.
        """
        remote = getattr(websocket, "remote_address", None)
        _LOGGER.info("Client connected from %s", remote)
        self._clients.add(websocket)

        try:
            async for raw in websocket:
                # raw is a text message (str) by default.
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send_error(
                        websocket,
                        request_id=None,
                        error="invalid_json",
                    )
                    continue

                await self._handle_message(websocket, msg)

        except ConnectionClosed:
            _LOGGER.info("Client disconnected: %s", remote)
        except Exception:
            _LOGGER.exception("Unexpected error in WebSocket handler")
        finally:
            self._clients.discard(websocket)

    async def _handle_message(
        self, websocket: ServerConnection, msg: Dict
    ) -> None:
        msg_type = msg.get("type")
        request_id = msg.get("request_id")

        if msg_type != "command":
            await self._send_error(
                websocket,
                request_id=request_id,
                error="type_must_be_command",
            )
            return

        command = msg.get("command")
        lock_name = msg.get("lock_name")

        try:
            if command == "list_locks":
                lock_names = self._lock_manager.get_lock_names()
                await self._send_ok(
                    websocket,
                    request_id=request_id,
                    data={"locks": lock_names},
                )
                return

            if command in ("lock", "unlock", "get_state") and not lock_name:
                raise ValueError("lock_name is required")

            if command == "lock":
                await self._lock_manager.get_lock(lock_name).lock()
                await self._send_ok(
                    websocket,
                    request_id=request_id,
                    data={"lock_name": lock_name},
                )
                return

            if command == "unlock":
                await self._lock_manager.get_lock(lock_name).unlock()
                await self._send_ok(
                    websocket,
                    request_id=request_id,
                    data={"lock_name": lock_name},
                )
                return

            if command == "get_state":
                snapshot = self._lock_manager.get_lock(lock_name).snapshot()
                await self._send_ok(
                    websocket,
                    request_id=request_id,
                    data=snapshot,
                )
                return

            # Unknown command
            raise ValueError(f"unknown_command: {command}")
        except Exception as exc:
            _LOGGER.exception("Error handling command: %s", msg)
            await self._send_error(
                websocket,
                request_id=request_id,
                error=str(exc),
            )

    async def _handle_lock_event(self, event: dict) -> None:
        """
        Called by LockManager when a lock state changes. Broadcast to all clients.

        Event shape: {"type": "lock_state", "lock_name": ..., "state": {...}}
        """
        if not self._clients:
            return

        payload = {
            "type": "event",
            "event": "lock_state",
            "lock_name": event["lock_name"],
            "state": event["state"],
        }
        msg = json.dumps(payload)

        coros = [self._safe_send(ws, msg) for ws in list(self._clients)]
        await asyncio.gather(*coros, return_exceptions=True)

    # ==== Helper methods ==================================================

    async def _send_ok(
        self,
        websocket: ServerConnection,
        request_id: Optional[str],
        data: Dict,
    ) -> None:
        response = {
            "type": "response",
            "request_id": request_id,
            "status": "ok",
            "data": data,
        }
        await self._safe_send(websocket, json.dumps(response))

    async def _send_error(
        self,
        websocket: ServerConnection,
        request_id: Optional[str],
        error: str,
    ) -> None:
        response = {
            "type": "response",
            "request_id": request_id,
            "status": "error",
            "error": error,
        }
        await self._safe_send(websocket, json.dumps(response))

    async def _safe_send(self, ws: ServerConnection, msg: str) -> None:
        """
        Send a message to a client, ignoring broken connections.
        """
        try:
            await ws.send(msg)
        except ConnectionClosed:
            self._clients.discard(ws)
        except Exception:
            _LOGGER.exception("Error sending message to client")
            self._clients.discard(ws)

    async def _safe_close(
        self,
        ws: ServerConnection,
        code: int = 1001,
        reason: str = "Server shutting down",
    ) -> None:
        """
        Close a client connection, ignoring errors.
        """
        try:
            await ws.close(code=code, reason=reason)
        except Exception:
            _LOGGER.debug("Error closing client connection", exc_info=True)
        finally:
            self._clients.discard(ws)
