'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const staticDir = path.resolve(__dirname, '../hub/static');
const source = (name) => fs.readFileSync(path.join(staticDir, name), 'utf8');

function helpers() {
  const sandbox = {
    document: { getElementById() { return null; } },
    URL,
    URLSearchParams,
    crypto: { randomUUID: () => 'unique-test-command-key' },
    console,
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source('app.js'), sandbox, { filename: 'app.js' });
  assert.ok(sandbox.__hub?.helpers, 'Tests must exercise helpers used by the real UI.');
  return sandbox.__hub.helpers;
}

const verifiedSession = { authenticated: true, authMode: 'external-preauth', connectorUrl: 'https://hub-connect.example.test' };
const syntheticWorld = {
  id: 'test-a', name: 'Private world', baseUrl: 'https://world.example.test', connected: true,
  snapshot: { agents: [{ id: 'helper', name: 'Helper' }], conversations: [], questions: [] },
};

function reply(data, { status = 200, contentType = 'application/json', redirected = false, type = 'basic' } = {}) {
  return {
    status, ok: status >= 200 && status < 300, redirected, type,
    headers: { get(name) { return name.toLowerCase() === 'content-type' ? contentType : null; } },
    async text() { return typeof data === 'string' ? data : JSON.stringify(data); },
  };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

// A small DOM double runs the real app and its event handlers, without a server or credentials.
async function appHarness({ me = verifiedSession, worlds = [], respond } = {}) {
  const nodes = new Map();
  let focused = null;
  class Node {
    constructor(tag = 'div') {
      this.tagName = tag.toUpperCase();
      this.children = [];
      this.attributes = new Map();
      this.listeners = new Map();
      this.value = '';
      this.className = '';
      this.hidden = false;
      this.disabled = false;
      this.open = false;
      this._text = '';
    }
    set textContent(value) { this.replaceChildren(); this._text = String(value); }
    get textContent() { return this._text + this.children.map((node) => node.textContent).join(''); }
    append(...children) {
      for (const child of children) { child.remove(); child.parent = this; this.children.push(child); }
    }
    replaceChildren(...children) {
      for (const child of this.children) child.parent = null;
      this.children = [];
      this._text = '';
      this.append(...children);
      if (this.tagName === 'SELECT') this.value = this.children[0]?.value || '';
    }
    remove() {
      if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
      this.parent = null;
    }
    insertBefore(child, before) {
      child.remove();
      child.parent = this;
      const index = this.children.indexOf(before);
      this.children.splice(index < 0 ? this.children.length : index, 0, child);
    }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    removeAttribute(name) { this.attributes.delete(name); }
    querySelector(selector) {
      for (const child of this.children) {
        if (selector === '.empty-state' && child.className.split(/\s+/).includes('empty-state')) return child;
        const found = child.querySelector(selector);
        if (found) return found;
      }
      return null;
    }
    addEventListener(type, callback) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push(callback);
    }
    async fire(type, details = {}) {
      const event = { preventDefault() {}, ...details };
      await Promise.all((this.listeners.get(type) || []).map((callback) => callback(event)));
      await settle();
    }
    focus() { focused = this; }
    showModal() { this.open = true; }
    close() { this.open = false; for (const callback of this.listeners.get('close') || []) callback({}); }
    reset() {
      const ids = this.id === 'world-form' ? ['world-id', 'world-name', 'world-base-url'] : ['conversation-title', 'conversation-message'];
      for (const id of ids) nodes.get(id).value = '';
    }
  }
  const html = source('index.html');
  for (const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*\bid="([^"]+)"[^>]*)>/gi)) {
    const node = new Node(match[1]);
    node.id = match[3];
    node.hidden = /\shidden(?:\s|$)/.test(match[2]);
    node.disabled = /\sdisabled(?:\s|$)/.test(match[2]);
    node.type = match[2].match(/\btype="([^"]+)"/)?.[1] || '';
    nodes.set(node.id, node);
  }
  const closers = [...html.matchAll(/<button\b([^>]*\bdata-close="([^"]+)"[^>]*)>/g)].map((match) => {
    const id = match[1].match(/\bid="([^"]+)"/)?.[1];
    const node = id ? nodes.get(id) : new Node('button');
    node.setAttribute('data-close', match[2]);
    return node;
  });
  const intervals = new Map();
  const timeouts = new Map();
  const fetches = [];
  const navigations = [];
  const clipboard = [];
  const events = new Map();
  let sequence = 0;
  function normalResponse(path) {
    if (path === '/api/me') return reply(me);
    if (path === '/health') return reply({ version: 'test' });
    if (path === '/api/worlds') return reply({ items: worlds });
    if (path.startsWith('/api/activity?')) return reply({ items: [] });
    throw new Error(`Unexpected test request: ${path}`);
  }
  let responder = respond;
  const document = {
    visibilityState: 'visible',
    getElementById(id) { return nodes.get(id) || null; },
    createElement(tag) { return new Node(tag); },
    createTextNode(value) { const node = new Node('#text'); node.textContent = value; return node; },
    querySelectorAll(selector) { assert.equal(selector, '[data-close]'); return closers; },
    addEventListener(type, callback) { events.set(type, callback); },
  };
  const sandbox = {
    document, URL, URLSearchParams, AbortController, console,
    crypto: { randomUUID: () => `synthetic-command-${++sequence}` },
    location: { origin: 'https://operator.example.test', protocol: 'https:', assign(url) { navigations.push(url); } },
    navigator: { clipboard: { async writeText(value) { clipboard.push(value); } } },
    setInterval(callback, ms) { const id = ++sequence; intervals.set(id, { callback, ms }); return id; },
    clearInterval(id) { intervals.delete(id); },
    setTimeout(callback, ms) { const id = ++sequence; timeouts.set(id, { callback, ms }); return id; },
    clearTimeout(id) { timeouts.delete(id); },
    addEventListener(type, callback) { events.set(type, callback); },
    async fetch(path, options) {
      fetches.push({ path, options });
      return responder ? responder(path, options, normalResponse) : normalResponse(path);
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source('app.js'), sandbox, { filename: 'app.js' });
  await settle();
  return {
    window: sandbox, id: (id) => nodes.get(id), fetches, navigations, clipboard, intervals, timeouts,
    normalResponse, setResponder(value) { responder = value; },
    async event(type, details = {}) { events.get(type)(details); await settle(); },
    get focused() { return focused; },
  };
}

function themeTokens(css, name) {
  const block = css.match(new RegExp('\\[data-theme="' + name + '"\\]\\s*\\{([^}]+)\\}'));
  assert.ok(block, `Missing ${name} theme token block`);
  return Object.fromEntries([...block[1].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((match) => [match[1], match[2].trim()]));
}

function luminance(hex) {
  assert.match(hex, /^#[\da-f]{6}$/i, `Contrast measurements need an opaque color: ${hex}`);
  const channels = hex.slice(1).match(/../g).map((part) => {
    const s = parseInt(part, 16) / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}

function contrast(a, b) {
  const values = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (values[0] + 0.05) / (values[1] + 0.05);
}

test('No-flash script precedes CSS; deferred theme code precedes the app; no third-party assets', () => {
  const html = source('index.html');
  assert.ok(html.indexOf('id="theme-init"') < html.indexOf('<link'));
  assert.match(html, /<script\b[^>]*src="\/static\/theme\.js"[^>]*defer/);
  assert.match(html, /<script\b[^>]*src="\/static\/app\.js"[^>]*defer/);
  assert.ok(html.indexOf('/static/theme.js') < html.indexOf('/static/app.js'));
  assert.doesNotMatch(html, /(?:src|href)="https?:\/\//i);
  assert.doesNotMatch(html, /\bon(?:click|load|submit|error)=/i);
});

test('Light and dark have identical semantic tokens, color schemes and reduced-motion support', () => {
  const css = source('styles.css');
  const light = themeTokens(css, 'light');
  const dark = themeTokens(css, 'dark');
  assert.deepEqual(Object.keys(light).sort(), Object.keys(dark).sort());
  for (const name of ['--bg', '--surface', '--surface-raised', '--surface-soft', '--text', '--muted', '--border', '--focus', '--accent', '--on-accent']) {
    assert.ok(light[name] && dark[name], `Missing semantic token ${name}`);
  }
  assert.match(css, /color-scheme:\s*light\s*;/);
  assert.match(css, /color-scheme:\s*dark\s*;/);
  assert.match(css, /prefers-reduced-motion:\s*reduce/);
  assert.match(css, /@media[^{}]*max-width/);
});

test('Both palettes meet measured WCAG text, control-border and focus contrast', () => {
  const css = source('styles.css');
  for (const name of ['light', 'dark']) {
    const tokens = themeTokens(css, name);
    const expectRatio = (foreground, background, minimum) => {
      const ratio = contrast(tokens[foreground], tokens[background]);
      assert.ok(ratio >= minimum, `${name}: ${foreground} on ${background} is ${ratio.toFixed(2)}:1; need ${minimum}:1`);
    };
    for (const surface of ['--bg', '--surface', '--surface-raised', '--surface-soft']) {
      expectRatio('--text', surface, 4.5);
      expectRatio('--muted', surface, 4.5);
      expectRatio('--border', surface, 3);
      expectRatio('--focus', surface, 3);
    }
    expectRatio('--on-accent', '--accent', 4.5);
    expectRatio('--on-accent', '--accent-hover', 4.5);
    for (const status of ['accent', 'positive', 'warning', 'danger', 'info']) {
      expectRatio(`--${status}`, `--${status}-soft`, 4.5);
    }
  }
});

test('Components use semantic color tokens rather than raw component colors', () => {
  const css = source('styles.css');
  const components = css.replace(/:root,\s*\[data-theme="light"\]\s*\{[^}]+\}/, '').replace(/\[data-theme="dark"\]\s*\{[^}]+\}/, '').replace(/:root:not\(\[data-theme\]\)\s*\{[^}]+\}/, '');
  assert.doesNotMatch(components, /#[\da-f]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\s*\(/i);
  assert.doesNotMatch(components, /(?:color|background(?:-color)?|border-color):\s*(?:white|black|gray|grey|red|blue|transparent)\b/i);
  assert.match(components, /:focus-visible/);
});

test('Network text has no HTML execution sinks; the enrollment secret field is protected and cleared', () => {
  const app = source('app.js');
  const html = source('index.html');
  assert.doesNotMatch(app, /\.(?:innerHTML|outerHTML)\s*=|insertAdjacentHTML|document\.write|\beval\s*\(/);
  assert.doesNotMatch(app, /localStorage|sessionStorage|console\.(?:log|debug|info)/);
  assert.match(app, /\.textContent\s*=/);
  const input = html.match(/<input[^>]*id="connector-token"[^>]*>/)?.[0];
  assert.ok(input, 'Missing connector-token');
  assert.match(input, /type="password"/);
  assert.match(input, /autocomplete="off"/);
  assert.match(input, /readonly/);
  assert.match(app, /connectorToken\.value\s*=\s*''/);
  assert.match(app, /state\.enrollment\s*=\s*null/);
  assert.match(app, /worldDialog\.addEventListener\('close', clearEnrollment\)/);
});

test('Safe world and conversation links reject unsafe schemes, credentials and relative URLs', () => {
  const h = helpers();
  for (const invalid of ['javascript:alert(1)', 'data:text/html,test', 'file:///secret', 'https://user:pass@example.com', '//example.com', '/agent/helper', 'not a url']) {
    assert.equal(h.safeWorldUrl(invalid), null, invalid);
    assert.equal(h.conversationUrl(invalid, 'helper', 'c_1'), null, invalid);
  }
  assert.equal(h.safeWorldUrl('https://world.example/portal/'), 'https://world.example/portal/');
  assert.equal(h.safeWorldUrl('http://localhost:8786'), 'http://localhost:8786/');
  const link = new URL(h.conversationUrl('https://world.example/portal/?old=1#fragment', 'a/b', 'c_1&escape=true'));
  assert.equal(link.pathname, '/portal/agent/a%2Fb/conversation/c_1%26escape%3Dtrue');
  assert.equal(link.search, '');
  assert.equal(link.searchParams.has('escape'), false);
  assert.equal(link.searchParams.has('old'), false);
  assert.equal(link.hash, '');
});

test('World rows consume per-world snapshots and retain offline freshness and routing data', () => {
  const h = helpers();
  const worlds = h.normalizeWorlds([
    { id: 'alpha', name: '<img src=x>', baseUrl: 'https://alpha.example', connected: true, lastSeen: '2026-01-01T10:00:00Z', snapshot: {
      observedAt: '2026-01-01T09:59:50Z', agents: [{ id: 'helper', name: 'Helper' }],
      conversations: [{ id: 'c_1', agentId: 'helper', title: '<script>unsafe</script>', updatedAt: '2026-01-01T09:58:00Z' }],
      questions: [{ conversationId: 'c_1', agentId: 'helper', requestId: 'r_1', prompt: 'Choose', inputType: 'free_form', choices: [] }],
    } },
    { id: 'beta', name: 'Beta', connected: false, lastSeen: '2026-01-01T09:00:00Z', snapshot: { agents: [], conversations: [], questions: [] } },
    { id: 'empty', snapshot: null },
  ]);
  assert.equal(worlds.length, 3);
  assert.equal(worlds[0].name, '<img src=x>');
  assert.equal(worlds[0].observedAt, '2026-01-01T09:59:50Z');
  assert.equal(worlds[1].connected, false);
  assert.equal(worlds[1].lastSeen, '2026-01-01T09:00:00Z');
  assert.equal(h.collectConversations(worlds)[0].world.id, 'alpha');
  assert.equal(h.collectConversations(worlds)[0].conversation.title, '<script>unsafe</script>');
  assert.equal(h.collectQuestions(worlds)[0].question.requestId, 'r_1');
  assert.equal(h.normalizeWorlds(null).length, 0);
});

test('Question helpers handle free text, single choices and multiple choices without losing values', () => {
  const h = helpers();
  assert.equal(h.questionMode({ inputType: 'free_form', choices: [] }), 'free_form');
  assert.equal(h.questionMode({ inputType: 'single_choice', choices: ['Yes', 'No'] }), 'single');
  assert.equal(h.questionMode({ inputType: 'multiple_choice', choices: ['A', 'B'] }), 'multiple');
  assert.equal(h.questionMode({ inputType: 'choice_or_free_form', choices: ['A', 'B'] }), 'mixed');
  assert.equal(h.questionMode({ inputType: 'single_choice', allowFreeForm: true, choices: ['A'] }), 'mixed');
  assert.equal(h.questionMode({ inputType: 'choice', allowMultiple: true, choices: ['A', 'B'] }), 'multiple');
  assert.equal(h.normalizeChoices(['Yes', { label: 'No', value: 'no' }])[1].value, 'no');
  assert.equal(h.normalizeChoices(['<img onerror=alert(1)>'])[0].label, '<img onerror=alert(1)>');
});

test('Requests stay same-origin, refuse redirects, keep the CSRF header and use fresh idempotency keys', () => {
  const h = helpers();
  const get = h.buildRequestOptions('GET');
  assert.equal(get.credentials, 'same-origin');
  assert.equal(get.headers['X-Hub-Request'], '1');
  assert.equal(get.cache, 'no-store');
  assert.equal(get.redirect, 'error');
  assert.equal(get.headers.Authorization, undefined);
  const post = h.buildRequestOptions('POST', { action: 'send_message', payload: { message: 'Hello' } });
  assert.equal(post.headers['Idempotency-Key'], 'unique-test-command-key');
  assert.equal(post.headers['Content-Type'], 'application/json');
  assert.equal(JSON.parse(post.body).action, 'send_message');
  assert.equal(h.buildRequestOptions('POST', {}, 'explicit-key').headers['Idempotency-Key'], 'explicit-key');
});

test('Command presentation calls success acceptance, keeps uncertain outcomes visible and never claims completion', () => {
  const h = helpers();
  assert.equal(h.commandPresentation('succeeded').label, 'Accepted');
  assert.match(h.commandPresentation('succeeded').detail, /not.*complet|still.*work|queue/i);
  assert.match(h.commandPresentation('uncertain').label, /unknown|uncertain/i);
  assert.match(h.commandPresentation('uncertain').detail, /not.*retr|never.*retr/i);
  assert.match(h.commandPresentation('failed').label, /not accepted|failed/i);
  assert.doesNotMatch(h.commandPresentation('succeeded').label, /complete|finished|succeeded/i);
});

test('The app polls read APIs while visible and has explicit command-status and visibility handling', () => {
  const app = source('app.js');
  assert.match(app, /POLL_INTERVAL_MS\s*=\s*[1-5]000\s*;/);
  assert.match(app, /visibilitychange/);
  assert.match(app, /visibilityState/);
  assert.match(app, /clearInterval\s*\(/);
  assert.match(app, /\/api\/worlds/);
  assert.match(app, /\/api\/activity/);
  assert.match(app, /\/api\/commands\//);
  assert.match(app, /202/);
  assert.match(app, /uncertain/);
  assert.match(app, /cancelled/);
  assert.match(app, /selectedValues/);
});

test('Dashboard exposes labelled tabs, forms, live feedback and stable browser-test hooks', () => {
  const html = source('index.html');
  for (const id of ['hub-app', 'access-panel', 'access-message', 'secure-access-button', 'dashboard', 'theme-toggle', 'world-filter', 'world-grid', 'new-conversation-button', 'conversation-dialog', 'conversation-form', 'add-world-button', 'world-dialog', 'world-form', 'connector-snippet', 'conversation-list', 'question-list', 'activity-list', 'command-list']) {
    assert.ok(html.includes(`id="${id}"`), `Missing stable selector #${id}`);
  }
  assert.match(html, /role="tablist"/);
  assert.match(html, /role="tab"/);
  assert.match(html, /aria-live="polite"/);
  assert.match(html, /<dialog[^>]*aria-labelledby/);
});


test('Native operator sign-in, credentials, cookies and fake sign-out are absent from the UI', () => {
  const html = source('index.html');
  const app = source('app.js');
  for (const content of [html, app]) {
    assert.doesNotMatch(content, /operator[ -](?:token|key)|HUB_ADMIN_TOKEN|HUB_SESSION_SECRET|\/api\/(?:login|logout)|login-form|login-button|logout-button|login-panel|tokenInput|document\.cookie/i);
    assert.doesNotMatch(content, /httpOnly|signed out|session expired|one-time(?:-use)? (?:connector )?token/i);
  }
  assert.match(html, /id="secure-access-button"[^>]*type="button"[^>]*>Reload secure access/);
  assert.match(html, /id="access-message"[^>]*role="alert"/);
  assert.match(html, /remains valid until rotated/);
  assert.match(html, /shown only once/);
});

test('Session admission requires external-preauth and an explicit, safe connector address', () => {
  const h = helpers();
  assert.equal(h.externalSession(verifiedSession).connectorUrl, verifiedSession.connectorUrl);
  for (const data of [null, [], {}, true, { ...verifiedSession, authenticated: false }, { ...verifiedSession, authenticated: 'true' }, { ...verifiedSession, authMode: undefined }, { ...verifiedSession, authMode: 'cookie' }, { ...verifiedSession, authMode: 'bearer' }, { ...verifiedSession, authMode: 'EXTERNAL-PREAUTH' }]) {
    assert.equal(h.externalSession(data), null);
  }
  for (const connectorUrl of [undefined, '', '/api/connect', '//connect.example.test', 'javascript:alert(1)', 'wss://connect.example.test', 'https://user:pass@connect.example.test', 'https://connect.example.test/?secret=1', 'https://connect.example.test/#fragment', 'https://connect.example.test/\n']) {
    assert.equal(h.externalSession({ ...verifiedSession, connectorUrl }), null, String(connectorUrl));
  }
  assert.equal(h.safeConnectorUrl('http://127.0.0.1:18885/'), 'http://127.0.0.1:18885');
});

test('Connector configurations use the separate connectorUrl, never the operator page origin', () => {
  const h = helpers();
  const enrollment = { worldId: 'test-a', name: 'Test A', baseUrl: 'https://world.example.test', connectorUrl: 'https://hub-connect.example.test/connector/' };
  const masked = h.connectorConfiguration(enrollment).gateway.extensions['botnexus-hub-connector'];
  assert.equal(masked.url, 'https://hub-connect.example.test/connector');
  assert.equal(masked.worldId, 'test-a');
  assert.equal(masked.worldName, 'Test A');
  assert.equal(masked.worldBaseUrl, enrollment.baseUrl);
  assert.equal(masked.worldToken, '<CONNECTOR_TOKEN>');
  assert.equal(masked.allowedAgents[0], 'REPLACE_WITH_AGENT_ID');
  assert.equal(h.connectorConfiguration(enrollment, 'synthetic-world-secret').gateway.extensions['botnexus-hub-connector'].worldToken, 'synthetic-world-secret');
  assert.throws(() => h.connectorConfiguration({ ...enrollment, connectorUrl: undefined }), /connector address/);
  assert.doesNotMatch(source('app.js'), /window\.location\.(?:origin|protocol|host|href)|new URL\(endpoint\)/);
  assert.match(source('app.js'), /connectorConfiguration\(state\.enrollment, displayToken\)/);
  assert.match(source('app.js'), /connectorConfiguration\(state\.enrollment, state\.enrollment\.token\)/);
});

test('Verified external access opens the dashboard directly and starts only read polling', async () => {
  const h = await appHarness({ worlds: [syntheticWorld] });
  assert.equal(h.id('dashboard').hidden, false);
  assert.equal(h.id('access-panel').hidden, true);
  assert.equal(h.id('session-status').hidden, true);
  assert.match(h.id('world-grid').textContent, /Private world/);
  assert.equal(h.intervals.size, 2);
  assert.equal(h.fetches.filter(({ path }) => path === '/api/me').length, 1);
  assert.equal(h.navigations.length, 0);
  for (const { options } of h.fetches) {
    assert.equal(options.method, 'GET');
    assert.equal(options.redirect, 'error');
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.headers['X-Hub-Request'], '1');
    assert.equal(options.headers.Authorization, undefined);
  }
});

test('The dashboard stays closed for legacy auth modes and missing connector settings', async () => {
  for (const me of [{ authenticated: true }, { ...verifiedSession, authMode: 'cookie' }, { ...verifiedSession, authMode: 'bearer' }, { ...verifiedSession, connectorUrl: undefined }]) {
    const h = await appHarness({ me });
    assert.equal(h.id('dashboard').hidden, true);
    assert.equal(h.id('access-panel').hidden, false);
    assert.equal(h.intervals.size, 0);
    assert.deepEqual(h.fetches.map(({ path }) => path), ['/api/me']);
    assert.deepEqual(h.navigations, []);
  }
});

const deniedResponses = [
  ['401', () => reply({ detail: 'private denial details' }, { status: 401 })],
  ['403', () => reply({ detail: 'private denial details' }, { status: 403 })],
  ['302', () => reply('', { status: 302 })],
  ['already redirected', () => reply(verifiedSession, { redirected: true })],
  ['opaque redirect', () => reply('', { type: 'opaqueredirect' })],
  ['HTML page', () => reply('<html>private denial details</html>', { contentType: 'text/html; charset=utf-8' })],
  ['XHTML page', () => reply('<html>private denial details</html>', { contentType: 'application/xhtml+xml' })],
  ['mislabeled HTML', () => reply('  <!doctype html><html>private denial details</html>')],
  ['blocked redirect or network failure', () => { throw new TypeError('Failed to fetch'); }],
];

test('Access denials and blocked redirects need an explicit reload; no token fallback or redirect retry', async () => {
  for (const [label, deny] of deniedResponses) {
    const h = await appHarness({ respond(path, options, normal) { return path === '/api/me' ? deny() : normal(path); } });
    assert.equal(h.id('dashboard').hidden, true, label);
    assert.equal(h.id('access-panel').hidden, false, label);
    assert.match(h.id('access-message').textContent, /external access|secure access/);
    assert.doesNotMatch(h.id('access-message').textContent, /private denial details|expired|signed out|SSO ended/i);
    if (label === 'blocked redirect or network failure') assert.match(h.id('access-message').textContent, /connection/);
    assert.equal(h.intervals.size, 0, label);
    assert.equal(h.timeouts.size, 0, label);
    assert.deepEqual(h.fetches.map(({ path }) => path), ['/api/me'], label);
    assert.deepEqual(h.navigations, [], label);
    await h.event('visibilitychange');
    await h.event('pageshow', { persisted: true });
    await h.window.__hub.refresh();
    assert.equal(h.fetches.length, 1, label);
    await h.id('secure-access-button').fire('click');
    assert.deepEqual(h.navigations, ['/'], label);
    assert.equal(h.fetches.length, 1, label);
  }
});

async function enrollSyntheticWorld(h) {
  await h.id('add-world-button').fire('click');
  h.id('world-id').value = 'synthetic-enrollment';
  h.id('world-name').value = 'Private enrollment';
  h.id('world-base-url').value = 'https://private-world.example.test';
  await h.id('world-form').fire('submit');
}

function enrollmentResponse(path, options, normal) {
  if (path === '/api/worlds' && options.method === 'POST') return reply({ id: 'synthetic-enrollment', connectorToken: 'synthetic-world-secret' }, { status: 201 });
  return normal(path);
}

test('Enrollment display and explicit copy share connectorUrl, mask secrets, and clear them on close', async () => {
  const h = await appHarness({ respond: enrollmentResponse });
  assert.equal(h.fetches.some(({ options }) => options.method !== 'GET'), false);
  await enrollSyntheticWorld(h);
  assert.equal(h.id('world-enrollment').hidden, false);
  assert.equal(h.id('connector-token').value, 'synthetic-world-secret');
  assert.equal(h.id('connector-token').type, 'password');
  const config = () => JSON.parse(h.id('connector-snippet').textContent).gateway.extensions['botnexus-hub-connector'];
  assert.equal(config().url, verifiedSession.connectorUrl);
  assert.equal(config().worldId, 'synthetic-enrollment');
  assert.equal(config().worldToken, '<CONNECTOR_TOKEN>');
  assert.doesNotMatch(h.id('connector-snippet').textContent, /operator\.example|synthetic-world-secret/);
  assert.equal(h.id('connector-header-snippet').textContent, 'Authorization: Bearer <CONNECTOR_TOKEN>');
  assert.equal(h.clipboard.length, 0);
  await h.id('copy-connector-snippet').fire('click');
  const copied = JSON.parse(h.clipboard[0]).gateway.extensions['botnexus-hub-connector'];
  assert.equal(copied.url, verifiedSession.connectorUrl);
  assert.equal(copied.worldToken, 'synthetic-world-secret');
  await h.id('reveal-connector-token').fire('click');
  assert.equal(config().worldToken, 'synthetic-world-secret');
  assert.equal(h.id('connector-token').type, 'text');
  assert.equal(h.id('connector-header-snippet').textContent, 'Authorization: Bearer synthetic-world-secret');
  await h.id('enrollment-done').fire('click');
  assert.equal(h.id('world-dialog').open, false);
  assert.equal(h.id('connector-token').value, '');
  assert.equal(h.id('connector-token').type, 'password');
  assert.equal(h.id('connector-snippet').textContent, '');
  assert.equal(h.id('connector-header-snippet').textContent, '');
  const writes = h.fetches.filter(({ options }) => options.method !== 'GET');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].options.headers['X-Hub-Request'], '1');
});

test('Protected denials clear worlds, drafts and enrollment secrets and stop background access checks', async () => {
  for (const [label, deny] of deniedResponses) {
    const h = await appHarness({ worlds: [syntheticWorld], respond: enrollmentResponse });
    await enrollSyntheticWorld(h);
    await h.id('reveal-connector-token').fire('click');
    h.id('conversation-title').value = 'Private draft';
    h.id('conversation-message').value = 'Private message';
    h.id('conversation-dialog').open = true;
    h.setResponder((path, options, normal) => path === '/api/worlds' ? deny() : normal(path));
    await h.window.__hub.refresh();
    assert.equal(h.id('access-panel').hidden, false, label);
    assert.equal(h.id('dashboard').hidden, true, label);
    assert.equal(h.intervals.size, 0, label);
    assert.equal(h.navigations.length, 0, label);
    for (const id of ['world-grid', 'conversation-list', 'question-list', 'activity-list', 'command-list', 'connector-snippet', 'connector-header-snippet', 'enrollment-feedback']) assert.equal(h.id(id).textContent, '', `${label}: ${id}`);
    for (const id of ['connector-token', 'conversation-title', 'conversation-message', 'world-id', 'world-name', 'world-base-url']) assert.equal(h.id(id).value, '', `${label}: ${id}`);
    for (const id of ['conversation-world', 'conversation-agent', 'world-filter']) assert.equal(h.id(id).children.length, 0, `${label}: ${id}`);
    assert.equal(h.id('world-dialog').open, false, label);
    assert.equal(h.id('conversation-dialog').open, false, label);
    assert.equal(h.id('connector-token').type, 'password', label);
    assert.equal(h.id('reveal-connector-token').getAttribute('aria-pressed'), 'false', label);
    assert.equal(h.id('commands-panel').hidden, true, label);
    assert.doesNotMatch(h.id('access-message').textContent, /private|expired|signed out|SSO ended/i);
    const requests = h.fetches.length;
    await h.event('visibilitychange');
    await h.event('pageshow', { persisted: true });
    await h.window.__hub.refresh();
    await h.id('add-world-button').fire('click');
    await h.id('world-form').fire('submit');
    assert.equal(h.fetches.length, requests, label);
    assert.equal(h.id('world-dialog').open, false, label);
  }
});

test('Late enrollment and command responses cannot restore private data after an access epoch changes', async () => {
  for (const action of ['enrollment', 'command']) {
    const h = await appHarness({ worlds: [syntheticWorld] });
    let complete;
    h.setResponder((path, options, normal) => {
      if (options.method === 'POST') return new Promise((resolve) => { complete = resolve; });
      return normal(path);
    });
    let submitting;
    if (action === 'enrollment') {
      await h.id('add-world-button').fire('click');
      h.id('world-id').value = 'synthetic-enrollment';
      h.id('world-name').value = 'Private enrollment';
      h.id('world-base-url').value = 'https://private-world.example.test';
      submitting = h.id('world-form').fire('submit');
    } else {
      await h.id('new-conversation-button').fire('click');
      h.id('conversation-title').value = 'Private draft';
      h.id('conversation-message').value = 'Private message';
      submitting = h.id('conversation-form').fire('submit');
    }
    await settle();
    assert.equal(typeof complete, 'function', action);
    assert.equal(h.fetches.filter(({ options }) => options.method === 'POST').length, 1, action);
    h.setResponder((path, options, normal) => path === '/api/worlds' ? reply({}, { status: 403 }) : normal(path));
    await h.window.__hub.refresh();
    complete(action === 'enrollment' ? reply({ connectorToken: 'late-private-secret' }, { status: 201 }) : reply({ id: 'late-receipt', worldId: 'test-a', status: 'queued' }, { status: 202 }));
    await submitting;
    assert.equal(h.id('access-panel').hidden, false, action);
    assert.equal(h.id('dashboard').hidden, true, action);
    assert.equal(h.id('world-enrollment').hidden, true, action);
    assert.equal(h.id('connector-token').value, '', action);
    assert.equal(h.id('connector-snippet').textContent, '', action);
    assert.equal(h.id('command-list').textContent, '', action);
    assert.equal(h.fetches.filter(({ options }) => options.method === 'POST').length, 1, action);
    assert.equal(h.intervals.size, 0, action);
  }
});

test('Read-only server errors keep stale data and polling without claiming external sign-out', async () => {
  const h = await appHarness({ worlds: [syntheticWorld] });
  h.setResponder(() => reply({ detail: 'Service unavailable' }, { status: 503 }));
  await h.window.__hub.refresh();
  assert.equal(h.id('dashboard').hidden, false);
  assert.equal(h.id('access-panel').hidden, true);
  assert.match(h.id('world-grid').textContent, /Private world/);
  assert.match(h.id('dashboard-error').textContent, /last received data/);
  assert.equal(h.intervals.size, 2);
  assert.equal(h.navigations.length, 0);
  assert.equal(h.fetches.some(({ options }) => options.method !== 'GET'), false);
});

test('Browser E2E uses explicit disposable ingress and a proxy secret file, never native credentials', () => {
  const browser = fs.readFileSync(path.resolve(__dirname, 'browser_e2e.py'), 'utf8');
  assert.match(browser, /--base-url/);
  assert.match(browser, /--proxy-secret-file/);
  assert.match(browser, /--allow-disposable/);
  assert.match(browser, /--output/);
  assert.match(browser, /extra_http_headers\s*=\s*\{['"]X-Hub-Proxy-Secret['"]:\s*proxy_secret\}/);
  assert.match(browser, /headers\s*=\s*\{['"]X-Hub-Proxy-Secret['"]:\s*proxy_secret\}/);
  assert.match(browser, /\{'test-a',\s*'test-b'\}/);
  assert.doesNotMatch(browser, /--env-file|HUB_ADMIN_TOKEN|HUB_SESSION_SECRET|Authorization|Bearer|\/api\/(?:login|logout)/);
  assert.doesNotMatch(browser, /locator\(['"]#(?:operator-token|login-button)[^\n]+\.(?:fill|click)\(/);
});


test('Command denials or blocked redirects clear private receipts without following or replaying the action', async () => {
  for (const [label, deny] of deniedResponses) {
    const h = await appHarness({ worlds: [syntheticWorld] });
    h.setResponder((path, options, normal) => options.method === 'POST' ? deny() : normal(path));
    await h.id('new-conversation-button').fire('click');
    h.id('conversation-title').value = 'Private command';
    h.id('conversation-message').value = 'Private message';
    await h.id('conversation-form').fire('submit');
    assert.equal(h.id('access-panel').hidden, false, label);
    assert.equal(h.id('dashboard').hidden, true, label);
    assert.equal(h.id('conversation-dialog').open, false, label);
    assert.equal(h.id('conversation-message').value, '', label);
    assert.equal(h.id('command-list').textContent, '', label);
    assert.equal(h.intervals.size, 0, label);
    assert.equal(h.navigations.length, 0, label);
    const writes = h.fetches.filter(({ options }) => options.method === 'POST');
    assert.equal(writes.length, 1, label);
    assert.equal(writes[0].path, '/api/worlds/test-a/commands', label);
    assert.equal(writes[0].options.redirect, 'error', label);
    assert.equal(writes[0].options.headers['X-Hub-Request'], '1', label);
    const count = h.fetches.length;
    await h.event('visibilitychange');
    await h.id('secure-access-button').fire('click');
    assert.equal(h.fetches.length, count, label);
    assert.deepEqual(h.navigations, ['/'], label);
  }
});

test('A protected receipt-status denial stops polling and clears private receipts', async () => {
  const h = await appHarness({ worlds: [syntheticWorld] });
  h.setResponder((path, options, normal) => {
    if (options.method === 'POST') return reply({ id: 'private-receipt', worldId: 'test-a', status: 'queued' }, { status: 202 });
    if (path === '/api/commands/private-receipt') return reply({ detail: 'Authentication required' }, { status: 403 });
    return normal(path);
  });
  await h.id('new-conversation-button').fire('click');
  h.id('conversation-title').value = 'Private command';
  h.id('conversation-message').value = 'Private message';
  await h.id('conversation-form').fire('submit');
  assert.equal(h.id('access-panel').hidden, false);
  assert.equal(h.id('command-list').textContent, '');
  assert.equal(h.id('commands-panel').hidden, true);
  assert.equal(h.intervals.size, 0);
  assert.equal(h.fetches.filter(({ options }) => options.method === 'POST').length, 1);
  assert.equal(h.fetches.filter(({ path }) => path === '/api/commands/private-receipt').length, 1);
});

test('Uncertain command receipts remain visible and are never retried automatically', async () => {
  for (const response of [() => reply({ detail: 'Service unavailable' }, { status: 503 }), () => reply('invalid receipt'), () => reply({ id: 'wrong-world-receipt', worldId: 'other-world', status: 'queued' }, { status: 202 })]) {
    const h = await appHarness({ worlds: [syntheticWorld] });
    h.setResponder((path, options, normal) => options.method === 'POST' ? response() : normal(path));
    await h.id('new-conversation-button').fire('click');
    h.id('conversation-title').value = 'Synthetic uncertain command';
    h.id('conversation-message').value = 'Send once';
    await h.id('conversation-form').fire('submit');
    assert.equal(h.id('dashboard').hidden, false);
    assert.equal(h.id('access-panel').hidden, true);
    assert.equal(h.id('commands-panel').hidden, false);
    assert.match(h.id('command-list').textContent, /Outcome unknown/);
    assert.equal(h.id('conversation-submit').disabled, true);
    assert.equal(h.intervals.size, 2);
    for (const { callback } of [...h.intervals.values()]) await callback();
    await settle();
    assert.equal(h.fetches.filter(({ options }) => options.method === 'POST').length, 1);
    assert.equal(h.navigations.length, 0);
  }
});


test('Protected request timeouts clear private state while intentional hidden-tab cancellation does not fake sign-out', async () => {
  const h = await appHarness({ worlds: [syntheticWorld] });
  h.setResponder((path, options, normal) => path === '/api/worlds' ? new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('Synthetic abort')));
  }) : normal(path));
  const pending = h.window.__hub.refresh();
  await settle();
  const timeout = [...h.timeouts.values()].find((timer) => timer.ms === 12000);
  assert.ok(timeout, 'The actual production timeout must be registered');
  timeout.callback();
  await pending;
  assert.equal(h.id('dashboard').hidden, true);
  assert.equal(h.id('access-panel').hidden, false);
  assert.equal(h.id('world-grid').textContent, '');
  assert.equal(h.id('connector-token').value, '');
  assert.equal(h.intervals.size, 0);
  assert.equal(h.fetches.some(({ options }) => options.method !== 'GET'), false);

  const hidden = await appHarness({ worlds: [syntheticWorld] });
  hidden.setResponder((path, options, normal) => path === '/api/worlds' ? new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('Synthetic intentional abort')));
  }) : normal(path));
  const hiddenPending = hidden.window.__hub.refresh();
  await settle();
  hidden.window.document.visibilityState = 'hidden';
  await hidden.event('visibilitychange');
  await hiddenPending;
  assert.equal(hidden.id('dashboard').hidden, false);
  assert.equal(hidden.id('access-panel').hidden, true);
  assert.match(hidden.id('world-grid').textContent, /Private world/);
});
