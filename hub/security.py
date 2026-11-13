"""Authentication helpers. Never include supplied credentials in errors or logs."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
from urllib.parse import urlsplit, urlunsplit


MAX_HTTP_BODY = 64 * 1024
MAX_FRAME_BYTES = 1024 * 1024
WORLD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
ROUTING_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")


class InvalidInput(ValueError):
    """A fixed, safe validation failure, without the input value."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def secure_equal(one: str, two: str) -> bool:
    try:
        return hmac.compare_digest(digest(one), digest(two))
    except (UnicodeError, TypeError, AttributeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def bearer(headers) -> str | None:
    value = headers.get("authorization", "")
    scheme, separator, token = value.partition(" ")
    if separator and scheme.lower() == "bearer" and 0 < len(token) <= 4096:
        return token
    return None


def _url_parts(value: str):
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise InvalidInput("Invalid HTTP URL")
    if "\\" in value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise InvalidInput("Invalid HTTP URL")
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
    except (ValueError, UnicodeError):
        raise InvalidInput("Invalid HTTP URL") from None
    if parts.scheme not in {"http", "https"} or not host or parts.username is not None or parts.password is not None:
        raise InvalidInput("Invalid HTTP URL")
    if "%" in host or (port is not None and not 1 <= port <= 65535):
        raise InvalidInput("Invalid HTTP URL")
    try:
        normalized_host = host.encode("idna").decode("ascii").lower()
        if ":" in normalized_host:
            ipaddress.IPv6Address(normalized_host)
        elif not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in normalized_host.rstrip(".").split(".")):
            raise ValueError
    except (ValueError, UnicodeError):
        raise InvalidInput("Invalid HTTP URL") from None
    return parts, normalized_host, port


def validate_base_url(value: str) -> str:
    parts, host, port = _url_parts(value)
    if parts.query or parts.fragment:
        raise InvalidInput("Base URL cannot contain a query or fragment")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def same_origin(origin: str, request_url: str) -> bool:
    try:
        supplied, host, port = _url_parts(origin)
        expected, expected_host, expected_port = _url_parts(request_url)
        if supplied.path or supplied.query or supplied.fragment:
            return False
        return (supplied.scheme, host, port or (443 if supplied.scheme == "https" else 80)) == (
            expected.scheme, expected_host, expected_port or (443 if expected.scheme == "https" else 80)
        )
    except InvalidInput:
        return False


def websocket_transport_allowed(websocket, allow_insecure: bool, public_url=None, trusted_cidrs="") -> bool:
    if websocket.url.scheme == "wss" or allow_insecure:
        return True
    # TLS may terminate at a configured proxy, but forwarded headers never grant trust.
    if public_url and public_url.startswith("https://") and websocket.client:
        try:
            client_ip = ipaddress.ip_address(websocket.client.host)
            if any(client_ip in ipaddress.ip_network(cidr.strip()) for cidr in trusted_cidrs.split(",") if cidr.strip()):
                return True
        except ValueError:
            pass
    try:
        return websocket.client is not None and ipaddress.ip_address(websocket.client.host).is_loopback
    except ValueError:
        return False


def routing_id(value) -> bool:
    return isinstance(value, str) and ROUTING_ID.fullmatch(value) is not None


def _no_constant(_):
    raise InvalidInput("Invalid JSON")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInput("Invalid JSON")
        result[key] = value
    return result


def _bounded_int(value: str):
    if len(value) > 128:
        raise InvalidInput("Invalid JSON")
    return int(value)


def _finite_float(value: str):
    result = float(value)
    if not math.isfinite(result):
        raise InvalidInput("Invalid JSON")
    return result


def parse_json(raw: bytes | str):
    try:
        value = json.loads(raw, parse_constant=_no_constant, parse_int=_bounded_int, parse_float=_finite_float, object_pairs_hook=_unique_object)
        pending, nodes = [(value, 0)], 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if depth > 32 or nodes > 100_000:
                raise InvalidInput("Invalid JSON")
            if isinstance(item, str):
                item.encode("utf-8")  # JSON permits escaped lone surrogates; this protocol does not.
            elif isinstance(item, dict):
                for key, child in item.items():
                    key.encode("utf-8")
                    pending.append((child, depth + 1))
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError):
        raise InvalidInput("Invalid JSON") from None


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
