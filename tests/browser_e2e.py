"""Real browser acceptance against synthetic Hub/worlds only. Explicit opt-in.
Uses a disposable browser context and a test-only ingress secret file.
Never use this suite against live worlds or a production proxy secret.
"""
import argparse
from contextlib import ExitStack, contextmanager
import json
import re
from pathlib import Path
import urllib.parse
import urllib.request
import uuid
from e2e import HarnessError, require, require_synthetic_worlds, validate_base_url

class NoRedirect(urllib.request.HTTPRedirectHandler):

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def base_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.hostname not in {'localhost', '127.0.0.1', '::1'} or (parsed.username is not None) or (parsed.password is not None) or (parsed.path not in {'', '/'}) or parsed.query or parsed.fragment or (port == 0) or ('\\' in value) or any((ord(char) <= 32 or ord(char) == 127 for char in value)):
            raise ValueError()
    except ValueError:
        raise argparse.ArgumentTypeError('Use an explicit loopback http:// or https:// ingress origin without credentials, a path, query, or fragment.') from None
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))

def origin_key(url):
    try:
        if not isinstance(url, str) or '\\' in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
            return None
        target = urllib.parse.urlsplit(url)
        if target.scheme not in {'http', 'https'} or not target.hostname or target.username is not None or target.password is not None or target.port == 0:
            return None
        return (target.scheme, target.hostname, target.port or (443 if target.scheme == 'https' else 80))
    except (ValueError, UnicodeError):
        return None


def same_origin_requests(origin):
    allowed = origin_key(validate_base_url(origin))

    def scoped_request(route):
        # Covers navigation AND subresources. Never let the browser or fetch
        # follow even a same-origin redirect before checking its destination.
        if origin_key(route.request.url) != allowed:
            route.abort()
            return
        response = route.fetch(max_redirects=0, max_retries=0)
        if 300 <= response.status < 400:
            route.abort()
        else:
            route.fulfill(response=response)
    return scoped_request


@contextmanager
def fixture_context(browser, origin, **options):
    handler = same_origin_requests(origin)  # Validate before creating a context.
    context = browser.new_context(service_workers='block', **options)
    try:
        context.route('**/*', handler)
        yield context
    finally:
        try:
            # Block new network work before removing the origin guard. Drain
            # active route.fetch/fulfill handlers before disposing their responses.
            context.set_offline(True)
            context.unroute_all(behavior='wait')
        finally:
            context.close()


def validate_portal_handoff(href, fixture):
    origin = validate_base_url(fixture['baseUrl'])
    require(isinstance(href, str) and '\\' not in href
            and not any(ord(char) <= 32 or ord(char) == 127 for char in href),
            'Refusing an unreviewed world portal handoff')
    try:
        target = urllib.parse.urlsplit(href)
    except ValueError:
        raise HarnessError('Refusing an unreviewed world portal handoff') from None
    expected = urllib.parse.urlsplit(origin)
    require((target.scheme, target.netloc) == (expected.scheme, expected.netloc)
            and re.fullmatch('/agent/helper/conversation/[A-Za-z0-9_.-]+', target.path)
            and not target.query and not target.fragment,
            'Refusing an unreviewed world portal handoff')
    return origin


def run(base, proxy_secret_file, output):
    validate_base_url(base)
    from playwright.sync_api import sync_playwright

    proxy_secret = proxy_secret_file.read_text(encoding='utf-8-sig').strip()
    if not re.fullmatch('[A-Za-z0-9_-]{43,128}', proxy_secret):
        raise ValueError('The test proxy secret file must contain one non-empty header value.')
    headers = {'X-Hub-Proxy-Secret': proxy_secret}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def read_api(path):
        req = urllib.request.Request(base + path, headers=headers)
        with opener.open(req, timeout=10) as res:
            require(res.status == 200, 'Disposable ingress preflight failed')
            require(res.headers.get_content_type() == 'application/json', 'Preflight did not return JSON; check test ingress access')
            return json.load(res)
    me = read_api('/api/me')
    require(me.get('authenticated') is True and me.get('authMode') == 'external-preauth', 'External preauth must be enabled on the disposable ingress')
    worlds = read_api('/api/worlds')['items']
    require_synthetic_worlds(worlds)
    require(len(worlds) == 2 and {w['id'] for w in worlds} == {'test-a', 'test-b'}, 'Refusing non-disposable world registry')
    require(all((w['connected'] for w in worlds)), 'Both disposable worlds must be connected')
    output.mkdir(parents=True, exist_ok=True)
    checks = 0

    with sync_playwright() as p, ExitStack() as resources:
        browser = p.chromium.launch(channel='msedge', headless=True)
        resources.callback(browser.close)
        context = resources.enter_context(fixture_context(browser, base, viewport={'width': 1440, 'height': 1000}, extra_http_headers={'X-Hub-Proxy-Secret': proxy_secret}))
        page = context.new_page()
        errors = []
        mutations = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: mutations.append(request.url) if request.method not in {'GET', 'HEAD', 'OPTIONS'} else None)
        page.goto(base)
        page.locator('#dashboard').wait_for(state='visible')
        page.locator('#world-grid [data-world-id="test-a"][data-connected="true"]').wait_for()
        page.locator('#world-grid [data-world-id="test-b"][data-connected="true"]').wait_for()
        require(page.locator('#access-panel').is_hidden(), 'Acceptance check failed')
        require(page.locator('#login-form, #operator-token, #logout-button').count() == 0, 'Acceptance check failed')
        require(not mutations, 'Page load must never send a mutation')
        checks += 6
        for theme in ['light', 'dark', 'system']:
            page.evaluate('(m)=>window.__hub.theme.setMode(m)', theme)
            if theme != 'system':
                require(page.locator('html').get_attribute('data-theme') == theme, 'Acceptance check failed')
            else:
                require(page.locator('html').get_attribute('data-theme') is None, 'Acceptance check failed')
            page.screenshot(path=str(output / (theme + '.png')), full_page=True)
            checks += 1
        title = 'Browser E2E ' + uuid.uuid4().hex[:8]
        page.locator('#new-conversation-button').click()
        page.locator('#conversation-world').select_option('test-a')
        page.locator('#conversation-agent').select_option('helper')
        page.locator('#conversation-title').fill(title)
        page.locator('#conversation-message').fill('Browser fixture: please ask me a question')
        page.locator('#conversation-submit').click()
        page.locator('#questions-tab').click()
        card = page.locator('#question-list [data-request-id]').filter(has_text=title)
        card.wait_for(timeout=45000)
        card.locator('textarea').fill('Browser Aurora')
        card.locator('[data-action="answer-question"]').click()
        card.wait_for(state='detached', timeout=45000)
        checks += 3
        page.locator('#conversations-tab').click()
        row = page.locator('#conversation-list li').filter(has_text=title)
        row.wait_for(timeout=15000)
        link = row.get_by_role('link', name='Open')
        href = link.get_attribute('href')
        require('/agent/helper/conversation/' in href, 'Acceptance check failed')
        fixture = next((world for world in worlds if world['id'] == 'test-a'))
        portal_origin = validate_portal_handoff(href, fixture)
        with fixture_context(browser, portal_origin) as world_context:
            worldpage = world_context.new_page()
            worldpage.goto(href)
            worldpage.wait_for_load_state('networkidle', timeout=30000)
            require(worldpage.locator('body').inner_text().strip(), 'Acceptance check failed')
        checks += 2
        for viewport in [{'width': 390, 'height': 844}, {'width': 1440, 'height': 1000}]:
            page.set_viewport_size(viewport)
            require(page.evaluate('document.documentElement.scrollWidth <= window.innerWidth+1'), 'Acceptance check failed')
            checks += 1
        require(not errors, errors)
        require(page.evaluate('Object.keys(localStorage).every(k=>k==="botnexushub.theme")'), 'Acceptance check failed')
        checks += 2

        def deny_world_reads(route):
            require(route.request.method == 'GET', 'Acceptance check failed')
            route.fulfill(status=403, content_type='text/html', body='<html>External access required</html>')
        page.route('**/api/worlds', deny_world_reads)
        before_denial = len(mutations)
        page.locator('#refresh-button').click()
        page.locator('#access-panel').wait_for(state='visible')
        require(page.locator('#dashboard').is_hidden(), 'Acceptance check failed')
        require(page.locator('#world-grid').inner_text() == '', 'Acceptance check failed')
        require(page.locator('#connector-token').input_value() == '', 'Acceptance check failed')
        require(len(mutations) == before_denial, 'Access recovery must not resend a mutation')
        page.screenshot(path=str(output / 'external-access.png'), full_page=True)
        page.unroute('**/api/worlds', deny_world_reads)
        page.get_by_role('button', name='Reload secure access').click()
        page.locator('#dashboard').wait_for(state='visible')
        page.locator('#world-grid [data-world-id="test-a"]').wait_for()
        require(len(mutations) == before_denial, 'Secure reload must not resend a mutation')
        require(not errors, errors)
        checks += 8
    print(json.dumps({'browserChecks': checks, 'result': 'passed', 'title': title, 'handoff': href, 'screenshots': str(output)}, indent=2))
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-disposable', action='store_true', required=True)
    parser.add_argument('--base-url', type=base_url, required=True)
    parser.add_argument('--proxy-secret-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.base_url, args.proxy_secret_file, args.output)
