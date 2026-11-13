"""Mutating smoke for an explicitly approved, loopback-only synthetic Hub.

Pass --allow-disposable, --base-url (the operator ingress, not the private Hub),
and --proxy-secret-file. The origin must exactly match HUB_PUBLIC_URL. Only
worlds test-a/test-b and agent helper are allowed; synthetic threads are retained.
"""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit
import uuid


WORLD_IDS = frozenset({'test-a', 'test-b'})
PROXY_SECRET = re.compile(r'[A-Za-z0-9_-]{43,128}\Z')


class HarnessError(RuntimeError):
    """A fixed diagnostic that never contains request headers or response bodies."""


class RequestError(HarnessError):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def require(condition, message):
    # Safety gates must still run under python -O.
    if not condition:
        raise HarnessError(message)


def validate_base_url(value):
    """Keep the supplied origin unchanged, and allow no remote hosts or paths."""
    try:
        require(isinstance(value, str) and value.startswith(('http://', 'https://')),
                'Use an explicit HTTP(S) loopback origin for --base-url')
        require(not any(ord(char) <= 32 or ord(char) >= 127 for char in value)
                and not any(char in value for char in '\\%?#'), 'Invalid loopback origin')
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
        require(host and parts.username is None and parts.password is None
                and not parts.path and not parts.query and not parts.fragment
                and not parts.netloc.endswith(':'),
                'Use an origin without credentials, a path, or a trailing slash')
        require(port is None or 1 <= port <= 65535, 'Invalid loopback origin port')
        local = host == 'localhost' or ipaddress.ip_address(host).is_loopback
        require(local, 'Mutable tests require a loopback origin')
    except (ValueError, UnicodeError):
        raise HarnessError('Mutable tests require localhost or a loopback IP origin') from None
    return value


def load_proxy_secret(path):
    try:
        secret = Path(path).read_text(encoding='utf-8').rstrip('\r\n')
    except (OSError, UnicodeError):
        raise HarnessError('Cannot read --proxy-secret-file') from None
    require(PROXY_SECRET.fullmatch(secret) is not None,
            'Proxy secret must contain 43-128 URL-safe characters, optionally followed by a newline')
    return secret


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise forwards custom headers, including the proxy secret.
        return None


def fixture_opener():
    # Ignore ambient HTTP proxies and do not install cookies or authentication.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def read_json(opener, request):
    try:
        with opener.open(request, timeout=10) as response:
            require(200 <= response.status < 300, 'Fixture request returned an unexpected status')
            return response.status, json.loads(response.read() or b'null')
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise RequestError('Fixture redirect refused; use the exact ingress or portal origin') from None
        raise RequestError('Fixture request failed (HTTP ' + str(error.code) + ')',
                           retryable=error.code in {408, 429, 500, 502, 503, 504}) from None
    except (urllib.error.URLError, OSError):
        raise RequestError('Fixture request could not complete', retryable=True) from None
    except (ValueError, UnicodeError):
        raise RequestError('Fixture response was not valid JSON') from None


def require_synthetic_worlds(items, *, connected=True):
    require(isinstance(items, list) and len(items) == 2
            and all(isinstance(world, dict) for world in items)
            and {world.get('id') for world in items} == WORLD_IDS,
            'Refusing a registry other than exactly test-a/test-b')
    for world in items:
        snapshot = world.get('snapshot')
        require(isinstance(snapshot, dict), 'Both synthetic worlds must have a snapshot')
        agents = snapshot.get('agents')
        require(isinstance(agents, list) and len(agents) == 1
                and isinstance(agents[0], dict) and agents[0].get('id') == 'helper',
                'Refusing a synthetic world with agents other than exactly helper')
        for field in ('conversations', 'questions'):
            rows = snapshot.get(field, [])
            require(isinstance(rows, list) and all(isinstance(row, dict)
                    and row.get('agentId') == 'helper' for row in rows),
                    'Refusing snapshot data outside agent helper')
        if connected:
            require(world.get('connected') is True, 'Both synthetic worlds must be connected')
    return items


def until(getter, predicate, name, seconds=45):
    deadline = time.monotonic() + seconds
    while True:
        try:
            value = getter()
            if predicate(value):
                return value
        except RequestError as error:
            if not error.retryable:
                raise
        remaining = deadline - time.monotonic()
        require(remaining > 0, 'Timed out: ' + name)
        time.sleep(min(.5, remaining))


class HubClient:
    def __init__(self, base_url, proxy_secret_file):
        self.base_url = validate_base_url(base_url)
        self._secret = load_proxy_secret(proxy_secret_file)
        self._opener = fixture_opener()

    def request(self, path, body=None, key=None):
        require(path.startswith('/') and not path.startswith('//')
                and not any(ord(char) <= 32 for char in path) and '#' not in path,
                'Only relative Hub API routes are allowed')
        headers = {'X-Hub-Proxy-Secret': self._secret, 'Origin': self.base_url,
                   'X-Hub-Request': '1', 'Content-Type': 'application/json',
                   'Accept': 'application/json'}
        if key:
            headers['Idempotency-Key'] = key
        request = urllib.request.Request(self.base_url + path, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
        return read_json(self._opener, request)

    def worlds(self, *, connected=True):
        return require_synthetic_worlds(self.request('/api/worlds')[1]['items'], connected=connected)

    def command(self, world, action, payload, key=None, seconds=45):
        require(world in WORLD_IDS and payload.get('agentId') == 'helper'
                and action in {'start_conversation', 'send_message', 'answer_question'},
                'Only synthetic helper commands are allowed')
        self.worlds()  # Recheck the complete registry before each mutation.
        status, command = self.request('/api/worlds/' + world + '/commands',
                                       {'action': action, 'payload': payload}, key or str(uuid.uuid4()))
        require(status == 202, 'Command was not accepted')
        route = '/api/commands/' + quote(command['id'], safe='')
        result = until(lambda: self.request(route)[1],
                       lambda item: item['status'] in {'succeeded', 'failed', 'uncertain'}, action, seconds)
        require(result['status'] == 'succeeded', 'Synthetic command did not succeed: ' + action)
        return result


def run(base_url, proxy_secret_file, *, allow_disposable=False):
    require(allow_disposable, 'Explicit --allow-disposable approval is required')
    client = HubClient(base_url, proxy_secret_file)
    health = client.request('/health')[1]
    require(health['status'] == 'ok', 'Hub health failed')
    version = Path(__file__).resolve().parents[1].joinpath('VERSION').read_text(encoding='utf-8').strip()
    require(health['version'] == version, 'Hub version does not match this checkout')
    client.worlds()
    checks = 4
    conversations = {}
    for world in ('test-a', 'test-b'):
        key = str(uuid.uuid4())
        payload = {'agentId': 'helper', 'title': 'Hub E2E ' + world,
                   'message': 'Hello from the isolated Hub E2E fixture'}
        command = client.command(world, 'start_conversation', payload, key)
        conversation = command['result']['conversationId']
        conversations[world] = conversation
        client.worlds()
        status, replay = client.request('/api/worlds/' + world + '/commands',
                                        {'action': 'start_conversation', 'payload': payload}, key)
        require(status == 202 and replay['id'] == command['id'], 'Idempotent replay changed the command')

        def current():
            return next(item for item in client.worlds() if item['id'] == world)['snapshot']

        snapshot = until(current, lambda item: any(row['id'] == conversation for row in item['conversations']),
                         'new conversation snapshot')
        require(sum(row['id'] == conversation for row in snapshot['conversations']) == 1,
                'Conversation was duplicated')
        client.command(world, 'send_message', {'agentId': 'helper', 'conversationId': conversation,
                                              'message': 'Please ask me a question'})
        snapshot = until(current, lambda item: any(row['conversationId'] == conversation for row in item['questions']),
                         'durable ask_user snapshot')
        question = next(row for row in snapshot['questions'] if row['conversationId'] == conversation)
        require(question['requestId'] and question['prompt'], 'Durable question is incomplete')
        client.command(world, 'answer_question', {'agentId': 'helper', 'conversationId': conversation,
                                                 'requestId': question['requestId'], 'text': 'Aurora',
                                                 'selectedValues': [], 'cancelled': False})
        until(current, lambda item: not any(row['conversationId'] == conversation for row in item['questions']),
              'prompt cleared')
        checks += 6
    require(conversations['test-a'] != conversations['test-b'], 'World conversations were not isolated')
    activity = client.request('/api/activity?limit=100')[1]['items']
    require({item['worldId'] for item in activity} >= WORLD_IDS, 'Synthetic activity is missing')
    checks += 2
    print(json.dumps({'checks': checks, 'result': 'passed', 'worlds': list(conversations),
                      'conversations': conversations}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-disposable', action='store_true', required=True)
    parser.add_argument('--base-url', required=True, help='Exact loopback operator ingress origin, without a trailing slash')
    parser.add_argument('--proxy-secret-file', type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.base_url, args.proxy_secret_file, allow_disposable=args.allow_disposable)
    except HarnessError as error:
        parser.exit(1, str(error) + '\n')
    except (OSError, KeyError, TypeError, ValueError):
        parser.exit(1, 'Unexpected fixture response or unreadable local version file\n')
