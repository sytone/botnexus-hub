"""Hub protocol and security tests use synthetic state and no gateway calls."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import secrets
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hub.app import create_app


@pytest.fixture
def settings(tmp_path):
    return {
        "db_path": tmp_path / "hub.sqlite3",
        "public_url": "http://127.0.0.1",
        "connector_url": "http://127.0.0.1:8081",
        "command_timeout": 0.3,
        "activity_limit": 12,
    }


@pytest.fixture
def app(settings):
    return create_app(**settings)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as instance:
        yield instance


@pytest.fixture
def auth(settings):
    return {"X-Hub-Request": "1", "Origin": settings["public_url"]}


def register(client, auth, world_id="test-a"):
    response = client.post(
        "/api/worlds",
        headers=auth,
        json={"id": world_id, "name": "Test A", "baseUrl": "http://127.0.0.1:8786"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def connect(client, world):
    return client.websocket_connect(
        f"ws://127.0.0.1/api/connect/{world['id']}",
        headers={"Authorization": f"Bearer {world['connectorToken']}"},
    )


def snapshot(world_id="test-a"):
    return {
        "type": "snapshot",
        "protocolVersion": 1,
        "worldId": world_id,
        "name": "Test A",
        "baseUrl": "http://127.0.0.1:8786",
        "version": "synthetic",
        "observedAt": "2026-01-02T03:04:05Z",
        "agents": [{"id": "helper", "name": "Helper", "description": "Synthetic agent"}],
        "conversations": [
            {
                "id": "c_test",
                "agentId": "helper",
                "title": "Synthetic conversation",
                "updatedAt": "2026-01-02T03:04:05Z",
                "status": "active",
                "pendingQuestion": None,
            }
        ],
        "questions": [
            {
                "conversationId": "c_test",
                "agentId": "helper",
                "title": "Synthetic conversation",
                "requestId": "request_test",
                "prompt": "Choose a test value",
                "inputType": "free_form",
                "choices": [],
                "allowMultiple": False,
            }
        ],
    }


def wait_for(read, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        time.sleep(0.005)
    pytest.fail(f"State did not converge: {value!r}")


def worlds(client, auth):
    response = client.get("/api/worlds", headers=auth)
    assert response.status_code == 200
    return response.json()["items"]


def publish(ws, client, auth, frame=None):
    frame = frame or snapshot()
    ws.send_json(frame)
    return wait_for(
        lambda: worlds(client, auth)[0],
        lambda world: world["snapshot"] is not None
        and world["snapshot"].get("observedAt") == frame.get("observedAt"),
    )


def command_body(action="send_message"):
    if action == "start_conversation":
        return {"action": action, "payload": {"agentId": "helper", "title": "Test", "message": "Hello"}}
    if action == "answer_question":
        return {
            "action": action,
            "payload": {
                "agentId": "helper",
                "conversationId": "c_test",
                "requestId": "request_test",
                "text": "A test answer",
                "selectedValues": [],
                "cancelled": False,
            },
        }
    return {"action": action, "payload": {"agentId": "helper", "conversationId": "c_test", "message": "Hello"}}


def submit(client, auth, body=None, key=None, world_id="test-a"):
    return client.post(
        f"/api/worlds/{world_id}/commands",
        headers={**auth, "Idempotency-Key": key or secrets.token_urlsafe(16)},
        json=body or command_body(),
    )


def get_command(client, auth, command_id):
    response = client.get(f"/api/commands/{command_id}", headers=auth)
    assert response.status_code == 200
    return response.json()


def test_factory_is_lazy_and_environment_friendly(tmp_path, monkeypatch):
    database = tmp_path / "nested" / "hub.sqlite3"
    app = create_app()
    monkeypatch.setenv("HUB_DB_PATH", str(database))
    monkeypatch.setenv("HUB_PUBLIC_URL", "http://127.0.0.1")
    monkeypatch.setenv("HUB_CONNECTOR_PUBLIC_URL", "http://127.0.0.1:8081")
    monkeypatch.setenv("HUB_COMMIT", "test-commit")
    assert not database.exists()
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "version": (Path(__file__).parents[1] / "VERSION").read_text().strip(),
            "commit": "test-commit",
        }
    assert database.exists()


def test_missing_public_origins_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("HUB_PUBLIC_URL", raising=False)
    monkeypatch.delenv("HUB_CONNECTOR_PUBLIC_URL", raising=False)
    with pytest.raises(RuntimeError, match="HUB_PUBLIC_URL"):
        with TestClient(create_app(db_path=tmp_path / "hub.sqlite")):
            pass


@pytest.mark.parametrize("origin", ["http://remote.example", "https://hub.example/path", "https://user:pass@hub.example"])
def test_invalid_public_origins_fail_closed(tmp_path, origin):
    with pytest.raises((RuntimeError, ValueError)):
        with TestClient(create_app(db_path=tmp_path / "hub.sqlite", public_url=origin, connector_url="https://connect.example")):
            pass


def test_backend_expects_external_preauth_not_native_auth(client, auth):
    result=client.get("/api/me")
    assert result.status_code==200
    assert result.json()=={"authenticated":True,"authMode":"external-preauth","connectorUrl":"http://127.0.0.1:8081"}
    assert "set-cookie" not in result.headers
    assert client.post("/api/login",headers=auth,json={"token":"retired-token"}).status_code==404
    assert client.post("/api/logout",headers=auth,json={}).status_code==404


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://127.0.0.1.evil.example", "http://127.0.0.1:9999", "http://user@127.0.0.1", "http://127.0.0.1/path"])
def test_mutations_reject_foreign_origin(client, origin):
    response=client.post("/api/worlds",headers={"X-Hub-Request":"1","Origin":origin},json={})
    assert response.status_code==403


def test_mutations_require_header_and_exact_origin(client,auth):
    body={"id":"one","name":"One","baseUrl":"https://world.example"}
    assert client.post("/api/worlds",json=body).status_code==403
    assert client.post("/api/worlds",headers={"X-Hub-Request":"1"},json=body).status_code==403
    assert client.post("/api/worlds",headers={"Origin":"http://127.0.0.1"},json=body).status_code==403
    assert client.post("/api/worlds",headers={**auth,"Origin":"http://127.0.0.1:80"},json=body).status_code==201


def test_worlds_and_tokens_are_durable_and_secret_free(client, auth, settings):
    world = register(client, auth)
    assert len(world["connectorToken"]) >= 40
    assert client.post("/api/worlds", headers=auth, json={"id": "test-a", "name": "Duplicate", "baseUrl": "https://example.test"}).status_code == 409
    listed = worlds(client, auth)
    assert listed[0]["connected"] is False and listed[0]["lastSeen"] is None
    assert world["connectorToken"] not in json.dumps(listed)
    assert "connectorToken" not in listed[0]
    with sqlite3.connect(settings["db_path"]) as db:
        dump = "\n".join(db.iterdump())
    assert world["connectorToken"] not in dump
    assert "hub_session" not in dump and "CREATE TABLE sessions" not in dump


@pytest.mark.parametrize("base_url", ["javascript:alert(1)", "data:text/html,test", "file:///tmp/test", "ftp://example.test", "http://user:pass@example.test", "https://user@example.test", "http://example.test\\@evil.test", "http://example.test\n.evil.test", "http://", "http://example.test:bad", "//example.test", "http://%65xample.test"])
def test_registration_rejects_unsafe_urls(client, auth, base_url):
    response = client.post("/api/worlds", headers=auth, json={"id": "bad", "name": "Bad", "baseUrl": base_url})
    assert response.status_code == 422
    assert not worlds(client, auth)


@pytest.mark.parametrize("world_id", ["../escape", "a/b", "a b", "a" * 65, "", "bad?query"])
def test_registration_rejects_unsafe_ids(client, auth, world_id):
    assert client.post("/api/worlds", headers=auth, json={"id": world_id, "name": "Bad", "baseUrl": "https://example.test"}).status_code == 422


def test_world_token_cannot_access_operator_or_another_world(client, auth):
    one = register(client, auth)
    two = register(client, auth, "test-b")
    connector_auth = {"Authorization": f"Bearer {one['connectorToken']}"}
    assert client.post("/api/worlds", headers=connector_auth, json={}).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("ws://127.0.0.1/api/connect/test-b", headers=connector_auth):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("ws://127.0.0.1/api/connect/test-a", headers={"Authorization":"Bearer retired-operator-token"}):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"ws://127.0.0.1/api/connect/test-a?token={one['connectorToken']}"):
            pass
    assert all(not world["connected"] and world["snapshot"] is None for world in worlds(client, auth))
    assert two["connectorToken"] != one["connectorToken"]


def test_duplicate_connection_rejected_without_displacing_first(client, auth):
    world = register(client, auth)
    with connect(client, world) as ws:
        publish(ws, client, auth)
        with pytest.raises(WebSocketDisconnect):
            with connect(client, world):
                pass
        assert worlds(client, auth)[0]["connected"] is True
        response = submit(client, auth)
        assert response.status_code == 202
        assert ws.receive_json()["id"] == response.json()["id"]


def test_nonloopback_plain_websocket_requires_explicit_development_flag(client, auth, monkeypatch, settings):
    # A non-loopback socket remains denied even with a spoofed loopback Host.
    with TestClient(create_app(**{**settings, "db_path": settings["db_path"].with_name("remote.sqlite3")}), client=("198.51.100.10", 50000)) as remote:
        world = register(remote, auth)
        for host in ("hub.example", "localhost", "127.0.0.1"):
            with pytest.raises(WebSocketDisconnect):
                with remote.websocket_connect(f"ws://{host}/api/connect/test-a", headers={"Authorization": f"Bearer {world['connectorToken']}"}):
                    pass
    monkeypatch.setenv("HUB_ALLOW_INSECURE_WS", "1")
    with TestClient(create_app(**{**settings, "db_path": settings["db_path"].with_name("dev.sqlite3")})) as dev:
        world = register(dev, auth)
        with dev.websocket_connect("ws://hub.example/api/connect/test-a", headers={"Authorization": f"Bearer {world['connectorToken']}"}):
            pass


def test_snapshot_is_normalized_and_uses_receipt_time(client, auth):
    world = register(client, auth)
    frame = snapshot()
    frame["config"] = {"secret": "NEVER_STORE_THIS"}
    frame["transcript"] = "NEVER_STORE_THIS"
    frame["agents"][0]["tools"] = "NEVER_STORE_THIS"
    frame["conversations"][0]["messages"] = ["NEVER_STORE_THIS"]
    with connect(client, world) as ws:
        state = publish(ws, client, auth, frame)
        assert state["connected"] is True
        assert state["lastSeen"] != frame["observedAt"]
        assert state["snapshot"]["observedAt"] == frame["observedAt"]
        assert "NEVER_STORE_THIS" not in json.dumps(state)
        assert set(state["snapshot"]["agents"][0]) == {"id", "name", "description"}
    state = wait_for(lambda: worlds(client, auth)[0], lambda value: not value["connected"])
    assert state["snapshot"]["conversations"][0]["id"] == "c_test"


def test_snapshot_clamps_strings_and_arrays(client, auth):
    world = register(client, auth)
    frame = snapshot()
    frame["agents"] = [{"id": f"agent-{i}", "name": "N" * 1000, "description": "D" * 1000} for i in range(110)]
    frame["conversations"] = [{"id": f"c_{i}", "agentId": "agent-0", "title": "T" * 1000, "status": "active"} for i in range(110)]
    frame["questions"] = [{"conversationId": "c_0", "agentId": "agent-0", "requestId": f"q_{i}", "prompt": "P" * 5000, "choices": []} for i in range(110)]
    assert len(json.dumps(frame).encode()) < 1024 * 1024
    with connect(client, world) as ws:
        state = publish(ws, client, auth, frame)["snapshot"]
        assert len(state["agents"]) == 100
        assert len(state["conversations"]) == 100
        assert len(state["questions"]) <= 100
        assert len(state["agents"][0]["name"]) <= 256
        assert len(state["agents"][0]["description"]) <= 512
        assert len(state["conversations"][0]["title"]) <= 256
        assert len(state["questions"][0]["prompt"]) <= 4000


def test_partial_snapshot_clears_old_questions_without_fabricating_freshness(client, auth):
    world = register(client, auth)
    with connect(client, world) as ws:
        publish(ws, client, auth)
        ws.send_json({"type": "snapshot", "protocolVersion": 1, "worldId": "test-a", "agents": snapshot()["agents"]})
        state = wait_for(lambda: worlds(client, auth)[0]["snapshot"], lambda value: value.get("questions") == [])
        assert state["observedAt"] is None and state["conversations"] == []
        assert state["agents"][0]["id"] == "helper"


@pytest.mark.parametrize("change", [
    {"worldId": "other-world"},
    {"protocolVersion": 2},
    {"protocolVersion": True},
    {"baseUrl": "javascript:alert(1)"},
    {"baseUrl": "http://user:secret@example.test"},
    {"agents": {}},
    {"observedAt": "not-a-date"},
])
def test_bad_snapshot_disconnects_and_preserves_last_good_state(client, auth, change):
    world = register(client, auth)
    with connect(client, world) as ws:
        previous = publish(ws, client, auth)["snapshot"]
        frame = {**snapshot(), **change}
        ws.send_json(frame)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    state = worlds(client, auth)[0]
    assert state["snapshot"] == previous
    assert state["connected"] is False


def test_hidden_disabled_nonuser_and_nested_items_are_not_exported(client, auth):
    world = register(client, auth)
    frame = snapshot()
    frame["agents"] += [{"id": "disabled", "enabled": False}, {"id": "hidden", "visible": False}]
    for suffix, extras in [("nested", {"parentId": "c_test"}), ("internal", {"userFacing": False}), ("closed", {"status": "archived"}), ("owner", {"agentId": "disabled"})]:
        frame["conversations"].append({**frame["conversations"][0], "id": suffix, **extras})
    frame["questions"].append({**frame["questions"][0], "agentId": "disabled"})
    with connect(client, world) as ws:
        state = publish(ws, client, auth, frame)["snapshot"]
        assert [item["id"] for item in state["agents"]] == ["helper"]
        assert [item["id"] for item in state["conversations"]] == ["c_test"]
        assert len(state["questions"]) == 1


@pytest.mark.parametrize("frame", ["x" * (1024 * 1024 + 1), "é" * (512 * 1024 + 1)], ids=["ascii", "utf8"])
def test_oversize_complete_frames_close_with_1009(client, auth, frame):
    world = register(client, auth)
    with connect(client, world) as ws:
        ws.send_text(frame)
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 1009
    assert worlds(client, auth)[0]["snapshot"] is None


@pytest.mark.parametrize("frame", ["not-json", "[]", '{"type":"unknown"}', '{"type":"snapshot","protocolVersion":NaN}'])
def test_malformed_frames_fail_closed_without_echo(client, auth, frame):
    world = register(client, auth)
    with connect(client, world) as ws:
        ws.send_text(frame)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert worlds(client, auth)[0]["snapshot"] is None


def test_offline_commands_and_missing_idempotency_rejected(client, auth):
    register(client, auth)
    assert submit(client, auth).status_code == 409
    assert client.post("/api/worlds/test-a/commands", headers=auth, json=command_body()).status_code == 422
    assert client.get("/api/commands/not-found", headers=auth).status_code == 404


@pytest.mark.parametrize("action", ["start_conversation", "send_message", "answer_question"])
def test_command_queue_and_successful_admission(client, auth, action):
    world = register(client, auth)
    with connect(client, world) as ws:
        publish(ws, client, auth)
        body = command_body(action)
        response = submit(client, auth, body)
        assert response.status_code == 202, response.text
        resource = response.json()
        assert resource["status"] in {"queued", "sent"}
        frame = ws.receive_json()
        assert frame == {"type": "command", "id": resource["id"], **body}
        assert get_command(client, auth, resource["id"])["status"] == "sent"
        ws.send_json({"type": "result", "id": resource["id"], "ok": True, "data": {"conversationId": "c_test", "status": "accepted", "transcript": "NEVER_STORE_RESULT"}})
        result = wait_for(lambda: get_command(client, auth, resource["id"]), lambda value: value["status"] == "succeeded")
        assert result["result"] == {"conversationId": "c_test", "status": "accepted"}
        assert result["error"] is None
        assert "NEVER_STORE_RESULT" not in json.dumps(result)


def test_failed_result_uses_only_a_safe_error(client, auth):
    world = register(client, auth)
    with connect(client, world) as ws:
        publish(ws, client, auth)
        resource = submit(client, auth).json()
        ws.receive_json()
        ws.send_json({"type": "result", "id": resource["id"], "ok": False, "error": "NEVER_STORE_SECRET_ERROR"})
        result = wait_for(lambda: get_command(client, auth, resource["id"]), lambda value: value["status"] == "failed")
        assert result["error"] == "command_failed" and result["result"] is None
        assert "NEVER_STORE_SECRET_ERROR" not in client.get("/api/activity", headers=auth).text


def test_idempotency_replay_and_mismatch(client, auth):
    world = register(client, auth)
    key = secrets.token_urlsafe(16)
    with connect(client, world) as ws:
        publish(ws, client, auth)
        first = submit(client, auth, key=key)
        frame = ws.receive_json()
        replay = submit(client, auth, key=key)
        assert first.status_code == replay.status_code == 202
        assert first.json()["id"] == replay.json()["id"] == frame["id"]
        different = command_body()
        different["payload"]["message"] = "Different"
        assert submit(client, auth, different, key).status_code == 409
        ws.send_json({"type": "result", "id": frame["id"], "ok": True, "data": {"status": "accepted"}})
        wait_for(lambda: get_command(client, auth, frame["id"]), lambda value: value["status"] == "succeeded")
    replay = submit(client, auth, key=key)
    assert replay.status_code == 202 and replay.json()["id"] == frame["id"]
    assert replay.json()["status"] == "succeeded"
    register(client, auth, "other")
    assert submit(client, auth, key=key, world_id="other").status_code == 409


@pytest.mark.parametrize("change", [
    {"action": "shell"},
    {"payload": {"agentId": "helper", "conversationId": "c_test", "message": "Hi", "url": "http://example.test"}},
    {"payload": {"agentId": "other", "conversationId": "c_test", "message": "Hi"}},
    {"payload": {"agentId": "helper", "conversationId": "other", "message": "Hi"}},
    {"payload": {"agentId": "helper", "conversationId": "c_test", "message": "x" * 16385}},
    {"worldId": "other"},
])
def test_command_payload_allowlist_and_scope(client, auth, change):
    world = register(client, auth)
    with connect(client, world) as ws:
        publish(ws, client, auth)
        response = submit(client, auth, {**command_body(), **change})
        assert response.status_code in {409, 422}


def test_answer_requires_current_request_and_owner(client, auth):
    world = register(client, auth)
    with connect(client, world) as ws:
        publish(ws, client, auth)
        body = command_body("answer_question")
        body["payload"]["requestId"] = "stale"
        assert submit(client, auth, body).status_code == 409


def test_disconnect_and_reconnect_never_replay_uncertain_commands(client, auth):
    world = register(client, auth)
    key = secrets.token_urlsafe(16)
    with connect(client, world) as ws:
        publish(ws, client, auth)
        response = submit(client, auth, key=key)
        command_id = ws.receive_json()["id"]
        assert response.json()["id"] == command_id
    wait_for(lambda: get_command(client, auth, command_id), lambda value: value["status"] == "uncertain")
    with connect(client, world) as ws:
        publish(ws, client, auth)
        replay = submit(client, auth, key=key)
        assert replay.json()["status"] == "uncertain" and replay.json()["id"] == command_id
        fresh = submit(client, auth)
        assert ws.receive_json()["id"] == fresh.json()["id"]
        assert get_command(client, auth, command_id)["status"] == "uncertain"


def test_timeout_is_uncertain_and_late_result_cannot_reset_status(client, auth):
    world = register(client, auth)
    with connect(client, world) as ws:
        publish(ws, client, auth)
        command_id = submit(client, auth).json()["id"]
        ws.receive_json()
        wait_for(lambda: get_command(client, auth, command_id), lambda value: value["status"] == "uncertain")
        ws.send_json({"type": "result", "id": command_id, "ok": True, "data": {"status": "accepted"}})
        later = submit(client, auth)
        assert ws.receive_json()["id"] == later.json()["id"]
        assert get_command(client, auth, command_id)["status"] == "uncertain"


def test_result_is_scoped_to_its_authenticated_connection(client, auth):
    one = register(client, auth)
    two = register(client, auth, "test-b")
    with connect(client, one) as first, connect(client, two) as second:
        publish(first, client, auth)
        second.send_json(snapshot("test-b"))
        wait_for(lambda: worlds(client, auth)[1], lambda value: value["snapshot"] is not None)
        command_id = submit(client, auth).json()["id"]
        first.receive_json()
        second.send_json({"type": "result", "id": command_id, "ok": True, "data": {"status": "accepted"}})
        with pytest.raises(WebSocketDisconnect):
            second.receive_json()
        assert get_command(client, auth, command_id)["status"] == "sent"


def test_rotation_disconnect_and_revoke(client, auth):
    world = register(client, auth)
    with connect(client, world) as ws:
        publish(ws, client, auth)
        command_id = submit(client, auth).json()["id"]
        ws.receive_json()
        rotation = client.post("/api/worlds/test-a/rotate-token", headers=auth)
        assert rotation.status_code == 200
        rotated = rotation.json()
        assert rotated["connectorToken"] != world["connectorToken"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert get_command(client, auth, command_id)["status"] == "uncertain"
    with pytest.raises(WebSocketDisconnect):
        with connect(client, world):
            pass
    with connect(client, rotated) as ws:
        publish(ws, client, auth)
        assert client.post("/api/worlds/test-a/disconnect", headers=auth).status_code == 200
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    with connect(client, rotated) as ws:
        assert client.delete("/api/worlds/test-a", headers=auth).status_code == 204
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert worlds(client, auth) == []
    assert submit(client, auth).status_code in {404, 409}
    with pytest.raises(WebSocketDisconnect):
        with connect(client, rotated):
            pass


def test_activity_is_bounded_paginated_filtered_and_redacted(client, auth):
    tokens = []
    for index in range(16):
        tokens.append(register(client, auth, f"world-{index}")["connectorToken"])
    first = client.get("/api/activity?limit=5", headers=auth).json()
    assert len(first["items"]) == 5
    assert first["nextCursor"] is not None
    second = client.get(f"/api/activity?limit=5&before={first['nextCursor']}", headers=auth).json()
    assert len(second["items"]) == 5
    assert not ({row["id"] for row in first["items"]} & {row["id"] for row in second["items"]})
    all_rows = client.get("/api/activity?limit=200", headers=auth).json()["items"]
    assert len(all_rows) == 12
    assert all_rows == sorted(all_rows, key=lambda row: row["id"], reverse=True)
    filtered = client.get("/api/activity?worldId=world-15", headers=auth).json()["items"]
    assert filtered and all(row["worldId"] == "world-15" for row in filtered)
    for row in all_rows:
        assert set(row) == {"id", "worldId", "kind", "summary", "createdAt", "conversationId", "agentId"}
        assert len(row["summary"]) <= 512
    assert not any(token in json.dumps(all_rows) for token in tokens)


def test_repeated_identical_snapshot_does_not_spam_activity(client, auth):
    world = register(client, auth)
    with connect(client, world) as ws:
        publish(ws, client, auth)
        first = client.get("/api/activity", headers=auth).json()["items"]
        frame = snapshot()
        frame["observedAt"] = "2026-01-02T03:04:06Z"
        publish(ws, client, auth, frame)
        assert client.get("/api/activity", headers=auth).json()["items"] == first


def test_restart_preserves_snapshot_history_idempotency_and_uncertainty(settings, auth):
    key = secrets.token_urlsafe(16)
    with TestClient(create_app(**settings), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        world = register(client, auth)
        with connect(client, world) as ws:
            previous = publish(ws, client, auth)["snapshot"]
            command_id = submit(client, auth, key=key).json()["id"]
            ws.receive_json()
    with TestClient(create_app(**settings), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        state = worlds(client, auth)[0]
        assert state["snapshot"] == previous and state["connected"] is False
        assert get_command(client, auth, command_id)["status"] == "uncertain"
        assert client.get("/api/activity", headers=auth).json()["items"]
        with connect(client, world) as ws:
            publish(ws, client, auth)
            assert submit(client, auth, key=key).json()["status"] == "uncertain"
            fresh = submit(client, auth)
            assert ws.receive_json()["id"] == fresh.json()["id"]


def test_oversize_http_body_and_malformed_json_do_not_echo_secrets(client, auth):
    response = client.post("/api/worlds", headers=auth, content="x" * (65536 + 1))
    assert response.status_code == 413
    response = client.post("/api/worlds", headers=auth, content='{"token":"NEVER_ECHO_SECRET",')
    assert response.status_code == 400
    assert "NEVER_ECHO_SECRET" not in response.text


def test_container_database_and_insecure_test_env_aliases(tmp_path, monkeypatch):
    database = tmp_path / "container" / "hub.sqlite"
    monkeypatch.delenv("HUB_DB_PATH", raising=False)
    monkeypatch.delenv("HUB_ALLOW_INSECURE_WS", raising=False)
    monkeypatch.setenv("HUB_DB", str(database))
    monkeypatch.setenv("HUB_ALLOW_INSECURE", "true")
    with TestClient(create_app(public_url="http://127.0.0.1", connector_url="http://127.0.0.1:8081")) as instance:
        assert instance.app.state.allow_insecure_ws is True
    assert database.exists()


def test_enrollment_matches_operator_script_token_field(client, auth):
    world = register(client, auth, "home.lab")
    assert len(world["token"]) >= 40
    assert "token" not in worlds(client, auth)[0]


def test_root_static_and_theme_bootstrap_are_public(client):
    response = client.get("/")
    assert response.status_code == 200 and '<html' in response.text
    assert client.get("/static/theme.js").status_code == 200
    assert client.get("/static/../app.py").status_code in {404, 401}
    import base64
    import hashlib
    import re
    match = re.search(r'<script\b[^>]*id="theme-init"[^>]*>(.*?)</script>', response.text, re.DOTALL)
    if match:
        allowed_hash = base64.b64encode(hashlib.sha256(match[1].encode()).digest()).decode()
        assert f"'sha256-{allowed_hash}'" in response.headers['content-security-policy']
    assert "'unsafe-inline'" not in response.headers['content-security-policy']


def test_legacy_sessions_removed_without_world_data_loss(settings):
    from hub.security import digest
    from hub.store import Store
    with sqlite3.connect(settings["db_path"]) as db:
        db.execute("CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL)")
        db.execute("INSERT INTO sessions VALUES (?,?)",(digest("retired"), time.time()+3600))
    with TestClient(create_app(**settings)) as client:
        with sqlite3.connect(settings["db_path"]) as db:
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name='sessions'").fetchone() is None


@pytest.mark.parametrize("raw", [
    '{"token":"\\ud800"}',
    '{"token":"first","token":"second"}',
    '{"token":1e999}',
    '{"token":' + '1' * 1000 + '}',
    '{"token":' + '[' * 100 + '0' + ']' * 100 + '}',
], ids=["surrogate", "duplicate-key", "infinity", "large-integer", "deep-nesting"])
def test_invalid_json_never_becomes_a_server_error_or_echo(client, auth, raw):
    response = client.post("/api/worlds", content=raw, headers={**auth,"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid JSON"}


def test_current_socket_needs_a_fresh_snapshot_before_command_admission(client, auth):
    world = register(client, auth)
    with connect(client, world) as ws:
        publish(ws, client, auth)
    with connect(client, world) as ws:
        assert submit(client, auth).status_code == 409
        publish(ws, client, auth)
        assert submit(client, auth).status_code == 202
        ws.receive_json()


def test_pending_questions_outside_recent_conversation_list_are_retained(client, auth):
    world = register(client, auth)
    frame = snapshot()
    frame["conversations"] = []
    with connect(client, world) as ws:
        state = publish(ws, client, auth, frame)["snapshot"]
        assert state["questions"][0]["conversationId"] == "c_test"
        response = submit(client, auth, command_body("answer_question"))
        assert response.status_code == 202
        assert ws.receive_json()["id"] == response.json()["id"]


def test_repeated_results_cannot_change_a_terminal_outcome(client, auth):
    world = register(client, auth)
    with connect(client, world) as ws:
        publish(ws, client, auth)
        command_id = submit(client, auth).json()["id"]
        ws.receive_json()
        ws.send_json({"type": "result", "id": command_id, "ok": False, "error": "queue_full"})
        wait_for(lambda: get_command(client, auth, command_id), lambda value: value["status"] == "failed")
        ws.send_json({"type": "result", "id": command_id, "ok": True, "data": {"status": "accepted"}})
        fresh = submit(client, auth)
        assert ws.receive_json()["id"] == fresh.json()["id"]
        assert get_command(client, auth, command_id)["status"] == "failed"
