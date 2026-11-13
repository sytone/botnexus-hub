"""One active connector per world and an in-memory, never-replayed send queue."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import anyio
from starlette.websockets import WebSocket, WebSocketDisconnect

from .security import InvalidInput, MAX_FRAME_BYTES, parse_json, routing_id
from .normalize import normalize_result, normalize_snapshot


@dataclass(eq=False)
class Connection:
    world_id: str
    websocket: WebSocket
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: bool = True
    has_snapshot: bool = False
    closed: bool = False
    pending: set[str] = field(default_factory=set)


class Router:
    def __init__(self, store, command_timeout: float):
        self.store = store
        self.command_timeout = command_timeout
        self.connections: dict[str, Connection] = {}
        self.deadlines: dict[str, asyncio.Task] = {}

    def connected(self, world_id: str) -> bool:
        connection = self.connections.get(world_id)
        return connection is not None and connection.active

    def ready(self, world_id: str) -> bool:
        connection = self.connections.get(world_id)
        return connection is not None and connection.active and connection.has_snapshot

    def claim(self, world_id: str, websocket: WebSocket) -> Connection | None:
        if self.connected(world_id):
            return None
        connection = Connection(world_id, websocket)
        self.connections[world_id] = connection
        return connection

    def has_capacity(self, world_id: str) -> bool:
        connection = self.connections.get(world_id)
        return connection is not None and connection.active and not connection.queue.full() and len(connection.pending) < 64

    def enqueue(self, world_id: str, command_id: str, body: dict):
        connection = self.connections[world_id]
        connection.pending.add(command_id)
        connection.queue.put_nowait({"type": "command", "id": command_id, **body})
        self.deadlines[command_id] = asyncio.create_task(self._timeout(connection, command_id))

    async def _timeout(self, connection: Connection, command_id: str):
        try:
            await asyncio.sleep(self.command_timeout)
            self.store.command_timeout(command_id)
            connection.pending.discard(command_id)
        finally:
            self.deadlines.pop(command_id, None)

    def _finish_pending(self, connection: Connection, command_id: str):
        connection.pending.discard(command_id)
        task = self.deadlines.pop(command_id, None)
        if task is not None:
            task.cancel()

    async def disconnect(self, world_id: str, reason="connector_disconnected", code=1000):
        connection = self.connections.get(world_id)
        if connection is not None:
            await self._disconnect(connection, reason, code)

    async def _disconnect(self, connection: Connection, reason: str, code: int):
        if connection.active:
            connection.active = False
            if self.connections.get(connection.world_id) is connection:
                self.connections.pop(connection.world_id, None)
            self.store.disconnected(connection.world_id, reason)
            for command_id in list(connection.pending):
                self._finish_pending(connection, command_id)
        async with connection.send_lock:
            if not connection.closed:
                connection.closed = True
                try:
                    await connection.websocket.close(code=code)
                except (RuntimeError, WebSocketDisconnect, OSError):
                    pass

    async def _send(self, connection: Connection):
        try:
            while connection.active:
                frame = await connection.queue.get()
                async with connection.send_lock:
                    if not connection.active:
                        break
                    if not self.store.mark_sent(frame["id"]):
                        continue
                    await connection.websocket.send_json(frame)
        except (RuntimeError, WebSocketDisconnect, OSError):
            await self._disconnect(connection, "connector_send_failed", 1011)

    async def serve(self, connection: Connection):
        sender = None
        try:
            await connection.websocket.accept()
            self.store.connected(connection.world_id)
            sender = asyncio.create_task(self._send(connection))
            while connection.active:
                message = await connection.websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("text")
                if raw is None:
                    raw = message.get("bytes")
                if not isinstance(raw, (str, bytes)):
                    raise InvalidInput("Invalid frame")
                byte_size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
                if byte_size > MAX_FRAME_BYTES:
                    await self._disconnect(connection, "frame_too_large", 1009)
                    return
                if not connection.active or self.connections.get(connection.world_id) is not connection:
                    break
                frame = parse_json(raw)
                if not isinstance(frame, dict):
                    raise InvalidInput("Invalid frame")
                if "worldId" in frame and frame["worldId"] != connection.world_id:
                    raise InvalidInput("World scope mismatch")
                if frame.get("type") == "snapshot":
                    world = self.store.world(connection.world_id)
                    if world is None:
                        raise InvalidInput("World is revoked")
                    normalized = normalize_snapshot(frame, world)
                    self.store.received_snapshot(connection.world_id, normalized)
                    connection.has_snapshot = True
                elif frame.get("type") == "result":
                    if "protocolVersion" in frame and (type(frame["protocolVersion"]) is not int or frame["protocolVersion"] != 1):
                        raise InvalidInput("Unsupported protocol version")
                    command_id = frame.get("id")
                    if not routing_id(command_id):
                        raise InvalidInput("Invalid command ID")
                    command = self.store.command(command_id)
                    if command is None or command["worldId"] != connection.world_id:
                        raise InvalidInput("Command scope mismatch")
                    ok, data, error = normalize_result(frame)
                    self.store.command_result(connection.world_id, command_id, ok, data, error)
                    self._finish_pending(connection, command_id)
                else:
                    raise InvalidInput("Unsupported frame")
        except (InvalidInput, ValueError, TypeError, RecursionError, UnicodeError):
            await self._disconnect(connection, "invalid_frame", 1008)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        finally:
            # ASGI may cancel the receiver as soon as the peer closes. Cleanup must
            # still mark pending commands uncertain and join the sender task.
            with anyio.CancelScope(shield=True):
                await self._disconnect(connection, "connector_disconnected", 1000)
                if sender is not None:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)

    async def close(self):
        for connection in list(self.connections.values()):
            await self._disconnect(connection, "hub_shutdown", 1001)
        tasks = list(self.deadlines.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
