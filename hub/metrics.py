"""Bounded, numeric-only metrics history, separate from the Hub command database.

All timestamps are UTC Unix milliseconds. Query buckets start at the requested
``from``; ``count`` is the sum of input weights, not a count of populated buckets.
Means use those weights; interval counts are summed without multiplying by weight.
No cross-world or cross-dimension rollup is inferred. Missing samples stay missing.

Resource limits are deliberate: 8,192 retained series, 1,024 series per world,
8,192 live boot high-water marks (128 per world), 1,000 entries per dimensions
list, 64 query series, and 64 tile worlds (at most three series each). Responses
set ``truncated`` when an output cap applies. New cardinality is rejected rather
than evicting an unexpired dedup record and accepting a replay. Retention and row
pruning remove unused series; boot high-water marks expire with retention.
Enrollment/tile metadata is one bounded-size row per currently enrolled world.

HUB_METRICS_RETENTION_DAYS is only the first-initialization default; later values
come from the persisted setting. HUB_METRICS_MAX_ROWS accepts 1..10,000,000 with a
1,000,000 default. The router permits one in-flight 64-KiB batch per socket and
32 globally; overload drops only history and increments rejectedBatches. The
diagnostic is flushed at maintenance, a successful batch, or clean shutdown.
Tiles add truncated when more than 64 enrolled worlds match. sampleIntervalSeconds
is the default collection interval, not a claim that every world reported it.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

from .normalize import conversation_routing_id
from .security import InvalidInput, WORLD_ID, canonical_json, routing_id

DAY_MS = 86_400_000
CLOCK_SKEW_MS = 300_000
MAX_BATCH_BYTES = 64 * 1024
MAX_BATCH_POINTS = 512
MAX_VALUE = 1e18
MAX_INTEGER = 2**63 - 1
MAX_STORED_SERIES = 8192
MAX_WORLD_SERIES = 1024
MAX_BOOTS = 8192
MAX_WORLD_BOOTS = 128
MAX_DIMENSIONS = 1000
MAX_QUERY_SERIES = 64
MAX_TILE_WORLDS = 64
DEFAULT_METRICS = ("gateway.cpu.percent", "gateway.memory.rss_bytes", "gateway.loops.active")
BOOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,63}\Z")
INTEGER_TEXT = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")


def _item(metric_id, name, unit, description, *, scope="world", aggregation="mean"):
    return {"id": metric_id, "name": name, "unit": unit, "scope": scope,
            "aggregation": aggregation, "description": description}


CATALOG = (
    _item("machine.cpu.percent", "Machine CPU", "%", "Host OS CPU view; not an inferred container limit."),
    _item("machine.memory.total_bytes", "Machine memory capacity", "bytes", "Total memory in the host OS view, not container capacity."),
    _item("machine.memory.used_bytes", "Machine memory used", "bytes", "Used memory in the host OS view."),
    _item("machine.memory.percent", "Machine memory used", "%", "Memory use in the host OS view."),
    _item("machine.disk.total_bytes", "Gateway filesystem capacity", "bytes", "Capacity of the gateway application filesystem, not world directory size."),
    _item("machine.disk.free_bytes", "Gateway filesystem free", "bytes", "Free space on the gateway application filesystem."),
    _item("machine.disk.percent", "Gateway filesystem used", "%", "Used capacity of the gateway application filesystem."),
    _item("container.cpu.percent", "Container CPU", "%", "CPU use relative to the available cgroup CPU limit, when supported."),
    _item("container.memory.total_bytes", "Container memory capacity", "bytes", "Memory capacity from the cgroup limit, when available."),
    _item("container.memory.used_bytes", "Container memory used", "bytes", "Memory use from cgroup accounting, when available."),
    _item("container.memory.percent", "Container memory used", "%", "Memory use relative to the cgroup limit, when available."),
    _item("gateway.cpu.percent", "Gateway CPU", "%", "Gateway process CPU, normalized by logical CPU count."),
    _item("gateway.memory.rss_bytes", "Gateway resident memory", "bytes", "Gateway process resident working set."),
    _item("gateway.memory.private_bytes", "Gateway private memory", "bytes", "Gateway process private memory, when supported."),
    _item("gateway.gc.heap_bytes", "Managed heap", "bytes", "Measured managed heap size; absent when unavailable."),
    _item("gateway.gc.pause_percent", "GC pause time", "%", "Measured managed garbage collection pause percentage."),
    _item("gateway.threads.count", "Gateway threads", "count", "Measured operating system process thread count."),
    _item("gateway.uptime.seconds", "Gateway uptime", "seconds", "Elapsed gateway process lifetime."),
    _item("gateway.io.read_bytes_per_second", "Gateway I/O read rate", "bytes/s", "Process OS I/O read rate, not database query activity."),
    _item("gateway.io.write_bytes_per_second", "Gateway I/O write rate", "bytes/s", "Process OS I/O write rate, not database query activity."),
    _item("gateway.loops.active", "Visible active loops", "count", "Active loops for allowed agents and visible top-level conversations only."),
    _item("gateway.questions.pending", "Visible pending questions", "count", "Pending questions in the visible allowed scope."),
    _item("gateway.conversations.visible", "Visible conversations", "count", "Visible allowed top-level conversations in the current snapshot."),
    _item("sqlite.connections.current", "SQLite connections", "count", "Passive logical connection count; not lock or integrity health."),
    _item("sqlite.connections.peak", "SQLite peak connections", "count", "Passive logical connection peak, when available."),
    _item("sqlite.connections.opened", "SQLite connections opened", "count", "Passive logical connections opened since process start."),
    _item("sqlite.connections.closed", "SQLite connections closed", "count", "Passive logical connections closed since process start."),
    _item("sqlite.memory.current_bytes", "SQLite native memory", "bytes", "Passive native allocator use, not SQL text or query health."),
    _item("sqlite.memory.peak_bytes", "SQLite peak native memory", "bytes", "Passive native allocator peak, when available."),
    _item("connector.collection.duration_ms", "Metrics collection duration", "ms", "Time spent collecting this connector sample."),
    _item("connector.runs.dropped", "Dropped run measurements", "count", "Numeric count of run measurements dropped by the bounded collector."),
    _item("agent.loops.active", "Visible active agent loops", "count", "Active loops for this exact visible conversation; agent-only zero is allowed when it has no visible conversations.", scope="agent"),
    _item("agent.loops.completed", "Completed loops", "count", "Completed terminal loops measured during the interval, including failed terminals.", scope="agent", aggregation="sum"),
    _item("agent.loops.duration_ms", "Completed loop duration", "ms", "Weighted mean duration of measured completed terminal loops, not just successes.", scope="agent"),
    _item("agent.loops.tools", "Measured tools per completed loop", "count", "Weighted mean of explicit non-null measured completed-result tool counts; no inferred token attribution.", scope="agent"),
)
CATALOG_BY_ID = {item["id"]: item for item in CATALOG}


class MetricsUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Metrics are unavailable")


def utc_ms() -> int:
    return time.time_ns() // 1_000_000


def catalog() -> dict:
    return {"items": [dict(item) for item in CATALOG], "defaultMetrics": list(DEFAULT_METRICS)}


def bounded_setting(value, minimum, maximum, name) -> int:
    if type(value) is int:
        result = value
    elif isinstance(value, str) and INTEGER_TEXT.fullmatch(value):
        result = int(value)
    else:
        raise RuntimeError(f"Invalid {name}")
    if not minimum <= result <= maximum:
        raise RuntimeError(f"Invalid {name}")
    return result


def database_path(hub_path, override=None) -> str:
    """Reject physical aliases before either database is opened.

    Separate :memory: connections do not share a database or create a file.
    SQLite URI filenames are not enabled by either store.
    """
    hub = str(hub_path)
    target = str(override) if override is not None else (":memory:" if hub == ":memory:" else str(Path(hub).with_name("metrics.sqlite")))
    if not target or target.startswith("file:") or hub.startswith("file:"):
        raise RuntimeError("Invalid metrics database path")
    if hub != ":memory:" and target != ":memory:":
        one, two = Path(hub).resolve(), Path(target).resolve()
        if os.path.normcase(str(one)) == os.path.normcase(str(two)) or (one.exists() and two.exists() and os.path.samefile(one, two)):
            raise RuntimeError("Hub and metrics databases must use different paths")
    return target


def _invalid_batch():
    raise InvalidInput("Invalid metrics batch")


def validate_batch(envelope, snapshot, now: int, retention_days: int) -> dict:
    """Validate the entire envelope before any supplied dimension is persisted."""
    required = {"schemaVersion", "bootId", "sequence", "observedAt", "points"}
    if not isinstance(envelope, dict) or not required <= envelope.keys() or envelope.keys() - required - {"intervalSeconds"}:
        _invalid_batch()
    if len(canonical_json(envelope).encode("utf-8")) > MAX_BATCH_BYTES:
        _invalid_batch()
    if type(envelope["schemaVersion"]) is not int or envelope["schemaVersion"] != 1:
        _invalid_batch()
    if not isinstance(envelope["bootId"], str) or not BOOT_ID.fullmatch(envelope["bootId"]):
        _invalid_batch()
    if type(envelope["sequence"]) is not int or not 1 <= envelope["sequence"] <= MAX_INTEGER:
        _invalid_batch()
    at, interval = envelope["observedAt"], envelope.get("intervalSeconds", 30)
    if type(at) is not int or not max(0, now - retention_days * DAY_MS) <= at <= min(MAX_INTEGER, now + CLOCK_SKEW_MS):
        _invalid_batch()
    if type(interval) is not int or not 15 <= interval <= 3600:
        _invalid_batch()
    points = envelope["points"]
    if not isinstance(points, list) or len(points) > MAX_BATCH_POINTS:
        _invalid_batch()
    agents = {item["id"] for item in snapshot.get("agents", [])}
    owners: dict[str, set[str]] = {}
    for item in (*snapshot.get("conversations", []), *snapshot.get("questions", [])):
        conversation = item.get("id") if "id" in item else item.get("conversationId")
        if item.get("agentId") in agents and conversation_routing_id(conversation):
            owners.setdefault(conversation, set()).add(item["agentId"])
    agents_with_conversations = {owner for values in owners.values() for owner in values}
    result, identities = [], set()
    for point in points:
        if not isinstance(point, dict) or not {"metricId", "value"} <= point.keys() or point.keys() - {"metricId", "value", "weight", "agentId", "conversationId"}:
            _invalid_batch()
        metric_id = point["metricId"]
        if not isinstance(metric_id, str) or metric_id not in CATALOG_BY_ID:
            _invalid_batch()
        value, weight = point["value"], point.get("weight", 1)
        if type(value) not in (int, float) or not 0 <= value <= MAX_VALUE or not math.isfinite(value):
            _invalid_batch()
        if CATALOG_BY_ID[metric_id]["unit"] == "%" and value > 100:
            _invalid_batch()
        if type(weight) is not int or not 1 <= weight <= 1_000_000:
            _invalid_batch()
        agent, conversation = point.get("agentId"), point.get("conversationId")
        if CATALOG_BY_ID[metric_id]["scope"] == "world":
            if agent is not None or conversation is not None:
                _invalid_batch()
        else:
            if not routing_id(agent) or agent not in agents:
                _invalid_batch()
            if conversation is not None:
                if not conversation_routing_id(conversation) or owners.get(conversation) != {agent}:
                    _invalid_batch()
            elif metric_id != "agent.loops.active" or value != 0 or agent in agents_with_conversations:
                # Store run measurements per conversation, never both an agent
                # total and its conversations. An idle agent may report a real 0.
                _invalid_batch()
        identity = (metric_id, agent or "", conversation or "")
        if identity in identities:
            _invalid_batch()
        identities.add(identity)
        result.append((*identity, float(value), weight))
    return {"bootId": envelope["bootId"], "sequence": envelope["sequence"], "observedAt": at, "intervalSeconds": interval, "points": result}


def query_arguments(params, now: int, retention_days: int) -> dict:
    """Strict HTTP parsing, without FastAPI reflecting hostile input in errors."""
    if set(params) - {"metricId", "worldId", "agentId", "conversationId", "from", "to", "maxPoints"}:
        raise InvalidInput("Invalid metrics query")
    metrics, worlds = params.getlist("metricId"), params.getlist("worldId")
    if not 1 <= len(metrics) <= 6 or any(item not in CATALOG_BY_ID for item in metrics):
        raise InvalidInput("Select one to six known metrics")
    if len(worlds) > MAX_QUERY_SERIES or any(not WORLD_ID.fullmatch(item) for item in worlds):
        raise InvalidInput("Invalid metrics world filter")
    for name in ("agentId", "conversationId", "from", "to", "maxPoints"):
        if len(params.getlist(name)) > 1:
            raise InvalidInput("Invalid metrics query")
    agent, conversation = params.get("agentId"), params.get("conversationId")
    if agent is not None and not routing_id(agent):
        raise InvalidInput("Invalid metrics agent filter")
    if conversation is not None and not conversation_routing_id(conversation):
        raise InvalidInput("Invalid metrics conversation filter")

    def integer(name, default, minimum=0, maximum=MAX_INTEGER):
        value = params.get(name)
        if value is None:
            return default
        if not INTEGER_TEXT.fullmatch(value):
            raise InvalidInput("Invalid metrics query range")
        result = int(value)
        if not minimum <= result <= maximum:
            raise InvalidInput("Invalid metrics query range")
        return result

    start, end = integer("from", max(0, now - retention_days * DAY_MS)), integer("to", now + 1)
    max_points = integer("maxPoints", 240, 16, 720)
    if start >= end:
        raise InvalidInput("Invalid metrics query range")
    return {"metric_ids": list(dict.fromkeys(metrics)), "world_ids": list(dict.fromkeys(worlds)),
            "agent_id": agent, "conversation_id": conversation, "start": start, "end": end, "max_points": max_points}


class MetricsStore:
    def __init__(self, path, *, retention_days=7, max_rows=1_000_000, clock=None, maintenance_seconds=60):
        self.path = str(path)
        initial_retention = bounded_setting(retention_days, 1, 365, "metrics retention")
        self.max_rows = bounded_setting(max_rows, 1, 10_000_000, "metrics row limit")
        if not 0 < maintenance_seconds <= 3600:
            raise RuntimeError("Invalid metrics maintenance interval")
        self.maintenance_seconds = maintenance_seconds
        self.clock = clock or utc_ms
        self.lock = threading.RLock()
        self._diagnostic_lock = threading.Lock()
        self._closed = False
        self._blocked_worlds: set[str] = set()
        self._enrollments: dict[str, str] = {}
        self._rejected_batches = 0
        self.db = None
        try:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path, timeout=0.2, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA busy_timeout=200")
            self.db.execute("PRAGMA journal_size_limit=8388608")
            self.db.execute("PRAGMA wal_autocheckpoint=256")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    retention_days INTEGER NOT NULL CHECK(retention_days BETWEEN 1 AND 365),
                    rejected_batches INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS enrollments (
                    world_id TEXT PRIMARY KEY,
                    enrollment TEXT NOT NULL,
                    tile_metrics TEXT
                );
                CREATE TABLE IF NOT EXISTS series (
                    id INTEGER PRIMARY KEY,
                    world_id TEXT NOT NULL REFERENCES enrollments(world_id) ON DELETE CASCADE,
                    metric_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    UNIQUE(world_id,metric_id,agent_id,conversation_id)
                );
                CREATE INDEX IF NOT EXISTS series_metric ON series(metric_id,world_id,agent_id,conversation_id);
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY,
                    series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                    observed_at INTEGER NOT NULL,
                    value REAL NOT NULL,
                    weight INTEGER NOT NULL,
                    interval_seconds INTEGER NOT NULL DEFAULT 30
                );
                CREATE INDEX IF NOT EXISTS samples_time ON samples(observed_at,id);
                CREATE INDEX IF NOT EXISTS samples_series_time ON samples(series_id,observed_at);
                CREATE TABLE IF NOT EXISTS boots (
                    world_id TEXT NOT NULL REFERENCES enrollments(world_id) ON DELETE CASCADE,
                    boot_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    last_at INTEGER NOT NULL,
                    PRIMARY KEY(world_id,boot_id)
                );
                CREATE INDEX IF NOT EXISTS boots_time ON boots(last_at);
            """)
            if "interval_seconds" not in {row[1] for row in self.db.execute("PRAGMA table_info(samples)")}:
                self.db.execute("ALTER TABLE samples ADD COLUMN interval_seconds INTEGER NOT NULL DEFAULT 30")
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO settings(id,retention_days) VALUES(1,?)", (initial_retention,))
            settings = self.db.execute("SELECT retention_days,rejected_batches FROM settings WHERE id=1").fetchone()
            self.retention_days = settings["retention_days"]
            self._rejected_batches = settings["rejected_batches"]
            self._row_count = self.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            self._enrollments = {row["world_id"]: row["enrollment"] for row in self.db.execute("SELECT world_id,enrollment FROM enrollments")}
            self.prune()
        except Exception:
            if self.db is not None:
                self.db.close()
            self._closed = True
            raise MetricsUnavailable() from None

    @contextmanager
    def _access(self):
        with self.lock:
            if self._closed:
                raise MetricsUnavailable()
            try:
                yield
            except InvalidInput:
                raise
            except Exception:
                raise MetricsUnavailable() from None

    def enrollment(self, world_id):
        with self.lock:
            return None if world_id in self._blocked_worlds else self._enrollments.get(world_id)

    def sync_enrollments(self, current: dict[str, str]):
        """Startup reconciliation also handles deletion while metrics were unavailable."""
        with self._access():
            self._blocked_worlds.update(set(self._enrollments) - current.keys())
            self._blocked_worlds.update(world for world, key in current.items() if self._enrollments.get(world) != key)
            with self.db:
                for world in set(self._enrollments) - current.keys():
                    self.db.execute("DELETE FROM enrollments WHERE world_id=?", (world,))
                for world, key in current.items():
                    if self._enrollments.get(world) != key:
                        self.db.execute("DELETE FROM enrollments WHERE world_id=?", (world,))
                        self.db.execute("INSERT INTO enrollments(world_id,enrollment) VALUES(?,?)", (world, key))
            self._row_count = self.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            self._enrollments = dict(current)
            self._blocked_worlds.clear()

    def register_world(self, world_id: str, enrollment: str, *, preserve=False):
        with self._access():
            can_preserve = preserve and world_id in self._enrollments and world_id not in self._blocked_worlds
            self._blocked_worlds.add(world_id)
            with self.db:
                if can_preserve:
                    self.db.execute("UPDATE enrollments SET enrollment=? WHERE world_id=?", (enrollment, world_id))
                else:
                    self.db.execute("DELETE FROM enrollments WHERE world_id=?", (world_id,))
                    self.db.execute("INSERT INTO enrollments(world_id,enrollment) VALUES(?,?)", (world_id, enrollment))
            self._enrollments[world_id] = enrollment
            self._row_count = self.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            self._blocked_worlds.discard(world_id)

    def delete_world(self, world_id):
        # Block reads before touching SQLite, including on a failed purge.
        with self.lock:
            self._blocked_worlds.add(world_id)
        with self._access():
            with self.db:
                self.db.execute("DELETE FROM enrollments WHERE world_id=?", (world_id,))
                count = self.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            self._row_count = count
            self._enrollments.pop(world_id, None)
            self._blocked_worlds.discard(world_id)

    def reject_batch(self):
        # Called by the receiver when its bounded history worker is occupied.
        # Never wait for the metrics database or a history query on this path.
        with self._diagnostic_lock:
            self._rejected_batches = min(MAX_INTEGER, self._rejected_batches + 1)

    def _persist_diagnostics_locked(self):
        with self._diagnostic_lock:
            rejected = self._rejected_batches
        self.db.execute("UPDATE settings SET rejected_batches=? WHERE id=1", (rejected,))

    def ingest(self, world_id, enrollment, envelope, snapshot) -> bool:
        """A metrics failure cannot escape into the snapshot/command receiver."""
        if envelope is None:
            return False
        try:
            with self._access():
                batch = validate_batch(envelope, snapshot, self.clock(), self.retention_days)
                if enrollment is None or world_id in self._blocked_worlds or self._enrollments.get(world_id) != enrollment:
                    _invalid_batch()
                with self.db:
                    boot = self.db.execute("SELECT sequence FROM boots WHERE world_id=? AND boot_id=?", (world_id, batch["bootId"])).fetchone()
                    if boot is not None and batch["sequence"] <= boot["sequence"]:
                        return False
                    if boot is None:
                        if self.db.execute("SELECT COUNT(*) FROM boots").fetchone()[0] >= MAX_BOOTS or self.db.execute("SELECT COUNT(*) FROM boots WHERE world_id=?", (world_id,)).fetchone()[0] >= MAX_WORLD_BOOTS:
                            _invalid_batch()
                    series_count = self.db.execute("SELECT COUNT(*) FROM series").fetchone()[0]
                    world_count = self.db.execute("SELECT COUNT(*) FROM series WHERE world_id=?", (world_id,)).fetchone()[0]
                    for metric, agent, conversation, value, weight in batch["points"]:
                        row = self.db.execute("SELECT id FROM series WHERE world_id=? AND metric_id=? AND agent_id=? AND conversation_id=?", (world_id, metric, agent, conversation)).fetchone()
                        if row is None:
                            if series_count >= MAX_STORED_SERIES or world_count >= MAX_WORLD_SERIES:
                                _invalid_batch()
                            series_id = self.db.execute("INSERT INTO series(world_id,metric_id,agent_id,conversation_id) VALUES(?,?,?,?)", (world_id, metric, agent, conversation)).lastrowid
                            series_count += 1
                            world_count += 1
                        else:
                            series_id = row["id"]
                        self.db.execute("INSERT INTO samples(series_id,observed_at,value,weight,interval_seconds) VALUES(?,?,?,?,?)", (series_id, batch["observedAt"], value, weight, batch["intervalSeconds"]))
                    self.db.execute("""INSERT INTO boots(world_id,boot_id,sequence,last_at) VALUES(?,?,?,?)
                        ON CONFLICT(world_id,boot_id) DO UPDATE SET sequence=excluded.sequence,last_at=MAX(boots.last_at,excluded.last_at)""", (world_id, batch["bootId"], batch["sequence"], batch["observedAt"]))
                    count = self._prune_locked(self.clock(), self._row_count + len(batch["points"]))
                self._row_count = count
            return True
        except Exception:
            self.reject_batch()
            return False

    def _prune_locked(self, now, count):
        self._persist_diagnostics_locked()
        cutoff = max(0, now - self.retention_days * DAY_MS)
        removed = self.db.execute("DELETE FROM samples WHERE observed_at<? OR observed_at>?", (cutoff, now + CLOCK_SKEW_MS)).rowcount
        count -= removed
        if count > self.max_rows:
            removed += self.db.execute("DELETE FROM samples WHERE id IN (SELECT id FROM samples ORDER BY observed_at,id LIMIT ?)", (count - self.max_rows,)).rowcount
            count = self.max_rows
        if removed:
            self.db.execute("DELETE FROM series WHERE NOT EXISTS (SELECT 1 FROM samples WHERE samples.series_id=series.id)")
        self.db.execute("DELETE FROM boots WHERE last_at<? OR last_at>?", (cutoff, now + CLOCK_SKEW_MS))
        return count

    def _release_space(self):
        # Freed pages are reused immediately; bounded incremental vacuum and WAL
        # truncation also give unused disk space back during idle maintenance.
        self.db.execute("PRAGMA incremental_vacuum(256)")
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def prune(self):
        with self._access():
            with self.db:
                count = self._prune_locked(self.clock(), self._row_count)
            self._row_count = count
            self._release_space()

    async def maintain(self, stop: asyncio.Event):
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.maintenance_seconds)
            except asyncio.TimeoutError:
                try:
                    await asyncio.to_thread(self.prune)
                except MetricsUnavailable:
                    # No raw exception or payload is logged. Retry only periodic
                    # read/storage maintenance, never an operator mutation.
                    pass

    def settings(self):
        with self._access():
            oldest = self.db.execute("SELECT MIN(observed_at) FROM samples").fetchone()[0]
            newest = self.db.execute("SELECT MAX(observed_at) FROM samples").fetchone()[0]
            size = 0
            if self.path != ":memory:":
                size = sum(path.stat().st_size for path in (Path(self.path), Path(self.path + "-wal"), Path(self.path + "-shm")) if path.exists())
            return {"retentionDays": self.retention_days, "maxRows": self.max_rows,
                    "sampleIntervalSeconds": 30, "rowCount": self._row_count,
                    "databaseBytes": size, "oldestAt": oldest, "newestAt": newest,
                    "rejectedBatches": self._rejected_batches}

    def set_retention(self, days):
        if type(days) is not int or not 1 <= days <= 365:
            raise InvalidInput("Invalid metrics retention")
        with self._access():
            previous = self.retention_days
            try:
                with self.db:
                    self.db.execute("UPDATE settings SET retention_days=? WHERE id=1", (days,))
                    self.retention_days = days
                    count = self._prune_locked(self.clock(), self._row_count)
            except Exception:
                self.retention_days = previous
                raise
            self._row_count = count
            self._release_space()
            return self.settings()

    def _world_filter(self, world_ids=None):
        allowed = sorted(set(self._enrollments) - self._blocked_worlds)
        if world_ids:
            allowed = sorted(set(allowed).intersection(world_ids))
        return allowed

    def dimensions(self, world_id=None):
        with self._access():
            allowed = self._world_filter([world_id] if world_id is not None else None)
            if not allowed:
                return {"worlds": [], "agents": [], "conversations": [], "truncated": False}
            # Joining the current enrollment registry avoids an unbounded IN list.
            where, parameters = "", []
            if world_id is not None:
                where = " AND s.world_id=?"
                parameters.append(world_id)
            blocked = self._blocked_worlds
            agents, conversations = [], []
            for row in self.db.execute("SELECT DISTINCT s.world_id,s.agent_id FROM series s JOIN enrollments e ON e.world_id=s.world_id WHERE s.agent_id<>''" + where + " ORDER BY s.world_id,s.agent_id LIMIT ?", (*parameters, MAX_DIMENSIONS + len(blocked) * MAX_WORLD_SERIES + 1)):
                if row["world_id"] not in blocked:
                    agents.append({"worldId": row["world_id"], "agentId": row["agent_id"]})
                    if len(agents) > MAX_DIMENSIONS:
                        break
            for row in self.db.execute("SELECT DISTINCT s.world_id,s.agent_id,s.conversation_id FROM series s JOIN enrollments e ON e.world_id=s.world_id WHERE s.conversation_id<>''" + where + " ORDER BY s.world_id,s.agent_id,s.conversation_id LIMIT ?", (*parameters, MAX_DIMENSIONS + len(blocked) * MAX_WORLD_SERIES + 1)):
                if row["world_id"] not in blocked:
                    conversations.append({"worldId": row["world_id"], "agentId": row["agent_id"], "conversationId": row["conversation_id"]})
                    if len(conversations) > MAX_DIMENSIONS:
                        break
            return {"worlds": allowed[:MAX_DIMENSIONS], "agents": agents[:MAX_DIMENSIONS],
                    "conversations": conversations[:MAX_DIMENSIONS],
                    "truncated": max(len(allowed), len(agents), len(conversations)) > MAX_DIMENSIONS}

    def query(self, *, metric_ids, world_ids=None, agent_id=None, conversation_id=None, start=None, end=None, max_points=240):
        with self._access():
            now = self.clock()
            start = max(0, now - self.retention_days * DAY_MS) if start is None else start
            end = now + 1 if end is None else end
            if not 1 <= len(metric_ids) <= 6 or any(metric not in CATALOG_BY_ID for metric in metric_ids) or type(start) is not int or type(end) is not int or not 0 <= start < end <= MAX_INTEGER or type(max_points) is not int or not 16 <= max_points <= 720:
                raise InvalidInput("Invalid metrics query")
            bucket = max(1, (end - start + max_points - 1) // max_points)
            result = {"from": start, "to": end, "retentionDays": self.retention_days, "bucketMs": bucket, "series": [], "truncated": False}
            allowed = set(self._world_filter(world_ids))
            if not allowed:
                return result
            conditions = ["s.metric_id IN (" + ",".join("?" for _ in metric_ids) + ")", "EXISTS (SELECT 1 FROM samples p WHERE p.series_id=s.id AND p.observed_at>=? AND p.observed_at<?)"]
            parameters = [*metric_ids, max(start, now - self.retention_days * DAY_MS), end]
            if world_ids:
                if len(world_ids) > MAX_QUERY_SERIES:
                    raise InvalidInput("Invalid metrics world filter")
                conditions.append("s.world_id IN (" + ",".join("?" for _ in world_ids) + ")")
                parameters.extend(world_ids)
            if agent_id is not None:
                conditions.append("s.agent_id=?")
                parameters.append(agent_id)
            if conversation_id is not None:
                conditions.append("s.conversation_id=?")
                parameters.append(conversation_id)
            # An agent/conversation filter never falls back to world aggregates.
            candidates = []
            for row in self.db.execute("SELECT s.* FROM series s JOIN enrollments e ON e.world_id=s.world_id WHERE " + " AND ".join(conditions) + " ORDER BY s.world_id,s.metric_id,s.agent_id,s.conversation_id LIMIT ?", (*parameters, MAX_STORED_SERIES)):
                if row["world_id"] in allowed:
                    candidates.append(row)
                    if len(candidates) > MAX_QUERY_SERIES:
                        break
            result["truncated"] = len(candidates) > MAX_QUERY_SERIES
            for row in candidates[:MAX_QUERY_SERIES]:
                aggregation = "SUM(value)" if CATALOG_BY_ID[row["metric_id"]]["aggregation"] == "sum" else "SUM(value*weight)/SUM(weight)"
                points = self.db.execute("SELECT CAST((observed_at-?)/? AS INTEGER) AS bucket," + aggregation + " AS value,SUM(weight) AS count,MAX(observed_at) AS latest,MAX(interval_seconds) AS cadence FROM samples WHERE series_id=? AND observed_at>=? AND observed_at<? GROUP BY bucket ORDER BY bucket LIMIT ?", (start, bucket, row["id"], max(start, now - self.retention_days * DAY_MS), end, max_points)).fetchall()
                if points:
                    result["series"].append({"metricId": row["metric_id"], "worldId": row["world_id"],
                        "agentId": row["agent_id"] or None, "conversationId": row["conversation_id"] or None,
                        "points": [{"timestamp": start + point["bucket"] * bucket, "value": point["value"], "count": point["count"]} for point in points],
                        "latestAt": max(point["latest"] for point in points),
                        "sampleIntervalSeconds": max(point["cadence"] for point in points)})
            return result

    def set_tile_metrics(self, world_id, metrics):
        if not isinstance(metrics, list) or not 1 <= len(metrics) <= 3 or any(not isinstance(metric, str) or metric not in CATALOG_BY_ID or CATALOG_BY_ID[metric]["scope"] != "world" for metric in metrics) or len(set(metrics)) != len(metrics):
            raise InvalidInput("Select one to three world metrics")
        with self._access(), self.db:
            if world_id not in self._world_filter([world_id]):
                raise MetricsUnavailable()
            self.db.execute("UPDATE enrollments SET tile_metrics=? WHERE world_id=?", (canonical_json(metrics), world_id))
        return {"worldId": world_id, "metrics": list(metrics)}

    def tiles(self, world_id=None):
        with self._access():
            now = self.clock()
            start, end = max(0, now - 3_600_000), now + 1
            allowed = self._world_filter([world_id] if world_id is not None else None)
            result = {"from": start, "to": end, "metrics": list(DEFAULT_METRICS), "worlds": {}, "retentionDays": self.retention_days, "truncated": len(allowed) > MAX_TILE_WORLDS}
            for world in allowed[:MAX_TILE_WORLDS]:
                row = self.db.execute("SELECT tile_metrics FROM enrollments WHERE world_id=?", (world,)).fetchone()
                selected = json.loads(row[0]) if row[0] else list(DEFAULT_METRICS)
                query = self.query(metric_ids=selected, world_ids=[world], start=start, end=end, max_points=60)
                result["worlds"][world] = {"metrics": selected, "series": query["series"]}
                result["truncated"] |= query["truncated"]
            return result

    def close(self):
        with self.lock:
            if not self._closed:
                try:
                    with self.db:
                        self._persist_diagnostics_locked()
                    self._release_space()
                except (sqlite3.Error, OSError):
                    pass
                finally:
                    self._closed = True
                    self.db.close()
