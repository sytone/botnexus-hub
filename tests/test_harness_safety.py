"""Safety gates and cleanup must run with python -O, not vanish with assert."""
import ast
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
TESTS=ROOT/'tests'


def test_mutating_harnesses_do_not_use_optimized_away_assertions():
    for filename in ('e2e.py','recovery_e2e.py','browser_e2e.py','ingress_e2e.py'):
        tree=ast.parse((TESTS/filename).read_text(encoding='utf-8'))
        assert not any(isinstance(node,ast.Assert) for node in ast.walk(tree)),filename


@pytest.mark.parametrize('bad_url',['https://remote.example','http://127.0.0.1:80/path','http://user:pass@localhost','http://127.0.0.1?query=1'])
def test_optimized_mode_rejects_nonfixture_origins(bad_url):
    script='import sys; sys.path.insert(0,sys.argv[1]); from e2e import validate_base_url; validate_base_url(sys.argv[2])'
    result=subprocess.run([sys.executable,'-O','-c',script,str(TESTS),bad_url],capture_output=True,text=True)
    assert result.returncode!=0
    assert 'HarnessError' in result.stderr


def test_optimized_mode_rejects_real_world_registry_and_nonsynthetic_agent():
    script='import sys; sys.path.insert(0,sys.argv[1]); from e2e import require_synthetic_worlds; import json; require_synthetic_worlds(json.loads(sys.argv[2]))'
    examples=['[{"id":"real-world","connected":true}]','[{"id":"test-a","snapshot":{"agents":[{"id":"other"}]}},{"id":"test-b"}]']
    for data in examples:
        result=subprocess.run([sys.executable,'-O','-c',script,str(TESTS),data],capture_output=True,text=True)
        assert result.returncode!=0
        assert 'HarnessError' in result.stderr


def test_ingress_cleanup_is_unconditional_and_portal_handoff_is_loopback_scoped():
    ingress=(TESTS/'ingress_e2e.py').read_text()
    browser=(TESTS/'browser_e2e.py').read_text()
    assert "cleanup = req(" in ingress
    assert "require(cleanup == 204" in ingress
    assert 'require_synthetic_worlds(worlds)' in ingress and 'require_synthetic_worlds(worlds)' in browser
    assert "validate_base_url(fixture['baseUrl'])" in browser
    assert 'Refusing an unreviewed world portal handoff' in browser


def test_optimized_mode_keeps_transport_scope_and_cleanup_guards():
    script = r'''
import sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from e2e import HarnessError
from browser_e2e import same_origin_requests, validate_portal_handoff
from ingress_e2e import probe_world, websocket_connect

origin = 'http://127.0.0.1:38001'
for guard in (
    lambda: same_origin_requests('https://remote.example'),
    lambda: websocket_connect('https://remote.example', '/start'),
    lambda: validate_portal_handoff('https://remote.example/agent/helper/conversation/thread', {'baseUrl': origin}),
):
    try:
        guard()
    except HarnessError:
        pass
    else:
        raise RuntimeError('An optimized scope gate was bypassed')

actions = []
def fetch(**options):
    actions.append(('fetch', options))
    return SimpleNamespace(status=302)
route = SimpleNamespace(
    request=SimpleNamespace(url=origin + '/static/app.js'),
    fetch=fetch,
    abort=lambda: actions.append(('abort',)),
    fulfill=lambda **options: actions.append(('fulfill',)),
)
guard = same_origin_requests(origin)
guard(route)
if actions != [('fetch', {'max_redirects': 0, 'max_retries': 0}), ('abort',)]:
    raise RuntimeError('An optimized redirect gate was bypassed')
actions.clear()
route.request.url = 'https://remote.example/static/app.js'
guard(route)
if actions != [('abort',)]:
    raise RuntimeError('An optimized subresource gate was bypassed')

calls = []
original = OSError('synthetic lost enrollment response')
def request(base, path, headers=None, body=None, method=None):
    calls.append((path, method))
    if method == 'DELETE':
        raise OSError('synthetic cleanup detail must not be printed')
    raise original
try:
    with probe_world(request, origin, {}, 'probe-optimized'):
        raise RuntimeError('A failed enrollment yielded')
except OSError as error:
    if error is not original:
        raise RuntimeError('Cleanup replaced the enrollment failure')
else:
    raise RuntimeError('An optimized cleanup gate was bypassed')
if calls != [('/api/worlds', None), ('/api/worlds/probe-optimized', 'DELETE')]:
    raise RuntimeError('Optimized enrollment cleanup was skipped')
'''
    result = subprocess.run([sys.executable, '-B', '-O', '-c', script, str(TESTS)],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stderr == 'Synthetic probe cleanup failed for probe-optimized\n'
