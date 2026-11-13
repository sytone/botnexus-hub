"""Explicit HTTP/WebSocket security acceptance against disposable ingress only.

This complements ASGI unit tests: run nginx and Hub on separate operator and
connector ports. Only a loopback, exactly test-a/test-b registry is accepted.
Credentials are read from a protected file, never argv or printed output.
"""
import argparse
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from e2e import HarnessError, require, require_synthetic_worlds, load_proxy_secret, validate_base_url

class NoRedirect(urllib.request.HTTPRedirectHandler):

    def redirect_request(self, *args, **kwargs):
        return None

class NoRedirectWebSocket(connect):
    # websockets 17.4 has no max_redirects argument. Its connect loop calls this
    # hook before opening another connection (also for same-origin redirects).
    # https://github.com/python-websockets/websockets/blob/17.4/src/websockets/asyncio/client.py
    def process_redirect(self, exc):
        if isinstance(exc, InvalidStatus) and 300 <= exc.response.status_code < 400:
            # Negative probes must not mistake a redirect for an access denial.
            return HarnessError('Fixture WebSocket redirect refused')
        return exc


def websocket_connect(base, path, headers=None):
    validate_base_url(base)
    require(path.startswith('/') and not path.startswith('//')
            and not any(char in path for char in '\\#')
            and not any(ord(char) <= 32 or ord(char) == 127 for char in path),
            'Only relative fixture WebSocket routes are allowed')
    return NoRedirectWebSocket(base.replace('http', 'ws', 1) + path,
                               additional_headers=dict(headers or {}),
                               proxy=None, open_timeout=6)


@contextmanager
def probe_world(req, operator, proxy, ident):
    failed = False
    try:
        # Creation can succeed even if its response is lost or malformed.
        (code, raw, _) = req(operator, '/api/worlds', proxy,
                             {'id': ident, 'name': 'Synthetic ingress probe',
                              'baseUrl': 'http://localhost:1'})
        require(code == 201, 'Acceptance check failed')
        token = json.loads(raw)['connectorToken']
        require(isinstance(token, str) and bool(token), 'Invalid synthetic probe enrollment response')
        yield token
    except BaseException:
        failed = True
        raise
    finally:
        try:
            cleanup = req(operator, '/api/worlds/' + ident, proxy, method='DELETE')[0]
            require(cleanup == 204, 'Synthetic probe cleanup failed')
        except Exception:
            if not failed:
                raise HarnessError('Synthetic probe cleanup failed') from None
            # Keep the original error and report cleanup independently, without
            # printing response bodies, headers, tokens, or exception details.
            print('Synthetic probe cleanup failed for ' + ident, file=sys.stderr)


def origin(value):
    p = urllib.parse.urlsplit(value)
    if p.scheme not in {'http', 'https'} or p.hostname not in {'127.0.0.1', 'localhost', '::1'} or p.path or p.query or p.fragment or p.username or p.password:
        raise argparse.ArgumentTypeError('A loopback ingress origin without a path is required')
    return value.rstrip('/')

def run(operator, connector, secret_file):
    validate_base_url(operator)
    validate_base_url(connector)
    secret = load_proxy_secret(secret_file)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    checks = 0

    def req(base, path, headers=None, body=None, method=None):
        headers = dict(headers or {})
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(base + path, headers=headers, data=json.dumps(body).encode() if body is not None else None, method=method)
        try:
            with opener.open(request, timeout=8) as r:
                return (r.status, r.read(), dict(r.headers))
        except urllib.error.HTTPError as e:
            return (e.code, e.read(), dict(e.headers))
    proxy = {'X-Hub-Proxy-Secret': secret, 'X-Hub-Request': '1', 'Origin': operator}
    (status, raw, _) = req(operator, '/api/worlds', proxy)
    require(status == 200, 'Acceptance check failed')
    worlds = json.loads(raw)['items']
    require_synthetic_worlds(worlds)
    require(len(worlds) == 2 and {w['id'] for w in worlds} == {'test-a', 'test-b'}, 'Refusing non-disposable registry')
    checks += 1
    for path in ['/', '/static/app.js', '/api/me', '/api/worlds', '/api/activity', '/api/commands/missing', '/.env', '/data/hub.sqlite', '/docs', '/openapi.json', '/api/not-a-route']:
        for forged in ({}, {'X-NetBird-User': 'spoof@example.invalid', 'X-Forwarded-For': '127.0.0.1', 'Host': 'hub.example.invalid'}, {'Authorization': 'Bearer retired-operator-token'}, {'X-Hub-Proxy-Secret': secret.swapcase()}):
            require(req(operator, path, forged)[0] == 403, (path, 'unprotected operator ingress'))
            checks += 1
    require(req(operator, '/health')[0] == 200, 'Acceptance check failed')
    require(req(operator, '/api/login', proxy, {'token': 'retired'})[0] == 404, 'Acceptance check failed')
    require(req(operator, '/api/logout', proxy, {})[0] == 404, 'Acceptance check failed')
    checks += 3
    for path in ['/', '/static/app.js', '/health', '/api/me', '/api/worlds', '/api/activity', '/api/commands/missing', '/api/worlds/test-a/rotate-token', '/docs', '/openapi.json', '/api/connect/test-a/../worlds', '/api/connect/test-a%2f..%2fworlds', '//api/worlds']:
        for headers in ({}, proxy, {'Host': 'hub.example.invalid', 'X-Forwarded-Host': 'hub.example.invalid', 'X-NetBird-User': 'spoof@example.invalid'}):
            require(req(connector, path, headers)[0] in {400, 403, 404}, (path, 'connector ingress exposed non-connector route'))
            checks += 1
    for headers in ({'X-Hub-Proxy-Secret': secret}, {**proxy, 'Origin': connector}, {**proxy, 'Origin': 'https://evil.example'}, {k: v for (k, v) in proxy.items() if k != 'X-Hub-Request'}):
        require(req(operator, '/api/worlds', headers, {})[0] == 403, 'Acceptance check failed')
        checks += 1
    require(req(operator, '/api/worlds', proxy, {'id': 'test-c', 'name': 'C', 'baseUrl': 'javascript:bad'})[0] == 422, 'Acceptance check failed')
    checks += 1

    async def ws_checks():
        count = 0

        async def rejected(base, path, token=None, extra=None):
            h = dict(extra or {})
            if token:
                h['Authorization'] = 'Bearer ' + token
            try:
                async with websocket_connect(base, path, h):
                    raise AssertionError('Unexpected accepted WebSocket')
            except (InvalidStatus, ConnectionClosed):
                pass
        for token in (None, 'not-a-world-token', secret):
            await rejected(connector, '/api/connect/test-a', token)
            count += 1
        await rejected(operator, '/api/connect/test-a', extra=proxy)
        count += 1
        ident = 'probe-' + uuid.uuid4().hex[:8]
        with probe_world(req, operator, proxy, ident) as token:
            await rejected(connector, '/api/connect/test-a', token)
            count += 1
            await rejected(connector, '/api/worlds', token)
            count += 1
            await rejected(connector, '/api/connect/' + ident + '?token=' + token)
            count += 1
            require(req(operator, '/api/worlds', {'Authorization': 'Bearer ' + token})[0] == 403, 'Acceptance check failed')
            count += 1
            async with websocket_connect(connector, '/api/connect/' + ident, {'Authorization': 'Bearer ' + token}) as ws:
                await ws.send(json.dumps({'type': 'snapshot', 'protocolVersion': 1, 'worldId': ident, 'observedAt': '2026-01-02T03:04:05Z', 'agents': [], 'conversations': [], 'questions': []}))
                count += 1
                await rejected(connector, '/api/connect/' + ident, token)
                count += 1
            (code, raw, _) = req(operator, '/api/worlds/' + ident + '/rotate-token', proxy, {})
            require(code == 200 and json.loads(raw)['connectorToken'] != token, 'Acceptance check failed')
            await rejected(connector, '/api/connect/' + ident, token)
            count += 1
        return count
    checks += asyncio.run(ws_checks())
    final = json.loads(req(operator, '/api/worlds', proxy)[1])['items']
    require({w['id'] for w in final} == {'test-a', 'test-b'}, 'Acceptance check failed')
    print(json.dumps({'checks': checks, 'result': 'passed', 'fixtureRemoved': True}))
if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--allow-disposable', action='store_true', required=True)
    p.add_argument('--base-url', type=origin, required=True)
    p.add_argument('--connector-url', type=origin, required=True)
    p.add_argument('--proxy-secret-file', type=Path, required=True)
    args = p.parse_args()
    run(args.base_url, args.connector_url, args.proxy_secret_file)
