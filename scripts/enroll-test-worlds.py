"""Enroll only synthetic loopback worlds; save connector credentials in .env only.

Required: --env-file, --base-url (exact operator ingress origin), and
--proxy-secret-file. Existing credentials are never replaced or printed.
The env file's parent directory must be private, especially on Windows where
POSIX file modes do not set a Windows ACL. Enrollment produces no success output.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlsplit


SECRET = re.compile(r'[A-Za-z0-9_-]{43,128}\Z')
TOKEN_KEYS = {'test-a': 'TEST_WORLD_A_TOKEN', 'test-b': 'TEST_WORLD_B_TOKEN'}
ASSIGNMENT = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$')


class EnrollmentError(RuntimeError):
    """Diagnostics must never contain a token, request header or response body."""


def require(condition, message):
    if not condition:
        raise EnrollmentError(message)


def validate_base_url(value):
    try:
        require(isinstance(value, str) and value.startswith(('http://', 'https://'))
                and not any(ord(char) <= 32 or ord(char) >= 127 for char in value)
                and not any(char in value for char in '\\%?#'), 'Use an explicit HTTP(S) loopback origin')
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
        require(host and parts.username is None and parts.password is None
                and not parts.path and not parts.query and not parts.fragment
                and not parts.netloc.endswith(':'),
                'Use an origin without credentials, a path, or a trailing slash')
        require(port is None or 1 <= port <= 65535, 'Invalid loopback origin port')
        require(host == 'localhost' or ipaddress.ip_address(host).is_loopback,
                'Synthetic enrollment requires loopback origins')
    except (ValueError, UnicodeError):
        raise EnrollmentError('Synthetic enrollment requires localhost or a loopback IP origin') from None
    return value


def load_proxy_secret(path):
    try:
        secret = Path(path).read_text(encoding='utf-8').rstrip('\r\n')
    except (OSError, UnicodeError):
        raise EnrollmentError('Cannot read --proxy-secret-file') from None
    require(SECRET.fullmatch(secret) is not None, 'Proxy secret must contain 43-128 URL-safe characters')
    return secret


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the proxy secret to another origin.


class HubClient:
    def __init__(self, base_url, proxy_secret_file):
        self.base_url = validate_base_url(base_url)
        self._secret = load_proxy_secret(proxy_secret_file)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, route, payload=None):
        require(route == '/api/worlds', 'Only fixture enrollment routes are allowed')
        request = urllib.request.Request(self.base_url + route,
                                         headers={'X-Hub-Proxy-Secret': self._secret,
                                                  'Origin': self.base_url, 'X-Hub-Request': '1',
                                                  'Content-Type': 'application/json', 'Accept': 'application/json'},
                                         data=json.dumps(payload).encode() if payload is not None else None)
        try:
            with self._opener.open(request, timeout=10) as response:
                require(response.status == (201 if payload is not None else 200),
                        'Unexpected enrollment response status')
                return json.load(response)
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                raise EnrollmentError('Enrollment redirect refused; use the exact ingress origin') from None
            raise EnrollmentError('Enrollment request failed (HTTP ' + str(error.code) + ')') from None
        except (urllib.error.URLError, OSError):
            raise EnrollmentError('Enrollment request could not complete') from None
        except (ValueError, UnicodeError):
            raise EnrollmentError('Enrollment response was not valid JSON') from None


def synthetic_registry(response):
    require(isinstance(response, dict) and isinstance(response.get('items'), list),
            'Invalid synthetic world registry')
    items = response['items']
    require(all(isinstance(world, dict) and world.get('id') in TOKEN_KEYS for world in items)
            and len({world['id'] for world in items}) == len(items),
            'Refusing a registry containing worlds other than test-a/test-b')
    for world in items:
        snapshot = world.get('snapshot')
        if snapshot is None:  # Enrolled fixtures may not have connected yet.
            continue
        require(isinstance(snapshot, dict), 'Invalid synthetic world snapshot')
        agents = snapshot.get('agents')
        require(isinstance(agents, list) and len(agents) == 1
                and isinstance(agents[0], dict) and agents[0].get('id') == 'helper',
                'Refusing a synthetic world with agents other than exactly helper')
        for field in ('conversations', 'questions'):
            rows = snapshot.get(field, [])
            require(isinstance(rows, list) and all(isinstance(row, dict)
                    and row.get('agentId') == 'helper' for row in rows),
                    'Refusing snapshot data outside agent helper')
    return {world['id'] for world in items}


def credential_value(value):
    value = value.strip()
    if not value or value.startswith('#'):
        return ''
    match = re.fullmatch(r'''(?:"([A-Za-z0-9_-]*)"|'([A-Za-z0-9_-]*)'|([A-Za-z0-9_-]*))(?:[ \t]+#.*)?''', value)
    require(match is not None, 'Invalid saved fixture credential; preserve the env file')
    token = next(part for part in match.groups() if part is not None)
    require(not token or SECRET.fullmatch(token) is not None,
            'Invalid saved fixture credential; preserve the env file')
    return token


class EnvFile:
    def __init__(self, path):
        self.path = Path(os.path.abspath(path))
        require(self.path.parent.is_dir(), 'The env file parent directory must already exist')
        self.raw = self.read_existing()
        self.bom = self.raw is not None and self.raw.startswith(b'\xef\xbb\xbf')
        try:
            self.text = self.raw.decode('utf-8-sig') if self.raw is not None else ''
        except UnicodeError:
            raise EnrollmentError('The env file must use UTF-8') from None
        self.values = {}
        for line in self.text.splitlines():
            match = ASSIGNMENT.fullmatch(line)
            if match and match[1] in TOKEN_KEYS.values():
                key = match[1]
                require(key not in self.values, 'Duplicate fixture credential keys; preserve the env file')
                self.values[key] = credential_value(match[2])

    def read_existing(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return None
        require(stat.S_ISREG(info.st_mode) and not self.path.is_symlink()
                and not getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0),
                'The env file must be a regular file, not a link')
        return self.path.read_bytes()

    def check_plan(self, existing):
        for ident, key in TOKEN_KEYS.items():
            if ident in existing:
                require(self.values.get(key),
                        'A registered fixture has no saved credential; preserve state and use explicit token recovery')
            else:
                require(not self.values.get(key),
                        'A saved credential has no matching registered fixture; refusing to replace it')

    def save(self, key, token):
        require(key in TOKEN_KEYS.values() and not self.values.get(key),
                'Refusing to overwrite an existing fixture credential')
        require(isinstance(token, str) and SECRET.fullmatch(token) is not None,
                'Invalid one-time connector credential')
        require(self.read_existing() == self.raw, 'The env file changed during enrollment; preserve state')
        newline = '\r\n' if '\r\n' in self.text else '\n'
        lines = self.text.splitlines(keepends=True)
        replaced = False
        for index, line in enumerate(lines):
            match = ASSIGNMENT.fullmatch(line.rstrip('\r\n'))
            if match and match[1] == key:
                ending = '\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''
                lines[index] = key + '=' + token + ending
                replaced = True
        updated = ''.join(lines)
        if not replaced:
            if updated and not updated.endswith(('\n', '\r')):
                updated += newline
            updated += key + '=' + token + newline
        raw = (b'\xef\xbb\xbf' if self.bom else b'') + updated.encode('utf-8')
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix='.' + self.path.name + '.enroll-', dir=self.path.parent)
            with os.fdopen(descriptor, 'wb') as stream:
                os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            require(self.read_existing() == self.raw, 'The env file changed during enrollment; preserve state')
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise EnrollmentError('Cannot save enrollment; preserve Hub state and use explicit credential recovery') from None
        finally:
            if temporary is not None:
                os.unlink(temporary)
        self.raw, self.text = raw, updated
        self.values[key] = token


def enroll(env_file, base_url, proxy_secret_file, *, world_a_base_url='http://localhost:8786',
           world_b_base_url='http://localhost:8787'):
    validate_base_url(world_a_base_url)
    validate_base_url(world_b_base_url)
    client = HubClient(base_url, proxy_secret_file)
    require(len({base_url, world_a_base_url, world_b_base_url}) == 3,
            'Hub ingress and the two fixture portals must have different origins')
    env = EnvFile(env_file)
    # Hold a task-owned exclusive lock for the entire enrollment. Never remove
    # another process's lock or silently recover from an incomplete enrollment.
    lock_path = env.path.with_name(env.path.name + '.enroll.lock')
    try:
        lock = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        raise EnrollmentError('Cannot lock the env file; check permissions or another enrollment') from None
    try:
        env.check_plan(synthetic_registry(client.call('/api/worlds')))
        for ident, name, portal in (('test-a', 'Workshop Alpha', world_a_base_url),
                                    ('test-b', 'Workshop Beta', world_b_base_url)):
            existing = synthetic_registry(client.call('/api/worlds'))
            env.check_plan(existing)
            if ident in existing:
                continue
            require(env.read_existing() == env.raw, 'The env file changed during enrollment; preserve state')
            result = client.call('/api/worlds', {'id': ident, 'name': name, 'baseUrl': portal})
            require(isinstance(result, dict) and result.get('id') == ident,
                    'Unexpected fixture enrollment result; preserve Hub state')
            env.save(TOKEN_KEYS[ident], result.get('connectorToken'))
    finally:
        os.close(lock)
        lock_path.unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', required=True, type=Path, help='Private env file; only world credentials are updated')
    parser.add_argument('--base-url', required=True, help='Exact loopback operator ingress origin, without a trailing slash')
    parser.add_argument('--proxy-secret-file', required=True, type=Path)
    parser.add_argument('--world-a-base-url', default='http://localhost:8786', help='Loopback synthetic portal origin')
    parser.add_argument('--world-b-base-url', default='http://localhost:8787', help='Loopback synthetic portal origin')
    args = parser.parse_args()
    try:
        enroll(args.env_file, args.base_url, args.proxy_secret_file,
               world_a_base_url=args.world_a_base_url, world_b_base_url=args.world_b_base_url)
    except EnrollmentError as error:
        parser.exit(1, str(error) + '\n')
    except (OSError, KeyError, TypeError, ValueError):
        parser.exit(1, 'Enrollment could not complete; preserve the env file and Hub state\n')
