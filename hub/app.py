"""FastAPI application factory; environment and SQLite are opened at startup only."""

from __future__ import annotations

from contextlib import asynccontextmanager
import base64
import hashlib
import os
from pathlib import Path
import re

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .normalize import command_in_scope, normalize_command
from .router import Router
from .security import (
    InvalidInput, MAX_HTTP_BODY, WORLD_ID, bearer, canonical_json, digest,
    new_token, parse_json, same_origin, secure_equal, validate_base_url, websocket_transport_allowed,
)
from .store import Store


ROOT = Path(__file__).resolve().parents[1]
MUTATIONS = {"POST", "PUT", "PATCH", "DELETE"}


def _problem(status: int, detail: str):
    return JSONResponse({"detail": detail}, status_code=status, headers={"Cache-Control": "no-store"})


def _positive_number(value, name: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
        if not minimum <= parsed <= maximum:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise RuntimeError(f"Invalid {name}") from None


async def _body(request: Request) -> dict:
    try:
        value = parse_json(await request.body())
    except InvalidInput:
        raise HTTPException(400, "Invalid JSON") from None
    if not isinstance(value, dict):
        raise HTTPException(422, "Expected a JSON object")
    return value


def create_app(
    db_path: str | Path | None = None,
    *,
    public_url: str | None = None,
    connector_url: str | None = None,
    command_timeout: float | None = None,
    activity_limit: int | None = None,
    static_dir: str | Path | None = None,
) -> FastAPI:
    """Use explicit synthetic settings in tests, or HUB_* environment/secret files.

    Defaults are resolved in lifespan, not on module import or factory creation.
    No operator credentials or browser sessions exist here. External preauthentication
    MUST protect the private backend; the provided ingress enforces that boundary.
    """
    assets = Path(static_dir) if static_dir is not None else ROOT / "hub" / "static"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        script_sources = "'self'"
        index_path = assets / "index.html"
        if index_path.is_file():
            bootstrap = re.search(r'<script\b[^>]*id="theme-init"[^>]*>(.*?)</script>', index_path.read_text(encoding="utf-8"), re.DOTALL)
            if bootstrap:
                script_hash = base64.b64encode(hashlib.sha256(bootstrap[1].encode()).digest()).decode()
                script_sources += f" 'sha256-{script_hash}'"
        app.state.csp = f"default-src 'self'; script-src {script_sources}; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        operator = public_url if public_url is not None else os.getenv("HUB_PUBLIC_URL", "")
        connector = connector_url if connector_url is not None else os.getenv("HUB_CONNECTOR_PUBLIC_URL", "")
        if not operator or not connector:
            raise RuntimeError("HUB_PUBLIC_URL and HUB_CONNECTOR_PUBLIC_URL are required")
        app.state.public_url = validate_base_url(operator)
        app.state.connector_url = validate_base_url(connector)
        from urllib.parse import urlsplit
        for value in (app.state.public_url, app.state.connector_url):
            parts = urlsplit(value)
            if parts.path or (parts.scheme != "https" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}):
                raise RuntimeError("Public origins require HTTPS (HTTP is loopback-test only), without a path")
        app.state.trusted_proxy_cidrs = os.getenv("HUB_TRUSTED_PROXY_CIDRS", "")
        app.state.commit = os.getenv("HUB_COMMIT", "unknown")[:128]
        app.state.allow_insecure_ws = os.getenv("HUB_ALLOW_INSECURE_WS", os.getenv("HUB_ALLOW_INSECURE", "")).lower() in {"1", "true"}
        deadline = _positive_number(command_timeout if command_timeout is not None else os.getenv("HUB_COMMAND_TIMEOUT_SECONDS", "30"), "command timeout", 0.01, 300)
        cap = int(_positive_number(activity_limit if activity_limit is not None else os.getenv("HUB_ACTIVITY_LIMIT", "1000"), "activity limit", 1, 10_000))
        app.state.store = Store(db_path if db_path is not None else os.getenv("HUB_DB_PATH", os.getenv("HUB_DB", "data/hub.sqlite3")), activity_limit=cap)
        app.state.router = Router(app.state.store, deadline)
        try:
            yield
        finally:
            await app.state.router.close()
            app.state.store.close()

    app = FastAPI(title="BotNexusHub", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        path = request.url.path
        is_api = path == "/api" or path.startswith("/api/")
        # Authentication is deliberately external. Keep CSRF independent of it:
        # an SSO cookie is an ambient browser credential, even without Hub cookies.
        if is_api and request.method in MUTATIONS:
            if request.headers.get("X-Hub-Request") != "1":
                return _problem(403, "X-Hub-Request: 1 is required")
            if not same_origin(request.headers.get("origin", ""), app.state.public_url):
                return _problem(403, "Same-origin request required")
        if is_api and request.method in MUTATIONS:
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) < 0:
                        raise ValueError
                    if int(length) > MAX_HTTP_BODY:
                        return _problem(413, "Request body too large")
                except ValueError:
                    return _problem(400, "Invalid content length")
            parts, total = [], 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_HTTP_BODY:
                    return _problem(413, "Request body too large")
                parts.append(chunk)
            # Starlette replays this cached body to downstream, including chunked input.
            request._body = b"".join(parts)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = app.state.csp
        if is_api:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(InvalidInput)
    async def invalid_input(request, error):
        return _problem(422, str(error))

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": app.state.version, "commit": app.state.commit}

    @app.get("/api/me")
    async def me():
        # This is deployment status, not a local identity or login mechanism.
        return {"authenticated": True, "authMode": "external-preauth", "connectorUrl": app.state.connector_url}

    def world_resource(world):
        return {**world, "connected": app.state.router.connected(world["id"])}

    def require_world(world_id: str):
        world = app.state.store.world(world_id)
        if world is None:
            raise HTTPException(404, "World not found")
        return world

    @app.get("/api/worlds")
    async def list_worlds():
        return {"items": [world_resource(world) for world in app.state.store.worlds()]}

    @app.post("/api/worlds", status_code=201)
    async def add_world(request: Request):
        body = await _body(request)
        if set(body) != {"id", "name", "baseUrl"}:
            raise InvalidInput("Expected id, name and baseUrl")
        world_id, name = body["id"], body["name"]
        if not isinstance(world_id, str) or not WORLD_ID.fullmatch(world_id):
            raise InvalidInput("Invalid world ID")
        if not isinstance(name, str) or not name.strip() or len(name) > 256 or any(ord(char) < 32 for char in name):
            raise InvalidInput("Invalid world name")
        base_url = validate_base_url(body["baseUrl"])
        token = new_token()
        if not app.state.store.create_world(world_id, name, base_url, token):
            raise HTTPException(409, "World already registered")
        return {**world_resource(require_world(world_id)), "token": token, "connectorToken": token}

    @app.post("/api/worlds/{world_id}/rotate-token")
    async def rotate_token(world_id: str):
        require_world(world_id)
        token = new_token()
        app.state.store.rotate_token(world_id, token)
        await app.state.router.disconnect(world_id, "token_rotated")
        return {**world_resource(require_world(world_id)), "token": token, "connectorToken": token}

    @app.post("/api/worlds/{world_id}/disconnect")
    async def disconnect(world_id: str):
        require_world(world_id)
        await app.state.router.disconnect(world_id, "operator_disconnected")
        return world_resource(require_world(world_id))

    @app.delete("/api/worlds/{world_id}", status_code=204)
    async def revoke(world_id: str):
        require_world(world_id)
        app.state.store.delete_world(world_id)
        await app.state.router.disconnect(world_id, "world_revoked")
        return None

    @app.get("/api/activity")
    async def activity(request: Request):
        params = request.query_params
        try:
            limit = int(params.get("limit", "50"))
            before = int(params["before"]) if "before" in params else None
            if not 1 <= limit <= 200 or (before is not None and before <= 0):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "Invalid activity pagination") from None
        world_id = params.get("worldId") or None
        if world_id is not None and not WORLD_ID.fullmatch(world_id):
            raise HTTPException(422, "Invalid world ID")
        return app.state.store.activity(world_id=world_id, limit=limit, before=before)

    @app.post("/api/worlds/{world_id}/commands", status_code=202)
    async def commands(world_id: str, request: Request):
        key = request.headers.get("Idempotency-Key", "")
        if not 1 <= len(key) <= 128 or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise HTTPException(422, "Idempotency-Key is required (1-128 printable characters)")
        body = await _body(request)
        # Fingerprint the actual JSON payload, not just its clamped/normalized fields.
        request_hash = digest(canonical_json({"worldId": world_id, "body": body}))
        existing = app.state.store.idempotent(key)
        if existing is not None:
            resource, previous_hash = existing
            if not secure_equal(previous_hash, request_hash):
                raise HTTPException(409, "Idempotency-Key was already used for a different request")
            return resource
        normalized = normalize_command(body)
        world = require_world(world_id)
        if not app.state.router.ready(world_id):
            raise HTTPException(409, "World is offline or has not supplied a current snapshot")
        if not command_in_scope(normalized, world["snapshot"]):
            raise HTTPException(409, "Command target is not in the current visible world snapshot")
        if not app.state.router.has_capacity(world_id):
            raise HTTPException(409, "World command queue is full")
        resource = app.state.store.create_command(world_id, normalized, key, request_hash)
        app.state.router.enqueue(world_id, resource["id"], normalized)
        return resource

    @app.get("/api/commands/{command_id}")
    async def command(command_id: str):
        resource = app.state.store.command(command_id)
        if resource is None:
            raise HTTPException(404, "Command not found")
        return resource

    @app.websocket("/api/connect/{world_id}")
    async def connect_world(websocket: WebSocket, world_id: str):
        supplied = bearer(websocket.headers)
        if not supplied or not app.state.store.authenticates_world(world_id, supplied) or not websocket_transport_allowed(websocket, app.state.allow_insecure_ws, app.state.connector_url, app.state.trusted_proxy_cidrs):
            await websocket.close(code=1008)
            return
        origin = websocket.headers.get("origin")
        equivalent_url = str(websocket.url).replace("wss://", "https://", 1).replace("ws://", "http://", 1)
        if origin is not None and not same_origin(origin, equivalent_url):
            await websocket.close(code=1008)
            return
        connection = app.state.router.claim(world_id, websocket)
        if connection is None:
            await websocket.close(code=1008)
            return
        await app.state.router.serve(connection)

    @app.get("/")
    async def index():
        if not (assets / "index.html").is_file():
            raise HTTPException(404, "UI assets are not installed")
        return FileResponse(assets / "index.html")

    app.mount("/static", StaticFiles(directory=str(assets), check_dir=False), name="static")
    return app


# ASGI compatibility for `uvicorn hub.app:app`. No I/O takes place on import.
app = create_app()
