"""Synthetic metrics history tests. No running world, external API or host state."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hub import metrics as metrics_module
from hub.app import create_app
from hub.metrics import (
    CATALOG, CLOCK_SKEW_MS, DAY_MS, DEFAULT_METRICS, MAX_INTEGER,
    MetricsStore, MetricsUnavailable, catalog, database_path,
)
from hub.normalize import normalize_snapshot
from hub.router import _parse_frame
from hub.security import InvalidInput

NOW = 1_794_931_200_000


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def snapshot(world_id="one", *, agent="helper", conversation="c_visible"):
    return {
        "type": "snapshot", "protocolVersion": 1, "worldId": world_id,
        "agents": [{"id": agent}],
        "conversations": [{"id": conversation, "agentId": agent, "status": "active"}],
        "questions": [],
    }


def normalized(frame=None):
    frame = frame or snapshot()
    return normalize_snapshot(frame, {"id": frame["worldId"], "name": "Synthetic", "baseUrl": "https://synthetic.example"})


def point(metric="gateway.cpu.percent", value=0, **kwargs):
    return {"metricId": metric, "value": value, **kwargs}


def batch(points=None, *, at=NOW, sequence=1, boot="boot-one", **kwargs):
    return {"schemaVersion": 1, "bootId": boot, "sequence": sequence,
            "observedAt": at, "intervalSeconds": 30,
            "points": [point()] if points is None else points, **kwargs}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    instance = MetricsStore(tmp_path / "metrics.sqlite", clock=clock)
    instance.register_world("one", "enrollment-one")
    yield instance
    instance.close()


def ingest(store, points=None, *, scope=None, world="one", enrollment=None, **kwargs):
    return store.ingest(world, enrollment or "enrollment-" + world, batch(points, **kwargs), normalized(scope or snapshot(world)))


def query(store, *metric_ids, **kwargs):
    return store.query(metric_ids=list(metric_ids or DEFAULT_METRICS), start=NOW - DAY_MS, end=NOW + 1, **kwargs)


@pytest.fixture
def app_settings(tmp_path):
    return {"db_path": tmp_path / "hub.sqlite", "public_url": "http://127.0.0.1",
            "connector_url": "http://127.0.0.1:8081", "command_timeout": 1}


@pytest.fixture
def auth():
    return {"X-Hub-Request": "1", "Origin": "http://127.0.0.1"}


@pytest.fixture
def client(app_settings):
    with TestClient(create_app(**app_settings), base_url="http://127.0.0.1", client=("127.0.0.1", 51000)) as instance:
        yield instance


def register(client, auth, world="one"):
    response = client.post("/api/worlds", json={"id": world, "name": "Synthetic", "baseUrl": "https://synthetic.example"}, headers=auth)
    assert response.status_code == 201, response.text
    return response.json()


def connect(client, world):
    return client.websocket_connect("ws://127.0.0.1/api/connect/" + world["id"], headers={"Authorization": "Bearer " + world["token"]})


def barrier(client, auth, ws, *, world="one"):
    """Prove the receive path is live, independently of metrics persistence."""
    response = client.post("/api/worlds/" + world + "/commands", headers={**auth, "Idempotency-Key": str(time.time_ns())},
                           json={"action": "start_conversation", "payload": {"agentId": "helper", "message": "Synthetic"}})
    assert response.status_code == 202, response.text
    command = ws.receive_json()
    ws.send_json({"type": "result", "id": command["id"], "ok": True, "data": {"status": "accepted"}})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        resource = client.get("/api/commands/" + command["id"]).json()
        if resource["status"] == "succeeded":
            return
        time.sleep(0.005)
    pytest.fail("Synthetic command result was not received")


def publish(client, auth, ws, envelope, *, world="one", scope=None):
    frame = scope or snapshot(world)
    if envelope is not ...:
        frame["metrics"] = envelope
    ws.send_json(frame)
    # Wait for the snapshot before submitting the ordering barrier.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        worlds = client.get("/api/worlds").json()["items"]
        if any(item["id"] == world and item["snapshot"] is not None for item in worlds):
            break
        time.sleep(0.005)
    else:
        pytest.fail("Synthetic snapshot was not received")
    barrier(client, auth, ws, world=world)
    # Persistence is intentionally outside the receive loop. Explicitly join
    # its bounded task in tests rather than relying on thread scheduling speed.
    async def join_metrics():
        tasks = list(client.app.state.router.metrics_tasks)
        if tasks:
            await asyncio.gather(*tasks)
    client.portal.call(join_metrics)


def test_catalog_is_fixed_numeric_described_and_honest(client):
    response = client.get("/api/metrics/catalog")
    assert response.status_code == 200
    result = response.json()
    assert result == catalog()
    assert result["defaultMetrics"] == list(DEFAULT_METRICS)
    items = {item["id"]: item for item in result["items"]}
    assert len(items) == 35
    assert {item["scope"] for item in items.values()} == {"world", "agent"}
    assert [item["id"] for item in items.values() if item["aggregation"] == "sum"] == ["agent.loops.completed"]
    assert all(set(item) == {"id", "name", "unit", "scope", "aggregation", "description"} and item["description"] for item in items.values())
    assert "host OS" in items["machine.memory.total_bytes"]["description"]
    assert "cgroup" in items["container.memory.total_bytes"]["description"]
    assert "filesystem" in items["machine.disk.total_bytes"]["description"]
    assert "not database" in items["gateway.io.read_bytes_per_second"]["description"]
    assert "not lock" in items["sqlite.connections.current"]["description"]
    assert "no-store" in response.headers["cache-control"]


def test_zero_is_real_and_unavailable_is_missing(store):
    assert ingest(store)
    result = query(store)
    assert len(result["series"]) == 1
    assert result["series"][0]["metricId"] == "gateway.cpu.percent"
    assert result["series"][0]["points"][0]["value"] == 0
    assert result["series"][0]["points"][0]["count"] == 1
    assert result["series"][0]["agentId"] is result["series"][0]["conversationId"] is None
    assert store.ingest("one", "enrollment-one", None, normalized()) is False
    assert store.settings()["rowCount"] == 1 and store.settings()["rejectedBatches"] == 0


def test_weighted_means_and_completed_interval_sums_keep_exact_dimensions(store):
    run = {"agentId": "helper", "conversationId": "c_visible"}
    assert ingest(store, [point("agent.loops.duration_ms", 10, weight=1, **run), point("agent.loops.completed", 2, weight=3, **run)], at=NOW - 2)
    assert ingest(store, [point("agent.loops.duration_ms", 30, weight=3, **run), point("agent.loops.completed", 4, weight=2, **run)], at=NOW - 1, sequence=2)
    result = query(store, "agent.loops.duration_ms", "agent.loops.completed", agent_id="helper", max_points=16)
    by_metric = {item["metricId"]: item for item in result["series"]}
    assert by_metric["agent.loops.duration_ms"]["points"] == [{"timestamp": NOW - DAY_MS + 15 * result["bucketMs"], "value": 25.0, "count": 4}]
    assert by_metric["agent.loops.completed"]["points"][0]["value"] == 6
    assert by_metric["agent.loops.completed"]["points"][0]["count"] == 5
    assert all(item["worldId"] == "one" and item["conversationId"] == "c_visible" for item in result["series"])


def test_no_cross_world_sum_or_dimension_fallback(store):
    store.register_world("two", "enrollment-two")
    assert ingest(store, [point(value=20)])
    assert ingest(store, [point(value=80)], world="two")
    assert [item["points"][0]["value"] for item in query(store)["series"]] == [20, 80]
    assert query(store, agent_id="helper")["series"] == []
    assert query(store, conversation_id="c_visible")["series"] == []
    assert query(store, world_ids=["unknown"])["series"] == []
    assert len(query(store, world_ids=["one"])["series"]) == 1


def test_boot_sequence_dedup_is_persistent_and_world_scoped(tmp_path, clock):
    path = tmp_path / "metrics.sqlite"
    store = MetricsStore(path, clock=clock)
    store.register_world("one", "enrollment-one")
    store.register_world("two", "enrollment-two")
    assert ingest(store, sequence=10)
    assert not ingest(store, [point(value=99)], sequence=10)
    assert not ingest(store, [point(value=99)], sequence=9)
    assert ingest(store, sequence=1, boot="boot-two")
    assert ingest(store, sequence=10, world="two")
    store.close()
    restarted = MetricsStore(path, clock=clock)
    try:
        assert not ingest(restarted, sequence=10)
        assert ingest(restarted, sequence=11)
        assert restarted.settings()["rowCount"] == 4
        assert restarted.settings()["rejectedBatches"] == 0
    finally:
        restarted.close()


@pytest.mark.parametrize("value", [True, False, None, "12", -1, 1e18 + 1024, float("nan"), float("inf"), {}, []])
def test_non_numeric_or_out_of_range_values_are_atomic_rejections(store, value):
    assert not ingest(store, [point(), point("gateway.memory.rss_bytes", value)])
    assert store.settings()["rowCount"] == 0
    assert store.settings()["rejectedBatches"] == 1
    assert store.db.execute("SELECT COUNT(*) FROM boots").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 0


@pytest.mark.parametrize("change", [
    {"schemaVersion": 2}, {"schemaVersion": True}, {"sequence": True}, {"sequence": 0},
    {"sequence": MAX_INTEGER + 1}, {"observedAt": "1794931200000"}, {"observedAt": True},
    {"observedAt": NOW + CLOCK_SKEW_MS + 1}, {"observedAt": NOW - 7 * DAY_MS - 1},
    {"intervalSeconds": 14}, {"intervalSeconds": 3601}, {"intervalSeconds": 30.0},
    {"bootId": "x" * 65}, {"bootId": "../secret"}, {"bootId": "<script>"},
    {"points": {}}, {"points": None}, {"worldId": "two"}, {"secret": "NEVER_PERSIST"},
])
def test_invalid_envelopes_never_persist_user_data(store, change):
    envelope = {**batch(), **change}
    assert not store.ingest("one", "enrollment-one", envelope, normalized())
    assert store.settings()["rowCount"] == 0
    assert store.settings()["rejectedBatches"] == 1
    dump = "\n".join(store.db.iterdump())
    assert "NEVER_PERSIST" not in dump and "<script>" not in dump
    assert store.db.execute("SELECT COUNT(*) FROM boots").fetchone()[0] == 0


@pytest.mark.parametrize("change", [
    {"metricId": "prompt.text"}, {"metricId": []}, {"metricId": "<img onerror=alert(1)>"},
    {"value": 100.1}, {"weight": True}, {"weight": 0}, {"weight": -1}, {"weight": 1_000_001},
    {"agentId": "helper"}, {"conversationId": "c_visible"}, {"tags": {"text": "NEVER_PERSIST"}},
])
def test_unknown_metrics_tags_bad_weights_and_wrong_scope_are_rejected(store, change):
    assert not ingest(store, [{**point(), **change}])
    assert store.settings()["rowCount"] == 0 and store.settings()["rejectedBatches"] == 1
    assert "NEVER_PERSIST" not in "\n".join(store.db.iterdump())


def test_required_fields_empty_batches_and_defaults(store):
    for key in ("schemaVersion", "bootId", "sequence", "observedAt", "points"):
        envelope = batch()
        del envelope[key]
        assert not store.ingest("one", "enrollment-one", envelope, normalized())
    assert ingest(store, [])
    envelope = batch(sequence=2)
    del envelope["intervalSeconds"]
    assert store.ingest("one", "enrollment-one", envelope, normalized())
    assert store.settings()["rowCount"] == 1


@pytest.mark.parametrize("extras", [
    {"agentId": "unknown", "conversationId": "c_visible"},
    {"agentId": "helper", "conversationId": "not-in-snapshot"},
    {"agentId": "helper", "conversationId": "c_other"},
    {"agentId": "helper", "conversationId": "../secret"},
    {"agentId": "helper", "conversationId": "<script>"},
    {"agentId": "helper", "conversationId": "c" * 257},
    {"agentId": "a" * 129, "conversationId": "c_visible"},
    {"agentId": "helper"}, {"conversationId": "c_visible"},
])
def test_agent_conversation_ownership_and_routing_ids_are_strict(store, extras):
    frame = snapshot()
    frame["agents"].append({"id": "other"})
    frame["conversations"].append({"id": "c_other", "agentId": "other", "status": "active"})
    assert not ingest(store, [point("agent.loops.duration_ms", 100, **extras)], scope=frame)
    assert store.settings()["rowCount"] == 0


@pytest.mark.parametrize("flags", [
    {"parentId": "c_parent"}, {"parentConversationId": "c_parent"}, {"isTopLevel": False},
    {"isSubagent": True}, {"hidden": True}, {"visible": False}, {"userFacing": False},
    {"isUserFacing": False}, {"status": "archived"},
])
def test_hidden_nested_archived_or_non_user_conversations_are_not_metric_dimensions(store, flags):
    frame = snapshot()
    frame["conversations"][0].update(flags)
    frame["questions"] = [{"agentId": "helper", "conversationId": "c_visible", "requestId": "q_one"}]
    safe = normalized(frame)
    assert safe["conversations"] == [] and safe["questions"] == []
    assert not store.ingest("one", "enrollment-one", batch([point("agent.loops.duration_ms", 10, agentId="helper", conversationId="c_visible")]), safe)
    assert store.dimensions()["conversations"] == []


def test_question_only_current_visible_owner_is_eligible(store):
    frame = snapshot()
    frame["conversations"] = []
    frame["questions"] = [{"agentId": "helper", "conversationId": "c_older", "requestId": "q_one"}]
    assert ingest(store, [point("agent.loops.completed", 1, agentId="helper", conversationId="c_older")], scope=frame)
    assert store.dimensions()["conversations"] == [{"worldId": "one", "agentId": "helper", "conversationId": "c_older"}]
    assert not ingest(store, [point("agent.loops.completed", 1, agentId="helper", conversationId="c_older")], sequence=2)


def test_ambiguous_snapshot_ownership_and_duplicate_private_routes_fail_closed(store):
    for private_first in (True, False):
        frame = snapshot()
        frame["agents"].append({"id": "other"})
        duplicate = {"id": "c_visible", "agentId": "other", "status": "active"}
        frame["conversations"].insert(0 if private_first else 1, duplicate)
        safe = normalized(frame)
        assert safe["conversations"] == []
        assert not store.ingest("one", "enrollment-one", batch([point("agent.loops.duration_ms", 1, agentId="helper", conversationId="c_visible")]), safe)
    frame = snapshot()
    frame["conversations"].append({**frame["conversations"][0], "hidden": True})
    assert normalized(frame)["conversations"] == []


def test_agent_only_zero_is_allowed_only_without_visible_conversations(store):
    frame = snapshot()
    frame["conversations"] = []
    assert ingest(store, [point("agent.loops.active", 0, agentId="helper")], scope=frame)
    assert not ingest(store, [point("agent.loops.active", 1, agentId="helper")], scope=frame, sequence=2)
    assert not ingest(store, [point("agent.loops.active", 0, agentId="helper")], sequence=3)
    assert query(store, "agent.loops.active")["series"][0]["conversationId"] is None


def test_maximum_routing_id_lengths_and_large_numeric_values(store):
    agent, conversation = "a" * 128, "c" * 256
    scope = snapshot(agent=agent, conversation=conversation)
    assert ingest(store, [point("agent.loops.duration_ms", 1e18, weight=1_000_000, agentId=agent, conversationId=conversation)], scope=scope)
    result = query(store, "agent.loops.duration_ms", agent_id=agent, conversation_id=conversation)
    assert result["series"][0]["points"][0]["value"] == 1e18


def test_batch_byte_point_and_duplicate_limits_are_atomic(store):
    assert not ingest(store, [point(), point()])
    assert not ingest(store, [point()] * 513)
    oversized = batch()
    oversized["bootId"] = "x" * (64 * 1024)
    assert not store.ingest("one", "enrollment-one", oversized, normalized())
    assert store.settings()["rowCount"] == 0
    assert store.settings()["rejectedBatches"] == 3


def test_retention_rejects_old_and_future_but_accepts_inclusive_boundaries(store, clock):
    assert ingest(store, at=clock() - 7 * DAY_MS)
    assert ingest(store, at=clock() + CLOCK_SKEW_MS, sequence=2)
    assert not ingest(store, at=clock() - 7 * DAY_MS - 1, sequence=3)
    assert not ingest(store, at=clock() + CLOCK_SKEW_MS + 1, sequence=4)
    assert store.settings()["rowCount"] == 2


def test_retention_change_prunes_samples_dimensions_and_boots(store, clock):
    assert ingest(store, [point("agent.loops.completed", 1, agentId="helper", conversationId="c_visible")], at=NOW - 3 * DAY_MS)
    assert ingest(store, sequence=2, boot="new-boot")
    result = store.set_retention(1)
    assert result["retentionDays"] == 1 and result["rowCount"] == 1
    assert result["oldestAt"] == result["newestAt"] == NOW
    assert store.dimensions()["agents"] == [] and store.dimensions()["conversations"] == []
    assert store.db.execute("SELECT COUNT(*) FROM boots").fetchone()[0] == 1
    with pytest.raises(InvalidInput, match="Invalid metrics retention"):
        store.set_retention(True)


def test_idle_periodic_maintenance_expires_without_any_reads_or_ingestion(tmp_path, clock):
    store = MetricsStore(tmp_path / "metrics.sqlite", retention_days=1, clock=clock, maintenance_seconds=0.01)
    store.register_world("one", "enrollment-one")
    assert ingest(store)
    clock.now += DAY_MS + 1

    async def exercise():
        stop = asyncio.Event()
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()
        original = store.prune

        def prune_once():
            original()
            loop.call_soon_threadsafe(finished.set)

        store.prune = prune_once
        task = asyncio.create_task(store.maintain(stop))
        try:
            await asyncio.wait_for(finished.wait(), 2)
        finally:
            stop.set()
            await task

    try:
        asyncio.run(exercise())
        assert store.settings()["rowCount"] == 0
        assert store.db.execute("SELECT COUNT(*) FROM boots").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 0
    finally:
        store.close()


def test_row_cap_is_hard_indexed_and_freed_pages_are_reused(tmp_path, clock):
    store = MetricsStore(tmp_path / "metrics.sqlite", max_rows=20, clock=clock)
    store.register_world("one", "enrollment-one")
    try:
        for seq in range(1, 81):
            assert ingest(store, at=NOW - 100 + seq, sequence=seq)
        assert store.settings()["rowCount"] == 20
        times = [row[0] for row in store.db.execute("SELECT observed_at FROM samples ORDER BY observed_at")]
        assert times == [NOW - 100 + seq for seq in range(61, 81)]
        assert store.db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        query_plan = " ".join(str(tuple(row)) for row in store.db.execute("EXPLAIN QUERY PLAN SELECT * FROM samples WHERE series_id=? AND observed_at>=? AND observed_at<?", (1, NOW - DAY_MS, NOW)))
        assert "samples_series_time" in query_plan and "SEARCH" in query_plan
        page_count = store.db.execute("PRAGMA page_count").fetchone()[0]
        for seq in range(81, 161):
            assert ingest(store, at=NOW - 100 + seq, sequence=seq)
        assert store.db.execute("PRAGMA page_count").fetchone()[0] <= page_count + 2
        clock.now += 8 * DAY_MS
        store.prune()
        assert store.settings()["rowCount"] == 0
        assert store.db.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 0
        assert Path(str(store.path) + "-wal").stat().st_size == 0
    finally:
        store.close()


def test_series_boot_and_response_cardinality_caps_are_enforced(store, monkeypatch):
    monkeypatch.setattr(metrics_module, "MAX_WORLD_SERIES", 2)
    assert ingest(store, [point(), point("gateway.memory.rss_bytes", 1)])
    assert not ingest(store, [point("gateway.loops.active", 1)], sequence=2)
    assert store.settings()["rowCount"] == 2
    monkeypatch.setattr(metrics_module, "MAX_WORLD_BOOTS", 1)
    assert not ingest(store, boot="new-boot", sequence=2)
    assert store.db.execute("SELECT COUNT(*) FROM boots").fetchone()[0] == 1
    monkeypatch.setattr(metrics_module, "MAX_QUERY_SERIES", 1)
    result = query(store)
    assert len(result["series"]) == 1 and result["truncated"] is True
    monkeypatch.setattr(metrics_module, "MAX_DIMENSIONS", 1)
    store.register_world("two", "enrollment-two")
    assert store.dimensions()["worlds"] == ["one"]
    assert store.dimensions()["truncated"] is True
    monkeypatch.setattr(metrics_module, "MAX_TILE_WORLDS", 1)
    tiles = store.tiles()
    assert list(tiles["worlds"]) == ["one"] and tiles["truncated"] is True


def test_query_series_point_limits_and_missing_gaps(store):
    for seq in range(1, 101):
        assert ingest(store, [point(value=seq % 100)], at=NOW - 1000 + seq * 10, sequence=seq)
    result = store.query(metric_ids=["gateway.cpu.percent"], start=NOW - 1000, end=NOW + 1, max_points=16)
    assert len(result["series"]) == 1 and len(result["series"][0]["points"]) <= 16
    assert result["bucketMs"] == 63
    empty = store.query(metric_ids=["gateway.memory.rss_bytes"], start=NOW - 1000, end=NOW + 1)
    assert empty["series"] == []
    assert result["series"][0]["latestAt"] == NOW
    exact = store.query(metric_ids=["gateway.cpu.percent"], start=NOW, end=NOW + 1, max_points=16)
    assert exact["series"][0]["points"] == [{"timestamp": NOW, "value": 0, "count": 1}]
    excluded = store.query(metric_ids=["gateway.cpu.percent"], start=NOW - 1, end=NOW, max_points=16)
    assert excluded["series"] == []


def test_tile_selection_is_world_only_persistent_and_enrollment_specific(tmp_path, clock):
    path = tmp_path / "metrics.sqlite"
    store = MetricsStore(path, clock=clock)
    store.register_world("one", "enrollment-one")
    selected = ["sqlite.connections.current", "gateway.memory.rss_bytes"]
    assert store.set_tile_metrics("one", selected) == {"worldId": "one", "metrics": selected}
    assert ingest(store, [point(selected[0], 3), point(selected[1], 0)])
    store.set_retention(12)
    store.close()
    restarted = MetricsStore(path, clock=clock, retention_days=3)
    try:
        tiles = restarted.tiles()
        assert tiles["retentionDays"] == 12
        assert tiles["worlds"]["one"]["metrics"] == selected
        assert {item["metricId"] for item in tiles["worlds"]["one"]["series"]} == set(selected)
        assert all(item["agentId"] is None and item["conversationId"] is None for item in tiles["worlds"]["one"]["series"])
        restarted.delete_world("one")
        assert restarted.settings()["rowCount"] == 0
        assert restarted.dimensions()["worlds"] == []
        assert restarted.tiles()["worlds"] == {}
        assert restarted.db.execute("SELECT COUNT(*) FROM boots").fetchone()[0] == 0
        restarted.register_world("one", "replacement")
        assert restarted.tiles()["worlds"]["one"]["metrics"] == list(DEFAULT_METRICS)
        assert restarted.tiles()["worlds"]["one"]["series"] == []
        assert not restarted.ingest("one", "enrollment-one", batch(sequence=2), normalized())
        assert restarted.ingest("one", "replacement", batch(), normalized())
    finally:
        restarted.close()


def test_startup_reconciliation_cannot_leak_reused_ids_after_failed_purge(tmp_path, clock):
    path = tmp_path / "metrics.sqlite"
    store = MetricsStore(path, clock=clock)
    store.register_world("one", "old-enrollment")
    assert store.ingest("one", "old-enrollment", batch(), normalized())
    store.set_tile_metrics("one", ["gateway.uptime.seconds"])
    store.close()
    restarted = MetricsStore(path, clock=clock)
    try:
        restarted.sync_enrollments({"one": "replacement", "two": "other-enrollment"})
        assert restarted.settings()["rowCount"] == 0
        assert restarted.tiles()["worlds"]["one"]["metrics"] == list(DEFAULT_METRICS)
        assert restarted.enrollment("one") == "replacement"
        assert restarted.db.execute("SELECT COUNT(*) FROM boots").fetchone()[0] == 0
    finally:
        restarted.close()


def test_settings_api_and_csrf_boundary(client, auth):
    initial = client.get("/api/metrics/settings")
    assert initial.status_code == 200
    assert initial.json() == {"retentionDays": 7, "maxRows": 1_000_000, "sampleIntervalSeconds": 30,
                              "rowCount": 0, "databaseBytes": initial.json()["databaseBytes"],
                              "oldestAt": None, "newestAt": None, "rejectedBatches": 0}
    assert initial.json()["databaseBytes"] > 0
    for headers in ({}, {"X-Hub-Request": "1"}, {"Origin": "http://127.0.0.1"}, {**auth, "Origin": "https://evil.example"}):
        assert client.put("/api/metrics/settings", headers=headers, json={"retentionDays": 1}).status_code == 403
    response = client.put("/api/metrics/settings", headers=auth, json={"retentionDays": 365})
    assert response.status_code == 200 and response.json()["retentionDays"] == 365
    for days in (0, 366, True, 2.5, "7", None):
        response = client.put("/api/metrics/settings", headers=auth, json={"retentionDays": days})
        assert response.status_code == 422 and response.json() == {"detail": "Invalid metrics retention"}
    assert client.put("/api/metrics/settings", headers=auth, json={"retentionDays": 1, "secret": "NEVER_ECHO"}).json() == {"detail": "Expected retentionDays"}
    assert client.put("/api/metrics/settings", headers=auth, content="x" * 65537).status_code == 413


@pytest.mark.parametrize("params", [
    [], [("metricId", "NEVER_ECHO")], [("metricId", "gateway.cpu.percent")] * 7,
    [("metricId", "gateway.cpu.percent"), ("from", "1.5")],
    [("metricId", "gateway.cpu.percent"), ("from", "true")],
    [("metricId", "gateway.cpu.percent"), ("from", "-1")],
    [("metricId", "gateway.cpu.percent"), ("from", "1e3")],
    [("metricId", "gateway.cpu.percent"), ("to", str(MAX_INTEGER + 1))],
    [("metricId", "gateway.cpu.percent"), ("from", "100"), ("to", "100")],
    [("metricId", "gateway.cpu.percent"), ("from", "100"), ("to", "99")],
    [("metricId", "gateway.cpu.percent"), ("maxPoints", "15")],
    [("metricId", "gateway.cpu.percent"), ("maxPoints", "721")],
    [("metricId", "gateway.cpu.percent"), ("maxPoints", "16"), ("maxPoints", "17")],
    [("metricId", "gateway.cpu.percent"), ("worldId", "../NEVER_ECHO")],
    [("metricId", "gateway.cpu.percent"), ("agentId", "<script>NEVER_ECHO")],
    [("metricId", "gateway.cpu.percent"), ("conversationId", "c" * 257)],
    [("metricId", "gateway.cpu.percent"), ("world", "one")],
])
def test_query_api_is_strict_without_reflecting_hostile_input(client, params):
    response = client.get("/api/metrics/query", params=params)
    assert response.status_code == 422, response.text
    assert "NEVER_ECHO" not in response.text and "<script>" not in response.text


def test_query_api_unknown_filters_are_empty_and_never_ignored(client, auth):
    world = register(client, auth)
    at = metrics_module.utc_ms()
    with connect(client, world) as ws:
        publish(client, auth, ws, batch(at=at))
        result = client.get("/api/metrics/query", params={"metricId": "gateway.cpu.percent", "from": at, "to": at + 1}).json()
        assert len(result["series"]) == 1 and result["series"][0]["points"][0]["value"] == 0
        for key in ("worldId", "agentId", "conversationId"):
            response = client.get("/api/metrics/query", params={"metricId": "gateway.cpu.percent", key: "unknown"})
            assert response.status_code == 200 and response.json()["series"] == []
        response = client.get("/api/metrics/query", params=[("metricId", "gateway.cpu.percent"), ("worldId", "unknown"), ("worldId", "one")])
        assert len(response.json()["series"]) == 1


def test_metrics_are_optional_on_v1_and_never_stored_in_world_snapshot(client, auth):
    world = register(client, auth)
    with connect(client, world) as ws:
        publish(client, auth, ws, ...)
        publish(client, auth, ws, None)
        assert client.get("/api/metrics/settings").json()["rowCount"] == 0
        publish(client, auth, ws, batch(at=metrics_module.utc_ms()))
        settings = client.get("/api/metrics/settings").json()
        assert settings["rowCount"] == 1 and settings["rejectedBatches"] == 0
        saved = client.get("/api/worlds").json()["items"][0]
        assert saved["connected"] is True and "metrics" not in saved["snapshot"]
    hub = client.app.state.store
    with hub.lock:
        assert hub.db.execute("SELECT name FROM sqlite_master WHERE name='samples'").fetchone() is None


@pytest.mark.parametrize("envelope", [False, {}, [], "NEVER_ECHO", {"schemaVersion": 2}, batch([point(value="NEVER_ECHO")])])
def test_invalid_metrics_do_not_break_snapshot_or_commands(client, auth, envelope, caplog):
    world = register(client, auth)
    with connect(client, world) as ws:
        publish(client, auth, ws, envelope)
        result = client.get("/api/metrics/settings").json()
        assert result["rowCount"] == 0 and result["rejectedBatches"] == 1
        assert client.get("/api/worlds").json()["items"][0]["connected"] is True
        assert "NEVER_ECHO" not in caplog.text
        dump = "\n".join(client.app.state.metrics.db.iterdump())
        assert "NEVER_ECHO" not in dump


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999", '"\\ud800"', "1" * 1000, '{"value":1,"value":2}'])
def test_invalid_json_values_inside_only_metrics_are_isolated(client, auth, value):
    world = register(client, auth)
    with connect(client, world) as ws:
        raw = json.dumps(snapshot())[:-1] + ',"metrics":' + value + '}'
        ws.send_text(raw)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if client.get("/api/metrics/settings").json()["rejectedBatches"]:
                break
            time.sleep(0.005)
        else:
            pytest.fail("Invalid optional metrics were not diagnosed")
        barrier(client, auth, ws)
        assert client.get("/api/worlds").json()["items"][0]["connected"] is True
        assert client.get("/api/metrics/settings").json()["rowCount"] == 0


@pytest.mark.parametrize("raw", [
    '{"type":"snapshot","protocolVersion":NaN,"metrics":NaN}',
    '{"type":"snapshot","protocolVersion":1,"worldId":"one","agents":[],"agents":[],"metrics":NaN}',
    '{"type":"snapshot","protocolVersion":1,"worldId":"one","name":"\\ud800","metrics":NaN}',
    '{"type":"snapshot","protocolVersion":1,"worldId":"one","observedAt":1e999,"metrics":NaN}',
    '{"type":"snapshot","protocolVersion":1,"worldId":"one","sequence":' + '1' * 1000 + ',"metrics":NaN}',
])
def test_metrics_fallback_cannot_weaken_full_snapshot_json_guards(raw):
    with pytest.raises(InvalidInput):
        _parse_frame(raw)


def test_wrong_world_still_disconnects_without_ingesting_metrics(client, auth):
    one = register(client, auth)
    register(client, auth, "two")
    with connect(client, one) as ws:
        ws.send_json({**snapshot("two"), "metrics": batch(at=metrics_module.utc_ms())})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert client.get("/api/metrics/settings").json()["rowCount"] == 0


def test_storage_failure_is_safe_fixed_and_does_not_break_commands(client, auth, monkeypatch, caplog):
    world = register(client, auth)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("NEVER_ECHO_DATABASE_SECRET")

    store = client.app.state.metrics
    monkeypatch.setattr(store, "ingest", broken)
    with connect(client, world) as ws:
        publish(client, auth, ws, batch(at=metrics_module.utc_ms()))
        assert store.settings()["rejectedBatches"] == 1
        assert client.get("/api/worlds").json()["items"][0]["connected"] is True
    assert "NEVER_ECHO_DATABASE_SECRET" not in caplog.text
    with store.lock:
        store.db.execute("DROP TABLE samples")
    response = client.get("/api/metrics/settings")
    assert response.status_code == 503 and response.json() == {"detail": "Metrics are unavailable"}
    assert "NEVER_ECHO" not in response.text


def test_unavailable_metrics_db_does_not_block_hub_startup(app_settings, auth, tmp_path):
    directory = tmp_path / "unavailable"
    directory.mkdir()
    with TestClient(create_app(**app_settings, metrics_db_path=directory), base_url="http://127.0.0.1", client=("127.0.0.1", 51000)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/metrics/settings").status_code == 503
        world = register(client, auth)
        with connect(client, world) as ws:
            publish(client, auth, ws, batch(at=metrics_module.utc_ms()))
            assert client.get("/api/worlds").json()["items"][0]["connected"] is True


def test_tiles_api_mutation_is_explicit_csrf_guarded_and_world_scoped(client, auth):
    world = register(client, auth)
    register(client, auth, "two")
    selected = ["sqlite.connections.current", "gateway.memory.rss_bytes"]
    url = "/api/worlds/one/metrics"
    assert client.put(url, json={"metrics": selected}).status_code == 403
    assert client.put(url, headers={**auth, "Origin": "https://evil.example"}, json={"metrics": selected}).status_code == 403
    for metrics in ([], ["unknown"], ["agent.loops.active"], list(DEFAULT_METRICS) + ["gateway.uptime.seconds"], ["gateway.cpu.percent"] * 2, [True], "gateway.cpu.percent"):
        response = client.put(url, headers=auth, json={"metrics": metrics})
        assert response.status_code == 422
    assert client.put(url, headers=auth, json={"metrics": selected}).json() == {"worldId": "one", "metrics": selected}
    with connect(client, world) as ws:
        publish(client, auth, ws, batch([point(selected[0], 2), point(selected[1], 0), point("agent.loops.completed", 1, agentId="helper", conversationId="c_visible")], at=metrics_module.utc_ms()))
        tiles = client.get("/api/metrics/tiles").json()
        assert tiles["worlds"]["one"]["metrics"] == selected
        assert tiles["worlds"]["two"]["metrics"] == list(DEFAULT_METRICS)
        assert {item["metricId"] for item in tiles["worlds"]["one"]["series"]} == set(selected)
        assert client.get("/api/metrics/tiles?worldId=unknown").json()["worlds"] == {}
        assert list(client.get("/api/metrics/tiles?worldId=one").json()["worlds"]) == ["one"]
        dims = client.get("/api/metrics/dimensions?worldId=one").json()
        assert dims["worlds"] == ["one"]
        assert dims["conversations"] == [{"worldId": "one", "agentId": "helper", "conversationId": "c_visible"}]
    assert client.put("/api/worlds/unknown/metrics", headers=auth, json={"metrics": selected}).status_code == 404
    for path in ("dimensions", "tiles"):
        assert client.get("/api/metrics/" + path + "?worldId=one&worldId=two").status_code == 422
        assert client.get("/api/metrics/" + path + "?agentId=helper").status_code == 422


def test_app_restart_persists_settings_and_history_env_is_only_initial_default(app_settings, auth, monkeypatch):
    monkeypatch.setenv("HUB_METRICS_RETENTION_DAYS", "3")
    for attempt in range(2):
        with TestClient(create_app(**app_settings), base_url="http://127.0.0.1", client=("127.0.0.1", 51000)) as client:
            if attempt == 0:
                assert client.get("/api/metrics/settings").json()["retentionDays"] == 3
                world = register(client, auth)
                with connect(client, world) as ws:
                    publish(client, auth, ws, batch(at=metrics_module.utc_ms()))
                assert client.put("/api/metrics/settings", headers=auth, json={"retentionDays": 10}).status_code == 200
                assert client.put("/api/worlds/one/metrics", headers=auth, json={"metrics": ["gateway.uptime.seconds"]}).status_code == 200
            else:
                assert client.get("/api/metrics/settings").json()["retentionDays"] == 10
                assert client.get("/api/metrics/settings").json()["rowCount"] == 1
                assert client.get("/api/metrics/tiles").json()["worlds"]["one"]["metrics"] == ["gateway.uptime.seconds"]


def test_app_delete_and_reuse_purges_history_preferences_and_dedup(client, auth):
    world = register(client, auth)
    other = register(client, auth, "two")
    at = metrics_module.utc_ms()
    with connect(client, world) as ws, connect(client, other) as second:
        publish(client, auth, ws, batch(at=at))
        publish(client, auth, second, batch(at=at), world="two")
        assert client.put("/api/worlds/one/metrics", headers=auth, json={"metrics": ["gateway.uptime.seconds"]}).status_code == 200
        assert client.delete("/api/worlds/one", headers=auth).status_code == 204
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    replacement = register(client, auth)
    assert client.get("/api/metrics/settings").json()["rowCount"] == 1
    assert client.get("/api/metrics/tiles?worldId=one").json()["worlds"]["one"] == {"metrics": list(DEFAULT_METRICS), "series": []}
    with connect(client, replacement) as ws:
        publish(client, auth, ws, batch(at=at))
    assert client.get("/api/metrics/settings").json()["rowCount"] == 2


def test_token_rotation_preserves_history_and_tile_selection(client, auth):
    world = register(client, auth)
    with connect(client, world) as ws:
        publish(client, auth, ws, batch(at=metrics_module.utc_ms()))
    selected = ["gateway.uptime.seconds"]
    assert client.put("/api/worlds/one/metrics", headers=auth, json={"metrics": selected}).status_code == 200
    response = client.post("/api/worlds/one/rotate-token", headers=auth)
    assert response.status_code == 200
    assert client.get("/api/metrics/settings").json()["rowCount"] == 1
    assert client.get("/api/metrics/tiles?worldId=one").json()["worlds"]["one"]["metrics"] == selected


def test_same_database_path_alias_is_rejected_before_hub_writes(app_settings, tmp_path):
    path = app_settings["db_path"]
    with pytest.raises(RuntimeError, match="different paths"):
        with TestClient(create_app(**app_settings, metrics_db_path=path.parent / "nested" / ".." / path.name)):
            pass
    assert not path.exists()
    assert database_path(path) == str(path.with_name("metrics.sqlite"))
    assert database_path(":memory:") == ":memory:"


def test_hardlink_database_alias_is_forbidden(tmp_path):
    import os
    path, alias = tmp_path / "hub.sqlite", tmp_path / "alias.sqlite"
    path.write_bytes(b"synthetic")
    os.link(path, alias)
    with pytest.raises(RuntimeError, match="different paths"):
        database_path(path, alias)


def test_shutdown_is_idempotent_and_stops_maintenance(app_settings):
    app = create_app(**app_settings, metrics_maintenance_seconds=0.01)
    with TestClient(app):
        store = app.state.metrics
        assert store._closed is False
    assert store._closed is True
    store.close()
    with pytest.raises(MetricsUnavailable):
        store.settings()


def test_512_numeric_points_are_accepted_but_513_are_not(store):
    frame = snapshot()
    frame["conversations"] = [{"id": f"c_{index}", "agentId": "helper", "status": "active"} for index in range(100)]
    frame["questions"] = [{"conversationId": f"c_{index}", "agentId": "helper", "requestId": f"q_{index}"} for index in range(100, 128)]
    metrics = [item["id"] for item in CATALOG if item["scope"] == "agent"]
    points = [point(metric, 0, agentId="helper", conversationId=f"c_{index}") for index in range(128) for metric in metrics]
    assert len(points) == 512
    assert len(json.dumps(batch(points), separators=(",", ":")).encode()) <= 64 * 1024
    assert ingest(store, points, scope=frame)
    assert store.settings()["rowCount"] == 512
    assert not ingest(store, points + [point()], scope=frame, sequence=2)
    assert store.settings()["rowCount"] == 512


@pytest.mark.parametrize("raw_metrics", [
    " " + json.dumps(batch()).replace('"points": [', '"points": [' + " " * (64 * 1024)),
    "[" * 2000 + "0" + "]" * 2000,
    '{"schemaVersion":1,"schemaVersion":1}',
], ids=["wire-byte-limit", "deep-nesting", "duplicate-key"])
def test_oversized_raw_or_deep_metrics_are_dropped_before_decoding(raw_metrics):
    raw = json.dumps(snapshot())[:-1] + ',"metrics":' + raw_metrics + '}'
    frame = _parse_frame(raw)
    assert frame["type"] == "snapshot" and frame["worldId"] == "one"
    assert frame["metrics"] is False
    assert normalized(frame)["agents"][0]["id"] == "helper"


def test_raw_metrics_size_boundary_counts_whitespace_inside_envelope():
    prefix = json.dumps(snapshot())[:-1] + ',"metrics":'
    envelope = json.dumps(batch(), separators=(",", ":"))
    exact = envelope[:-1] + " " * (64 * 1024 - len(envelope)) + "}"
    assert len(exact.encode()) == 64 * 1024
    assert _parse_frame(prefix + exact + "}")["metrics"] == batch()
    oversized = exact[:-1] + " }"
    assert _parse_frame(prefix + oversized + "}")["metrics"] is False


def test_snapshot_fields_with_quoted_brackets_escape_sequences_and_metrics_like_names():
    frame = snapshot()
    frame["name"] = 'Name: {"metrics": ["not a field", "\\\\", "\\\""]}'
    frame["metrics"] = batch()
    raw = json.dumps(frame, ensure_ascii=True)
    assert _parse_frame(raw) == frame
    frame_without = {key: value for key, value in frame.items() if key != "metrics"}
    assert _parse_frame(json.dumps(frame_without)) == frame_without
    assert _parse_frame(raw.replace('"metrics":', '"metri\\u0063s":')) == frame


@pytest.mark.parametrize("raw", [
    '{"type":"snapshot","worldId":"one","metrics":{"bad"::1}}',
    '{"type":"snapshot","worldId":"one","metrics":[1,2}}',
    '{"type":"snapshot","worldId":"one","metrics":{} } trailing',
    '{"type":"snapshot","worldId":"one","metrics":{}}}',
    '{"type":"snapshot","worldId":"one","metrics":}',
])
def test_optional_metrics_do_not_accept_invalid_whole_frame_syntax(raw):
    with pytest.raises(InvalidInput):
        _parse_frame(raw)


def test_slow_metrics_storage_cannot_delay_commands_or_grow_unbounded_work(client, auth, monkeypatch):
    world = register(client, auth)
    store = client.app.state.metrics
    original = store.ingest
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow(*args):
        calls.append(1)
        entered.set()
        if not release.wait(5):
            raise AssertionError("Synthetic blocked writer was not released")
        return original(*args)

    monkeypatch.setattr(store, "ingest", slow)
    with connect(client, world) as ws:
        try:
            ws.send_json({**snapshot(), "metrics": batch(at=metrics_module.utc_ms())})
            assert entered.wait(2)
            barrier(client, auth, ws)
            for seq in range(2, 8):
                ws.send_json({**snapshot(), "metrics": batch(sequence=seq, at=metrics_module.utc_ms())})
            barrier(client, auth, ws)
            assert len(calls) == 1
            assert len(client.app.state.router.metrics_tasks) == 1
            assert store.settings()["rejectedBatches"] == 6
            assert client.get("/api/worlds").json()["items"][0]["connected"] is True
        finally:
            release.set()

        async def join():
            await asyncio.gather(*list(client.app.state.router.metrics_tasks))
        client.portal.call(join)
        assert store.settings()["rowCount"] == 1


def test_failed_purge_blocks_all_history_and_later_rotation_cannot_restore_it(store):
    assert ingest(store)
    store.set_tile_metrics("one", ["gateway.uptime.seconds"])
    with store.db:
        store.db.execute("CREATE TRIGGER blocked_delete BEFORE DELETE ON enrollments BEGIN SELECT RAISE(FAIL, 'NEVER_ECHO_PURGE_SECRET'); END")
    with pytest.raises(MetricsUnavailable, match="^Metrics are unavailable$"):
        store.delete_world("one")
    assert query(store)["series"] == []
    assert store.dimensions() == {"worlds": [], "agents": [], "conversations": [], "truncated": False}
    assert store.tiles()["worlds"] == {}
    with store.db:
        store.db.execute("DROP TRIGGER blocked_delete")
    store.register_world("one", "replacement", preserve=True)
    assert store.settings()["rowCount"] == 0
    assert store.tiles()["worlds"]["one"]["metrics"] == list(DEFAULT_METRICS)
    assert store.db.execute("SELECT COUNT(*) FROM boots").fetchone()[0] == 0


def test_real_query_cap_does_not_exceed_64_exact_series(store):
    frame = snapshot()
    frame["conversations"] = [{"id": f"c_{index:03}", "agentId": "helper", "status": "active"} for index in range(80)]
    assert ingest(store, [point("agent.loops.active", 0, agentId="helper", conversationId=item["id"]) for item in frame["conversations"]], scope=frame)
    result = query(store, "agent.loops.active")
    assert result["truncated"] is True and len(result["series"]) == 64
    assert len({(item["worldId"], item["agentId"], item["conversationId"]) for item in result["series"]}) == 64
    assert all(item["points"][0]["value"] == 0 for item in result["series"])
    assert len(store.dimensions()["conversations"]) == 80


def test_global_series_and_boot_caps_and_capacity_rollback(store, monkeypatch):
    store.register_world("two", "enrollment-two")
    monkeypatch.setattr(metrics_module, "MAX_STORED_SERIES", 2)
    assert ingest(store)
    assert not ingest(store, [point("gateway.memory.rss_bytes", 1), point("gateway.loops.active", 0)], world="two")
    assert store.settings()["rowCount"] == 1
    assert store.db.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM boots WHERE world_id='two'").fetchone()[0] == 0
    assert ingest(store, world="two")
    monkeypatch.setattr(metrics_module, "MAX_BOOTS", 2)
    assert not ingest(store, boot="another-boot", sequence=2)
    assert store.db.execute("SELECT COUNT(*) FROM boots").fetchone()[0] == 2


def test_out_of_order_observations_still_drop_oldest_observation_not_newest_receipt(tmp_path, clock):
    store = MetricsStore(tmp_path / "metrics.sqlite", max_rows=2, clock=clock)
    store.register_world("one", "enrollment-one")
    try:
        assert ingest(store, at=NOW, sequence=1)
        assert ingest(store, at=NOW - 2, sequence=2)
        assert ingest(store, at=NOW - 1, sequence=3)
        assert [row[0] for row in store.db.execute("SELECT observed_at FROM samples ORDER BY observed_at")] == [NOW - 1, NOW]
        assert not ingest(store, at=NOW, sequence=2)
        assert store.settings()["rowCount"] == 2
    finally:
        store.close()


def test_startup_prunes_without_traffic_and_env_overrides_only_initial_settings(app_settings, auth, monkeypatch, tmp_path):
    metrics_path = tmp_path / "separate" / "history.sqlite"
    monkeypatch.setenv("HUB_METRICS_DB_PATH", str(metrics_path))
    monkeypatch.setenv("HUB_METRICS_MAX_ROWS", "20")
    app = create_app(**app_settings)
    assert not metrics_path.exists()
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 51000)) as client:
        world = register(client, auth)
        with connect(client, world) as ws:
            publish(client, auth, ws, batch(at=metrics_module.utc_ms()))
        assert client.get("/api/metrics/settings").json()["maxRows"] == 20
    assert metrics_path.exists() and not metrics_path.with_name("metrics.sqlite").exists()
    with sqlite3.connect(metrics_path) as db:
        db.execute("UPDATE samples SET observed_at=observed_at-?", (8 * DAY_MS,))
        db.execute("UPDATE boots SET last_at=last_at-?", (8 * DAY_MS,))
    with TestClient(create_app(**app_settings)) as client:
        result = client.get("/api/metrics/settings").json()
        assert result["rowCount"] == 0 and result["oldestAt"] is None and result["newestAt"] is None
        assert client.app.state.metrics.db.execute("SELECT COUNT(*) FROM boots").fetchone()[0] == 0
        assert client.app.state.metrics.db.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 0


def test_standalone_metrics_frames_are_still_unsupported(client, auth):
    world = register(client, auth)
    with connect(client, world) as ws:
        ws.send_json({"type": "metrics", **batch(at=metrics_module.utc_ms())})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert client.get("/api/metrics/settings").json()["rowCount"] == 0


def test_delayed_old_enrollment_write_cannot_cross_delete_and_reuse(client, auth, monkeypatch):
    world = register(client, auth)
    store = client.app.state.metrics
    original = store.ingest
    entered, release = threading.Event(), threading.Event()
    results = []

    def delayed(*args):
        entered.set()
        if not release.wait(5):
            raise AssertionError("Synthetic old-enrollment writer was not released")
        result = original(*args)
        results.append(result)
        return result

    monkeypatch.setattr(store, "ingest", delayed)
    with connect(client, world) as ws:
        try:
            ws.send_json({**snapshot(), "metrics": batch(at=metrics_module.utc_ms())})
            assert entered.wait(2)
            barrier(client, auth, ws)
            assert client.delete("/api/worlds/one", headers=auth).status_code == 204
            replacement = register(client, auth)
            assert replacement["token"] != world["token"]
        finally:
            release.set()

    async def join():
        await asyncio.gather(*list(client.app.state.router.metrics_tasks))
    client.portal.call(join)
    assert results == [False]
    assert store.settings()["rowCount"] == 0
    assert store.tiles()["worlds"]["one"]["series"] == []
    assert store.db.execute("SELECT COUNT(*) FROM boots").fetchone()[0] == 0
    monkeypatch.setattr(store, "ingest", original)
    with connect(client, replacement) as ws:
        publish(client, auth, ws, batch(at=metrics_module.utc_ms()))
    assert store.settings()["rowCount"] == 1


def test_api_failed_purge_cannot_restore_history_on_reenrollment(client, auth):
    world = register(client, auth)
    store = client.app.state.metrics
    with connect(client, world) as ws:
        publish(client, auth, ws, batch(at=metrics_module.utc_ms()))
    with store.lock, store.db:
        store.db.execute("CREATE TRIGGER blocked_delete BEFORE DELETE ON enrollments BEGIN SELECT RAISE(FAIL, 'NEVER_ECHO'); END")
    assert client.delete("/api/worlds/one", headers=auth).status_code == 204
    assert client.get("/api/metrics/query?metricId=gateway.cpu.percent").json()["series"] == []
    replacement = register(client, auth)
    assert client.get("/api/metrics/tiles?worldId=one").json()["worlds"] == {}
    with store.lock, store.db:
        store.db.execute("DROP TRIGGER blocked_delete")
    # A token rotation may recover metrics, but cannot preserve a blocked
    # previous enrollment's history or tile selection.
    response = client.post("/api/worlds/one/rotate-token", headers=auth)
    assert response.status_code == 200 and response.json()["token"] != replacement["token"]
    assert store.settings()["rowCount"] == 0
    assert store.tiles()["worlds"]["one"] == {"metrics": list(DEFAULT_METRICS), "series": []}


@pytest.mark.parametrize("flags", [{"hidden": True}, {"visible": False}, {"enabled": False}])
def test_conflicting_agent_visibility_is_never_an_allowed_metrics_owner(store, flags):
    frame = snapshot()
    frame["agents"].append({"id": "helper", **flags})
    safe = normalized(frame)
    assert safe["agents"] == [] and safe["conversations"] == []
    assert not store.ingest("one", "enrollment-one", batch([point("agent.loops.active", 0, agentId="helper")]), safe)
    assert store.settings()["rowCount"] == 0


def test_mutating_retention_failure_rolls_back_persistent_and_memory_setting(store):
    assert ingest(store)
    with store.db:
        store.db.execute("CREATE TRIGGER blocked_settings BEFORE UPDATE ON settings BEGIN SELECT RAISE(FAIL, 'NEVER_ECHO'); END")
    with pytest.raises(MetricsUnavailable, match="^Metrics are unavailable$"):
        store.set_retention(1)
    assert store.retention_days == 7
    assert store.db.execute("SELECT retention_days FROM settings").fetchone()[0] == 7
    assert store.settings()["rowCount"] == 1


def test_unknown_query_filters_do_not_affect_known_matching_dimensions(store):
    assert ingest(store, [point("agent.loops.duration_ms", 2, agentId="helper", conversationId="c_visible")])
    for filters in ({"world_ids": ["two"]}, {"agent_id": "other"}, {"conversation_id": "other"}, {"world_ids": ["one"], "agent_id": "other", "conversation_id": "c_visible"}):
        assert query(store, "agent.loops.duration_ms", **filters)["series"] == []
    assert len(query(store, "agent.loops.duration_ms", agent_id="helper", conversation_id="c_visible")["series"]) == 1


def test_rejected_batch_diagnostic_is_durable_on_clean_shutdown(tmp_path, clock):
    path = tmp_path / "metrics.sqlite"
    store = MetricsStore(path, clock=clock)
    store.register_world("one", "enrollment-one")
    assert not ingest(store, [point(value="NEVER_ECHO")])
    assert store.settings()["rejectedBatches"] == 1
    store.close()
    restarted = MetricsStore(path, clock=clock)
    try:
        assert restarted.settings()["rejectedBatches"] == 1
        assert "NEVER_ECHO" not in "\n".join(restarted.db.iterdump())
    finally:
        restarted.close()


def test_corrupt_tile_setting_uses_fixed_unavailable_error(client, auth):
    register(client, auth)
    store = client.app.state.metrics
    with store.lock, store.db:
        store.db.execute("UPDATE enrollments SET tile_metrics='NEVER_ECHO_BAD_JSON'")
    result = client.get("/api/metrics/tiles")
    assert result.status_code == 503
    assert result.json() == {"detail": "Metrics are unavailable"}


def test_mixed_world_cadence_is_preserved_in_query_tiles_and_restart(tmp_path, clock):
    path = tmp_path / "cadence.sqlite"
    store = MetricsStore(path, clock=clock)
    for world, interval in (("one", 30), ("two", 300)):
        store.register_world(world, "enrollment-" + world)
        payload = batch(at=NOW - 120000)
        payload["intervalSeconds"] = interval
        assert store.ingest(world, "enrollment-" + world, payload, normalized(snapshot(world)))
    rows = query(store, "gateway.cpu.percent")["series"]
    assert {row["worldId"]: row["sampleIntervalSeconds"] for row in rows} == {"one": 30, "two": 300}
    assert store.tiles()["worlds"]["two"]["series"][0]["sampleIntervalSeconds"] == 300
    store.close()
    with_store = MetricsStore(path, clock=clock)
    try:
        assert query(with_store, "gateway.cpu.percent", world_ids=["two"])["series"][0]["sampleIntervalSeconds"] == 300
    finally:
        with_store.close()


def test_legacy_sample_cadence_schema_migrates_without_losing_values(tmp_path, clock):
    path = tmp_path / "legacy-metrics.sqlite"
    store = MetricsStore(path, clock=clock)
    store.register_world("one", "enrollment-one")
    assert ingest(store)
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE samples DROP COLUMN interval_seconds")
    store = MetricsStore(path, clock=clock)
    try:
        row = query(store, "gateway.cpu.percent")["series"][0]
        assert row["sampleIntervalSeconds"] == 30
        assert row["points"][0]["value"] == 0
    finally:
        store.close()
