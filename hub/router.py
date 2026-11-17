"""One active connector per world and an in-memory, never-replayed send queue."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json

import anyio
from starlette.websockets import WebSocket, WebSocketDisconnect

from .metrics import MAX_BATCH_BYTES
from .security import InvalidInput, MAX_FRAME_BYTES, parse_json, routing_id
from .normalize import normalize_result, normalize_snapshot


@dataclass(eq=False)
class Connection:
    world_id: str
    websocket: WebSocket
    enrollment: str | None = None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: bool = True
    has_snapshot: bool = False
    closed: bool = False
    pending: set[str] = field(default_factory=set)
    metrics_task: asyncio.Task | None = None


def _parse_frame(raw):
    """Separate the optional envelope before strict, bounded v1 parsing.

    Scan only JSON boundaries, without recursively decoding metrics. This permits
    actual wire-byte limits and isolates deep/oversized/non-finite metrics while
    always applying the original strict parser to every other snapshot field.
    Invalid JSON syntax or mismatched delimiters still fail the complete frame.
    No data from a rejected envelope is copied into a stored snapshot.
    """
    try:
        source = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        decoder = json.JSONDecoder()

        def whitespace(index):
            while index < len(source) and source[index] in " \t\r\n":
                index += 1
            return index

        def value_end(index):
            stack, quoted, escaped = [], False, False
            start = index
            while index < len(source):
                char = source[index]
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                elif char == '"':
                    quoted = True
                elif char in "[{":
                    stack.append("]" if char == "[" else "}")
                elif char in "]}":
                    if not stack:
                        break
                    if stack.pop() != char:
                        raise InvalidInput("Invalid frame")
                elif char == "," and not stack:
                    break
                index += 1
            if stack or quoted or index == start:
                raise InvalidInput("Invalid frame")
            return index

        index = whitespace(0)
        if index >= len(source) or source[index] != "{":
            return parse_json(raw)
        fields, envelopes = [], []
        index = whitespace(index + 1)
        if index < len(source) and source[index] == "}":
            return parse_json(raw)
        while index < len(source):
            start = index
            if source[index] != '"':
                raise InvalidInput("Invalid frame")
            key, index = decoder.raw_decode(source, index)
            if not isinstance(key, str):
                raise InvalidInput("Invalid frame")
            index = whitespace(index)
            if index >= len(source) or source[index] != ":":
                raise InvalidInput("Invalid frame")
            value_start = whitespace(index + 1)
            index = value_end(value_start)
            if key == "metrics":
                envelopes.append(source[value_start:index])
            else:
                fields.append(source[start:index])
            if index >= len(source):
                raise InvalidInput("Invalid frame")
            if source[index] == "}":
                if whitespace(index + 1) != len(source):
                    raise InvalidInput("Invalid frame")
                break
            if source[index] != ",":
                raise InvalidInput("Invalid frame")
            index = whitespace(index + 1)
        else:
            raise InvalidInput("Invalid frame")
        frame = parse_json("{" + ",".join(fields) + "}")
        if not envelopes or frame.get("type") != "snapshot":
            return parse_json(raw)
        if len(envelopes) != 1 or len(envelopes[0].encode("utf-8")) > MAX_BATCH_BYTES:
            frame["metrics"] = False
        else:
            try:
                frame["metrics"] = parse_json(envelopes[0])
            except InvalidInput:
                # Preserve syntax errors as invalid frames. Well-formed JSON
                # that violates the numeric/depth/Unicode budget is metrics-only.
                try:
                    json.loads(envelopes[0])
                except json.JSONDecodeError:
                    raise InvalidInput("Invalid frame") from None
                except (ValueError, RecursionError):
                    pass
                frame["metrics"] = False
        return frame
    except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError):
        raise InvalidInput("Invalid frame") from None


class Router:
    def __init__(self, store, command_timeout: float, *, metrics=None):
        self.store = store
        self.metrics = metrics
        self.command_timeout = command_timeout
        self.connections: dict[str, Connection] = {}
        self.deadlines: dict[str, asyncio.Task] = {}
        self.metrics_tasks: set[asyncio.Task] = set()

    def connected(self, world_id: str) -> bool:
        connection = self.connections.get(world_id)
        return connection is not None and connection.active

    def ready(self, world_id: str) -> bool:
        connection = self.connections.get(world_id)
        return connection is not None and connection.active and connection.has_snapshot

    def claim(self, world_id: str, websocket: WebSocket, *, enrollment=None) -> Connection | None:
        if self.connected(world_id):
            return None
        connection = Connection(world_id, websocket, enrollment=enrollment)
        self.connections[world_id] = connection
        return connection

    async def _ingest_metrics(self, connection, envelope, snapshot):
        try:
            await asyncio.to_thread(self.metrics.ingest, connection.world_id, connection.enrollment, envelope, snapshot)
        except Exception:
            # Neither storage errors nor diagnostics can affect command routing.
            self.metrics.reject_batch()

    def _record_metrics(self, connection, envelope, snapshot):
        if self.metrics is None or envelope is None:
            return
        # No unbounded queue and no waiting in the WebSocket receiver: one
        # in-flight batch per socket, at most 32 globally. Overload is diagnosed.
        if (connection.metrics_task is not None and not connection.metrics_task.done()) or len(self.metrics_tasks) >= 32:
            self.metrics.reject_batch()
            return
        task = asyncio.create_task(self._ingest_metrics(connection, envelope, snapshot))
        connection.metrics_task = task
        self.metrics_tasks.add(task)
        task.add_done_callback(self.metrics_tasks.discard)

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
                frame = _parse_frame(raw)
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
                    self._record_metrics(connection, frame.get("metrics"), normalized)
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
        # Do not cancel to_thread writes and close their SQLite connection while
        # a worker still uses it. At most 32 bounded batches must finish here.
        if self.metrics_tasks:
            await asyncio.gather(*self.metrics_tasks, return_exceptions=True)
