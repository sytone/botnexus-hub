"""Local-only regressions for credential scope, redirects, and fixture cleanup."""
import asyncio
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

import browser_e2e as browser
import ingress_e2e as ingress
from e2e import HarnessError


REDIRECTS = (300, 301, 302, 303, 304, 307, 308, 399)
PORTAL = 'http://127.0.0.1:38001'
HANDOFF = PORTAL + '/agent/helper/conversation/synthetic-thread'
SYNTHETIC_SECRET = 'synthetic-proxy-proof-not-a-real-credential'


@pytest.mark.parametrize('status', REDIRECTS)
@pytest.mark.parametrize('cross_origin', (False, True), ids=('same-origin', 'cross-origin'))
def test_websocket_redirect_never_sends_a_second_request(status, cross_origin, monkeypatch):
    import websockets.asyncio.client as client

    def ambient_proxy_lookup(*args):
        pytest.fail('Fixture WebSockets must not consult ambient proxies')

    monkeypatch.setattr(client, 'get_proxy', ambient_proxy_lookup)

    async def scenario():
        first_requests, sink_requests, errors = [], [], []
        handlers = set()
        location = ''

        async def handle(reader, writer, source):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                request = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
                if source and request.startswith(b'GET /start HTTP/1.1\r\n'):
                    first_requests.append(request)
                    response = (f'HTTP/1.1 {status} Redirect\r\nLocation: {location}\r\n'
                                'Content-Length: 0\r\nConnection: close\r\n\r\n')
                else:
                    sink_requests.append(request)
                    response = 'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
                writer.write(response.encode('ascii'))
                await writer.drain()
            except Exception as error:
                errors.append(error)
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                finally:
                    handlers.discard(task)

        source = await asyncio.start_server(lambda r, w: handle(r, w, True), '127.0.0.1', 0)
        try:
            sink = await asyncio.start_server(lambda r, w: handle(r, w, False), '127.0.0.1', 0)
            async with source, sink:
                base = 'http://127.0.0.1:' + str(source.sockets[0].getsockname()[1])
                sink_base = 'http://127.0.0.1:' + str(sink.sockets[0].getsockname()[1])
                location = (sink_base if cross_origin else base) + '/sink'
                with pytest.raises(HarnessError, match='WebSocket redirect refused'):
                    async with ingress.websocket_connect(base, '/start', {
                        'X-Hub-Proxy-Secret': SYNTHETIC_SECRET,
                        'Authorization': 'Bearer synthetic-world-token',
                    }):
                        pytest.fail('A redirect must never open a fixture WebSocket')
                assert len(first_requests) == 1
                assert ('X-Hub-Proxy-Secret: ' + SYNTHETIC_SECRET).encode() in first_requests[0]
                assert b'Authorization: Bearer synthetic-world-token' in first_requests[0]
                # Includes /sink on the original listener as well as a different
                # origin. Zero second requests also means zero forwarded proof.
                assert sink_requests == []
                assert errors == []
        finally:
            source.close()
            await source.wait_closed()
            pending = list(handlers)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_websocket_helper_allows_fixture_handshake_and_normal_denial(monkeypatch):
    import websockets.asyncio.client as client
    from websockets.asyncio.server import serve
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    def ambient_proxy_lookup(*args):
        pytest.fail('Fixture WebSockets must not consult ambient proxies')
    monkeypatch.setattr(client, 'get_proxy', ambient_proxy_lookup)

    async def scenario():
        paths = []

        def process_request(connection, request):
            paths.append(request.path)
            if request.path == '/denied':
                return Response(403, 'Forbidden', Headers({'Content-Length': '0'}))

        async def handler(connection):
            await connection.send('synthetic fixture accepted')

        async with serve(handler, '127.0.0.1', 0, process_request=process_request) as server:
            base = 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
            async with ingress.websocket_connect(base, '/accepted') as connection:
                assert await connection.recv() == 'synthetic fixture accepted'
            with pytest.raises(ingress.InvalidStatus) as caught:
                async with ingress.websocket_connect(base, '/denied'):
                    pytest.fail('A denied fixture WebSocket must not open')
            assert caught.value.response.status_code == 403
            assert paths == ['/accepted', '/denied']

    asyncio.run(asyncio.wait_for(scenario(), 5))


def probe_request(enrollment, cleanup=(204, b'', {})):
    calls = []

    def request(base, path, headers=None, body=None, method=None):
        calls.append((base, path, headers, body, method))
        result = cleanup if method == 'DELETE' else enrollment
        if isinstance(result, BaseException):
            raise result
        return result
    return request, calls


@pytest.mark.parametrize('enrollment, error', [
    (OSError('synthetic lost enrollment response'), OSError),
    ((201, b'not-json', {}), ValueError),
    ((201, b'{}', {}), KeyError),
    ((201, b'{"connectorToken":null}', {}), HarnessError),
    ((201, b'{"connectorToken":""}', {}), HarnessError),
    ((503, b'', {}), HarnessError),
])
def test_enrollment_failures_still_delete_the_requested_probe(enrollment, error):
    request, calls = probe_request(enrollment)
    headers = {'X-Hub-Proxy-Secret': SYNTHETIC_SECRET}
    ident = 'probe-enrollment-regression'
    with pytest.raises(error):
        with ingress.probe_world(request, PORTAL, headers, ident):
            pytest.fail('Invalid enrollment must not run a probe')
    assert len(calls) == 2
    assert calls[0][1] == '/api/worlds'
    assert calls[0][3]['id'] == ident
    assert calls[1] == (PORTAL, '/api/worlds/' + ident, headers, None, 'DELETE')


@pytest.mark.parametrize('cleanup', [(503, b'', {}), OSError('synthetic cleanup transport failure')])
def test_cleanup_failure_preserves_original_enrollment_failure(cleanup, capsys):
    original = OSError('synthetic original enrollment failure')
    request, calls = probe_request(original, cleanup)
    with pytest.raises(OSError) as caught:
        with ingress.probe_world(request, PORTAL, {}, 'probe-cleanup-regression'):
            pytest.fail('A lost response must not run a probe')
    assert caught.value is original
    assert calls[-1][-1] == 'DELETE'
    diagnostic = capsys.readouterr().err
    assert diagnostic == 'Synthetic probe cleanup failed for probe-cleanup-regression\n'
    assert SYNTHETIC_SECRET not in diagnostic
    assert 'transport failure' not in diagnostic


def test_cleanup_failure_preserves_original_probe_failure(capsys):
    request, calls = probe_request((201, b'{"connectorToken":"synthetic-token"}', {}), (500, b'', {}))
    original = RuntimeError('synthetic probe failure')
    with pytest.raises(RuntimeError) as caught:
        with ingress.probe_world(request, PORTAL, {}, 'probe-body-regression') as token:
            assert token == 'synthetic-token'
            raise original
    assert caught.value is original
    assert calls[-1][-1] == 'DELETE'
    assert 'Synthetic probe cleanup failed' in capsys.readouterr().err


@pytest.mark.parametrize('cleanup', [(500, b'', {}), OSError('synthetic cleanup failure')])
def test_cleanup_failure_cannot_report_success(cleanup):
    request, calls = probe_request((201, b'{"connectorToken":"synthetic-token"}', {}), cleanup)
    with pytest.raises(HarnessError, match='^Synthetic probe cleanup failed$'):
        with ingress.probe_world(request, PORTAL, {}, 'probe-cleanup-regression'):
            pass
    assert calls[-1][-1] == 'DELETE'


def test_successful_probe_is_deleted():
    request, calls = probe_request((201, b'{"connectorToken":"synthetic-token"}', {}))
    with ingress.probe_world(request, PORTAL, {}, 'probe-success-regression') as token:
        assert token == 'synthetic-token'
        assert len(calls) == 1
    assert calls[-1][1] == '/api/worlds/probe-success-regression'
    assert calls[-1][-1] == 'DELETE'


class Route:
    """No browser or outbound requests: record every route action."""
    def __init__(self, url, status=200, resource_type='document', location=None):
        self.request = SimpleNamespace(url=url, resource_type=resource_type)
        self.response = SimpleNamespace(status=status, headers={'location': location})
        self.fetch = Mock(return_value=self.response)
        self.abort = Mock()
        self.fulfill = Mock()


@pytest.mark.parametrize('path, resource_type', [
    ('/agent/helper/conversation/synthetic-thread', 'document'),
    ('/static/app.js', 'script'),
    ('/static/app.css', 'stylesheet'),
    ('/static/icon.png', 'image'),
    ('/api/state?fresh=1', 'fetch'),
])
def test_portal_guard_allows_only_same_origin_resources(path, resource_type):
    route = Route(PORTAL + path, resource_type=resource_type)
    browser.same_origin_requests(PORTAL)(route)
    route.fetch.assert_called_once_with(max_redirects=0, max_retries=0)
    route.fulfill.assert_called_once_with(response=route.response)
    route.abort.assert_not_called()


@pytest.mark.parametrize('url', [
    'http://127.0.0.1:38002/static/app.js',
    'http://localhost:38001/static/app.js',
    'https://127.0.0.1:38001/static/app.js',
    'https://remote.example/static/app.js',
    'http://127.0.0.1.remote.example:38001/static/app.js',
    'http://user:pass@127.0.0.1:38001/static/app.js',
    'http://127.0.0.1:invalid/static/app.js',
    'http://127.0.0.1:38001\\@remote.example/static/app.js',
    'http://127.0.0.1:38001/\nstatic/app.js',
    'file:///static/app.js',
    'data:text/html,unreviewed',
    '//127.0.0.1:38001/static/app.js',
])
@pytest.mark.parametrize('resource_type', ('document', 'script', 'image', 'fetch'))
def test_portal_guard_aborts_off_origin_navigation_and_subresources(url, resource_type):
    route = Route(url, resource_type=resource_type)
    browser.same_origin_requests(PORTAL)(route)
    route.abort.assert_called_once_with()
    route.fetch.assert_not_called()
    route.fulfill.assert_not_called()


@pytest.mark.parametrize('status', REDIRECTS)
@pytest.mark.parametrize('location', (PORTAL + '/next', 'https://remote.example/sink'))
def test_portal_guard_aborts_all_redirects_without_following(status, location):
    route = Route(HANDOFF, status=status, location=location)
    browser.same_origin_requests(PORTAL)(route)
    route.fetch.assert_called_once_with(max_redirects=0, max_retries=0)
    route.abort.assert_called_once_with()
    route.fulfill.assert_not_called()


@pytest.mark.parametrize('href', [
    None,
    '/agent/helper/conversation/synthetic-thread',
    HANDOFF.replace('127.0.0.1', 'localhost'),
    HANDOFF.replace(':38001', ':38002'),
    HANDOFF.replace('http:', 'https:'),
    HANDOFF.replace('helper', 'other-agent'),
    HANDOFF + '?redirect=https://remote.example',
    HANDOFF + '#fragment',
    HANDOFF + '\n',
    HANDOFF.replace('127.0.0.1', 'user:pass@127.0.0.1'),
    'http://[invalid/agent/helper/conversation/thread',
])
def test_portal_handoff_rejects_unreviewed_urls(href):
    with pytest.raises(HarnessError, match='unreviewed world portal handoff'):
        browser.validate_portal_handoff(href, {'baseUrl': PORTAL})


def test_portal_handoff_accepts_the_exact_validated_fixture_origin():
    assert browser.validate_portal_handoff(HANDOFF, {'baseUrl': PORTAL}) == PORTAL


@pytest.mark.parametrize('origin', ('https://remote.example', PORTAL + '/path', 'http://user:pass@localhost'))
def test_portal_context_rejects_unvalidated_origins_before_creation(origin):
    fake_browser = Mock()
    with pytest.raises(HarnessError):
        with browser.fixture_context(fake_browser, origin):
            pytest.fail('An unvalidated origin must not create a context')
    fake_browser.new_context.assert_not_called()


@pytest.mark.parametrize('failure', ('route', 'goto', 'fetch'))
def test_portal_context_blocks_workers_and_closes_on_guard_or_navigation_failure(failure):
    fake_browser = Mock()
    context = fake_browser.new_context.return_value
    original = RuntimeError('synthetic ' + failure + ' failure')
    if failure == 'route':
        context.route.side_effect = original
    with pytest.raises(RuntimeError) as caught:
        with browser.fixture_context(fake_browser, PORTAL) as opened:
            assert opened is context
            fake_browser.new_context.assert_called_once_with(service_workers='block')
            pattern, guard = context.route.call_args.args
            assert pattern == '**/*'
            # The guard is installed before creating a page or navigating.
            context.new_page.assert_not_called()
            if failure == 'fetch':
                route = Route(HANDOFF)
                route.fetch.side_effect = original
                guard(route)
            else:
                page = context.new_page.return_value
                page.goto.side_effect = original
                context.new_page().goto(HANDOFF)
    assert caught.value is original
    fake_browser.new_context.assert_called_once_with(service_workers='block')
    context.close.assert_called_once_with()


@pytest.mark.parametrize('failure', ('hub-route', 'hub-goto', 'world-route', 'world-goto', 'world-close', 'hub-close', 'none'))
def test_browser_run_closes_all_created_contexts_and_browser(failure, monkeypatch, capsys):
    """Drive run() with fake pages; never launch a browser or touch an ingress."""
    fake_browser = Mock()
    hub_context, world_context = Mock(), Mock()
    page, world_page = Mock(), Mock()
    hub_context.new_page.return_value = page
    world_context.new_page.return_value = world_page
    fake_browser.new_context.side_effect = (hub_context, world_context)
    original = RuntimeError('synthetic ' + failure)
    points = {
        'hub-route': hub_context.route, 'hub-goto': page.goto,
        'world-route': world_context.route, 'world-goto': world_page.goto,
        'world-close': world_context.close, 'hub-close': hub_context.close,
    }
    if failure in points:
        points[failure].side_effect = original

    nodes = {}
    def node(selector):
        return nodes.setdefault(selector, Mock())
    page.locator.side_effect = node
    node('#login-form, #operator-token, #logout-button').count.return_value = 0
    node('#world-grid').inner_text.return_value = ''
    node('#connector-token').input_value.return_value = ''
    node('#conversation-list li').filter.return_value.get_by_role.return_value.get_attribute.return_value = HANDOFF
    world_page.locator.return_value.inner_text.return_value = 'Synthetic portal'
    theme = [None]
    def evaluate(script, *args):
        if args:
            theme[0] = None if args[0] == 'system' else args[0]
        return True
    page.evaluate.side_effect = evaluate
    node('html').get_attribute.side_effect = lambda name: theme[0]

    manager = MagicMock()
    manager.__enter__.return_value.chromium.launch.return_value = fake_browser
    playwright = ModuleType('playwright')
    sync_api = ModuleType('playwright.sync_api')
    sync_api.sync_playwright = Mock(return_value=manager)
    playwright.sync_api = sync_api
    monkeypatch.setitem(sys.modules, 'playwright', playwright)
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', sync_api)

    worlds = [{'id': ident, 'baseUrl': PORTAL, 'connected': True,
               'snapshot': {'agents': [{'id': 'helper'}], 'conversations': [], 'questions': []}}
              for ident in ('test-a', 'test-b')]
    responses = []
    for payload in ({'authenticated': True, 'authMode': 'external-preauth'}, {'items': worlds}):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers.get_content_type.return_value = 'application/json'
        response.read.return_value = json.dumps(payload)
        responses.append(response)
    opener = Mock()
    opener.open.side_effect = responses
    monkeypatch.setattr(browser.urllib.request, 'build_opener', Mock(return_value=opener))
    secret_file = SimpleNamespace(read_text=lambda **kwargs: 'S' * 43)
    output = MagicMock()
    output.__truediv__.return_value = Path('unused-synthetic-screenshot.png')

    if failure == 'none':
        browser.run(PORTAL, secret_file, output)
        assert '"result": "passed"' in capsys.readouterr().out
    else:
        with pytest.raises(RuntimeError) as caught:
            browser.run(PORTAL, secret_file, output)
        assert caught.value is original
        assert '"result": "passed"' not in capsys.readouterr().out
    hub_context.close.assert_called_once_with()
    fake_browser.close.assert_called_once_with()
    manager.__exit__.assert_called_once()
    assert fake_browser.new_context.call_args_list[0].kwargs['service_workers'] == 'block'
    if failure not in {'hub-route', 'hub-goto'}:
        assert fake_browser.new_context.call_args_list[1].kwargs == {'service_workers': 'block'}
        world_context.route.assert_called_once()
        world_context.close.assert_called_once_with()
    else:
        world_context.close.assert_not_called()


@pytest.mark.parametrize('failure', ('none', 'offline', 'drain'))
def test_fixture_teardown_blocks_network_and_drains_routes_before_disposal(failure):
    fake_browser = Mock()
    context = fake_browser.new_context.return_value
    original = RuntimeError('synthetic teardown failure')
    if failure == 'offline':
        context.set_offline.side_effect = original
    if failure == 'drain':
        context.unroute_all.side_effect = original
    if failure == 'none':
        with browser.fixture_context(fake_browser, PORTAL):
            pass
    else:
        with pytest.raises(RuntimeError) as caught:
            with browser.fixture_context(fake_browser, PORTAL):
                pass
        assert caught.value is original
    calls = [call[0] for call in context.mock_calls]
    assert calls[:2] == ['route', 'set_offline']
    context.set_offline.assert_called_once_with(True)
    if failure == 'offline':
        context.unroute_all.assert_not_called()
    else:
        context.unroute_all.assert_called_once_with(behavior='wait')
        assert calls[-2:] == ['unroute_all', 'close']
    context.close.assert_called_once_with()
