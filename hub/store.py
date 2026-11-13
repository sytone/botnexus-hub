"""Durable, bounded SQLite state. Connector tokens are stored only as SHA256."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import uuid

from .security import canonical_json, digest, secure_equal, utc_now


class Store:
    def __init__(self, path: str | Path, *, activity_limit=1000):
        path = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.activity_limit = max(1, min(int(activity_limit), 10_000))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS worlds (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                last_seen TEXT,
                snapshot TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY,
                world_id TEXT NOT NULL,
                action TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued','sent','succeeded','failed','uncertain')),
                result TEXT,
                error TEXT,
                idempotency_key TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS commands_pending ON commands(world_id, status);
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                world_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                conversation_id TEXT,
                agent_id TEXT
            );
            CREATE INDEX IF NOT EXISTS activity_world ON activity(world_id, id DESC);
        """)
        # A previous process may have stopped between queue admission and a result.
        # Only pending commands change; terminal results and idempotency never reset.
        with self.lock, self.db:
            worlds = self.db.execute("SELECT DISTINCT world_id FROM commands WHERE status IN ('queued','sent')").fetchall()
            self.db.execute("UPDATE commands SET status='uncertain', error='hub_restarted', updated_at=? WHERE status IN ('queued','sent')", (utc_now(),))
            for world in worlds:
                self._activity(world["world_id"], "error", "Pending command outcome is uncertain after Hub restart")
            self._prune_activity()
            # Native sessions from pre-0.2 installations are no longer credentials.
            self.db.execute("DROP TABLE IF EXISTS sessions")

    def close(self):
        with self.lock:
            self.db.close()

    @staticmethod
    def _world(row) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"], "name": row["name"], "baseUrl": row["base_url"],
            "lastSeen": row["last_seen"], "snapshot": json.loads(row["snapshot"]) if row["snapshot"] else None,
        }

    def world(self, world_id: str) -> dict | None:
        with self.lock:
            return self._world(self.db.execute("SELECT * FROM worlds WHERE id=?", (world_id,)).fetchone())

    def worlds(self) -> list[dict]:
        with self.lock:
            return [self._world(row) for row in self.db.execute("SELECT * FROM worlds ORDER BY id")]

    def create_world(self, world_id: str, name: str, base_url: str, token: str) -> bool:
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM worlds WHERE id=?", (world_id,)).fetchone():
                return False
            self.db.execute("INSERT INTO worlds(id,name,base_url,token_hash,created_at) VALUES (?,?,?,?,?)", (world_id, name, base_url, digest(token), utc_now()))
            self._activity(world_id, "registration", "World registered")
            return True

    def authenticates_world(self, world_id: str, token: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT token_hash FROM worlds WHERE id=?", (world_id,)).fetchone()
            return row is not None and secure_equal(row["token_hash"], digest(token))

    def rotate_token(self, world_id: str, token: str) -> bool:
        with self.lock, self.db:
            updated = self.db.execute("UPDATE worlds SET token_hash=? WHERE id=?", (digest(token), world_id)).rowcount
            if updated:
                self._activity(world_id, "token_rotated", "Connector token rotated")
            return bool(updated)

    def delete_world(self, world_id: str) -> bool:
        with self.lock, self.db:
            deleted = self.db.execute("DELETE FROM worlds WHERE id=?", (world_id,)).rowcount
            if deleted:
                self._activity(world_id, "revoked", "World access revoked")
            return bool(deleted)

    def received_snapshot(self, world_id: str, snapshot: dict):
        with self.lock, self.db:
            previous = self.world(world_id)
            if previous is None:
                return
            self.db.execute("UPDATE worlds SET last_seen=?,snapshot=? WHERE id=?", (utc_now(), canonical_json(snapshot), world_id))
            before = previous["snapshot"] or {}
            keys = ("agents", "conversations", "questions", "version")
            if any(before.get(key) != snapshot.get(key) for key in keys):
                self._activity(world_id, "summary", "World summary updated")

    def connected(self, world_id: str):
        with self.lock, self.db:
            self.db.execute("UPDATE worlds SET last_seen=? WHERE id=?", (utc_now(), world_id))
            self._activity(world_id, "connect", "World connector connected")

    def disconnected(self, world_id: str, reason="connector_disconnected"):
        with self.lock, self.db:
            self._mark_uncertain(world_id, reason)
            self._activity(world_id, "disconnect", "World connector disconnected")

    def _prune_activity(self):
        self.db.execute("DELETE FROM activity WHERE id NOT IN (SELECT id FROM activity ORDER BY id DESC LIMIT ?)", (self.activity_limit,))

    def _activity(self, world_id: str, kind: str, summary: str, conversation_id=None, agent_id=None):
        self.db.execute("INSERT INTO activity(world_id,kind,summary,created_at,conversation_id,agent_id) VALUES(?,?,?,?,?,?)", (world_id, kind, summary[:512], utc_now(), conversation_id, agent_id))
        self._prune_activity()

    def activity(self, *, world_id: str | None, limit: int, before: int | None) -> dict:
        conditions, parameters = [], []
        if world_id:
            conditions.append("world_id=?")
            parameters.append(world_id)
        if before is not None:
            conditions.append("id<?")
            parameters.append(before)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.lock:
            rows = self.db.execute("SELECT * FROM activity" + where + " ORDER BY id DESC LIMIT ?", (*parameters, limit + 1)).fetchall()
        items = [{
            "id": row["id"], "worldId": row["world_id"], "kind": row["kind"], "summary": row["summary"],
            "createdAt": row["created_at"], "conversationId": row["conversation_id"], "agentId": row["agent_id"],
        } for row in rows[:limit]]
        return {"items": items, "nextCursor": items[-1]["id"] if len(rows) > limit else None}

    @staticmethod
    def _command(row) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"], "status": row["status"], "action": row["action"], "worldId": row["world_id"],
            "result": json.loads(row["result"]) if row["result"] is not None else None, "error": row["error"],
        }

    def command(self, command_id: str) -> dict | None:
        with self.lock:
            return self._command(self.db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone())

    def idempotent(self, key: str) -> tuple[dict, str] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM commands WHERE idempotency_key=?", (digest(key),)).fetchone()
            return (self._command(row), row["request_hash"]) if row is not None else None

    def create_command(self, world_id: str, body: dict, key: str, request_hash: str) -> dict:
        command_id, now = str(uuid.uuid4()), utc_now()
        with self.lock, self.db:
            self.db.execute("""INSERT INTO commands(id,world_id,action,payload,status,idempotency_key,request_hash,created_at,updated_at)
                VALUES(?,?,?,?,'queued',?,?,?,?)""", (command_id, world_id, body["action"], canonical_json(body["payload"]), digest(key), request_hash, now, now))
            self._activity(world_id, "command_queued", "Command queued for connector", body["payload"].get("conversationId"), body["payload"]["agentId"])
            return self.command(command_id)

    def mark_sent(self, command_id: str) -> bool:
        with self.lock, self.db:
            return bool(self.db.execute("UPDATE commands SET status='sent',updated_at=? WHERE id=? AND status='queued'", (utc_now(), command_id)).rowcount)

    def command_result(self, world_id: str, command_id: str, ok: bool, result: dict | None, error: str | None) -> bool:
        with self.lock, self.db:
            status = "succeeded" if ok else ("uncertain" if error == "outcome_unknown" else "failed")
            changed = self.db.execute("UPDATE commands SET status=?,result=?,error=?,updated_at=? WHERE id=? AND world_id=? AND status='sent'", (status, canonical_json(result) if result is not None else None, error, utc_now(), command_id, world_id)).rowcount
            if changed:
                self._activity(world_id, "command_admitted" if ok else "error", "Connector admitted command" if ok else ("Command outcome is uncertain" if status == "uncertain" else "Connector rejected command"), (result or {}).get("conversationId"))
            return bool(changed)

    def _mark_uncertain(self, world_id: str, reason: str):
        changed = self.db.execute("UPDATE commands SET status='uncertain',error=?,updated_at=? WHERE world_id=? AND status IN ('queued','sent')", (reason, utc_now(), world_id)).rowcount
        if changed:
            self._activity(world_id, "error", "Pending command outcome is uncertain")

    def command_timeout(self, command_id: str):
        with self.lock, self.db:
            row = self.db.execute("SELECT world_id FROM commands WHERE id=? AND status IN ('queued','sent')", (command_id,)).fetchone()
            if row:
                self.db.execute("UPDATE commands SET status='uncertain',error='connector_timeout',updated_at=? WHERE id=? AND status IN ('queued','sent')", (utc_now(), command_id))
                self._activity(row["world_id"], "error", "Command timed out; outcome is uncertain")
