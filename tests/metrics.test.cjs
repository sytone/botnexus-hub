'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const staticDir = path.resolve(__dirname, '../hub/static');
const source = fs.readFileSync(path.join(staticDir, 'metrics.js'), 'utf8');
const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(staticDir, 'metrics.css'), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));
const settle = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const NOW = 1794931200000;
const DAY = 86400000;
const DEFAULTS = ['gateway.cpu.percent', 'gateway.memory.rss_bytes', 'gateway.loops.active'];
const item = (id, name, unit, scope = 'world', aggregation = 'mean') => ({ id, name, unit, scope, aggregation, description: `${name} measured in the exact visible scope.` });
const CATALOG = { items: [
  item(DEFAULTS[0], 'Gateway CPU', '%'), item(DEFAULTS[1], 'Gateway resident memory', 'bytes'), item(DEFAULTS[2], 'Visible active loops', 'count'),
  item('gateway.threads.count', 'Gateway threads', 'count'), item('gateway.uptime.seconds', 'Gateway uptime', 'seconds'),
  item('gateway.io.read_bytes_per_second', 'Gateway I/O read rate', 'bytes/s'),
  item('agent.loops.active', 'Visible active agent loops', 'count', 'agent'), item('agent.loops.completed', 'Completed loops', 'count', 'agent', 'sum'),
  item('agent.loops.duration_ms', 'Completed loop duration', 'ms', 'agent'),
], defaultMetrics: DEFAULTS };
const SETTINGS = { retentionDays: 7, maxRows: 1000000, sampleIntervalSeconds: 30, rowCount: 6, databaseBytes: 16384, oldestAt: NOW - 3600000, newestAt: NOW - 1000, rejectedBatches: 0 };
const WORLDS = [
  { id: 'test-a', name: 'Test A', connected: true, agents: [{ id: 'helper', name: 'Helper' }], conversations: [{ id: 'c_alpha', agentId: 'helper', title: 'Alpha' }] },
  { id: 'test-b', name: 'Test B', connected: false, agents: [{ id: 'helper', name: 'Helper B' }], conversations: [{ id: 'c_beta', agentId: 'helper', title: 'Beta' }] },
];
const point = (timestamp, value, count = 1) => ({ timestamp, value, count });
function queryData({ from = NOW - 3600000, to = NOW, metrics = DEFAULTS, worlds = ['test-a', 'test-b'], agentId = null, conversationId = null } = {}) {
  const bucketMs = Math.ceil((to - from) / 240);
  return { from, to, retentionDays: 7, bucketMs, truncated: false, series: worlds.flatMap((worldId) => metrics.filter((id) => !agentId || id.startsWith('agent.')).map((metricId) => ({
    worldId, metricId, agentId: metricId.startsWith('agent.') ? agentId || 'helper' : null,
    conversationId: metricId.startsWith('agent.') ? conversationId || (worldId === 'test-a' ? 'c_alpha' : 'c_beta') : null,
    points: [point(from, 0), point(from + bucketMs, metricId.endsWith('_bytes') ? 1048576 : 4, 3), point(from + 6 * bucketMs, metricId.endsWith('_bytes') ? 2097152 : 2, 2)], latestAt: from + 7 * bucketMs - 1,
  }))) };
}
function tileData(worldIds = ['test-a', 'test-b'], selections = new Map(), to = NOW) {
  const from = to - 3600000;
  return { from, to, retentionDays: 7, metrics: DEFAULTS, worlds: Object.fromEntries(worldIds.map((id) => {
    const metrics = selections.get(id) || DEFAULTS;
    return [id, { metrics, series: metrics.map((metricId) => ({ metricId, worldId: id, agentId: null, conversationId: null,
      points: [point(to - 120000, 2), point(to - 60000, 0)], latestAt: to - 1000 })) }];
  })) };
}

// The real module runs in a DOM double. Storage and direct networking throw:
// reads, mutations and access failures must all go through the supplied API.
function harness({ respond, authenticated = true, initialWorlds = WORLDS, hash = '', retentionDays = 7 } = {}) {
  const nodes = new Map();
  const calls = [];
  const events = new Map();
  const selections = new Map();
  let active = authenticated;
  let enrolled = structuredClone(initialWorlds);
  let now = NOW;
  let savedSettings = { ...SETTINGS, retentionDays };
  class Node {
    constructor(tag = 'div') {
      this.tagName = tag.toUpperCase(); this.children = []; this.attributes = new Map(); this.listeners = new Map();
      this.className = ''; this.hidden = false; this.disabled = false; this.open = false; this._text = ''; this._value = undefined;
      this.clientWidth = 0;
    }
    set textContent(value) { this.replaceChildren(); this._text = String(value); }
    get textContent() { return this._text + this.children.map((node) => node.textContent).join(''); }
    set value(value) { this._value = String(value); }
    get value() {
      if (this._value !== undefined) return this._value;
      if (this.tagName === 'SELECT') return this.querySelector('option')?.value || '';
      return '';
    }
    append(...children) {
      for (const child of children) { child.remove(); child.parent = this; this.children.push(child); }
    }
    replaceChildren(...children) { for (const child of this.children) child.parent = null; this.children = []; this._text = ''; this._value = undefined; this.append(...children); }
    remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; }
    setAttribute(name, value) { this.attributes.set(name, String(value)); if (name === 'id') this.id = String(value); if (name === 'class') this.className = String(value); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    removeAttribute(name) { this.attributes.delete(name); }
    matches(selector) {
      if (selector.startsWith('.')) return this.className.split(/\s+/).includes(selector.slice(1));
      if (selector.startsWith('[')) return this.attributes.has(selector.slice(1, -1));
      return this.tagName.toLowerCase() === selector;
    }
    querySelectorAll(selector) { return this.children.flatMap((child) => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    contains(node) { return node === this || this.children.some((child) => child.contains(node)); }
    addEventListener(type, callback) { if (!this.listeners.has(type)) this.listeners.set(type, []); this.listeners.get(type).push(callback); }
    async fire(type, details = {}) {
      if (this.disabled && ['click', 'change'].includes(type)) return;
      const event = { preventDefault() {}, ...details };
      await Promise.all((this.listeners.get(type) || []).map((callback) => callback(event))); await settle();
    }
    focus() { document.activeElement = this; for (const callback of this.listeners.get('focus') || []) callback({}); }
    showModal() { this.open = true; }
    close() { this.open = false; }
    scrollIntoView() {}
    getBoundingClientRect() { return { left: 0, width: 580, height: 246 }; }
  }
  for (const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*\bid="([^"]+)"[^>]*)>/gi)) {
    const node = new Node(match[1]); node.id = match[3];
    node.hidden = /\shidden(?:\s|$)/.test(match[2]); node.disabled = /\sdisabled(?:\s|$)/.test(match[2]);
    for (const attr of match[2].matchAll(/([\w-]+)="([^"]*)"/g)) node.setAttribute(attr[1], attr[2]);
    nodes.set(node.id, node);
  }
  for (const range of ['1h', '24h', '7d', 'full']) {
    const node = new Node('button'); node.setAttribute('data-metrics-range', range); node.setAttribute('aria-pressed', String(range === 'full')); nodes.get('metrics-presets').append(node);
  }
  const document = {
    visibilityState: 'visible', activeElement: null,
    getElementById(id) { return nodes.get(id) || null; }, createElement(tag) { return new Node(tag); },
    createElementNS(namespace, tag) { assert.equal(namespace, 'http://www.w3.org/2000/svg'); return new Node(tag); },
    addEventListener(type, callback) { events.set(type, callback); },
  };
  class Clock extends Date { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return now; } }
  const sandbox = { document, AbortController, URL, URLSearchParams, Date: Clock, console, innerWidth: 1280,
    location: { pathname: '/', search: '', hash },
    history: { replaceState(_state, _unused, value) { sandbox.location.hash = value.includes('#') ? value.slice(value.indexOf('#')) : ''; } },
    addEventListener(type, callback) { events.set(type, callback); },
    requestAnimationFrame(callback) { callback(); },
    setInterval() { assert.fail('Metrics must use parent refresh calls, not another polling timer.'); },
    fetch() { assert.fail('Metrics must use the parent authenticated API.'); },
  };
  for (const name of ['localStorage', 'sessionStorage', 'indexedDB']) Object.defineProperty(sandbox, name, { get() { assert.fail(`Metrics must not use ${name}.`); } });
  sandbox.window = sandbox;
  function normalResponse(url, method, body) {
    const parsed = new URL(url, 'https://hub.example.test');
    if (url === '/api/metrics/catalog') return structuredClone(CATALOG);
    if (url === '/api/metrics/settings') {
      if (method === 'PUT') savedSettings.retentionDays = body.retentionDays;
      return { ...savedSettings };
    }
    if (parsed.pathname === '/api/metrics/dimensions') {
      const worldId = parsed.searchParams.get('worldId'); const selectedWorlds = enrolled.filter((world) => !worldId || worldId === world.id);
      return { worlds: selectedWorlds.map((world) => world.id), agents: selectedWorlds.flatMap((world) => world.agents.map((agent) => ({ worldId: world.id, agentId: agent.id }))),
        conversations: selectedWorlds.flatMap((world) => world.conversations.map((conversation) => ({ worldId: world.id, agentId: conversation.agentId, conversationId: conversation.id }))) };
    }
    if (parsed.pathname === '/api/metrics/tiles') return tileData(enrolled.filter((world) => !parsed.searchParams.has('worldId') || world.id === parsed.searchParams.get('worldId')).map((world) => world.id), selections, now);
    if (parsed.pathname === '/api/metrics/query') return queryData({ from: Number(parsed.searchParams.get('from')), to: Number(parsed.searchParams.get('to')),
      metrics: parsed.searchParams.getAll('metricId'), worlds: enrolled.filter((world) => !parsed.searchParams.has('worldId') || world.id === parsed.searchParams.get('worldId')).map((world) => world.id),
      agentId: parsed.searchParams.get('agentId'), conversationId: parsed.searchParams.get('conversationId') });
    const match = parsed.pathname.match(/^\/api\/worlds\/([^/]+)\/metrics$/);
    if (match && method === 'PUT') { const worldId = decodeURIComponent(match[1]); selections.set(worldId, [...body.metrics]); return { worldId, metrics: [...body.metrics] }; }
    throw new Error(`Unexpected metrics request ${method} ${url}`);
  }
  async function api(url, method = 'GET', body, key, options) {
    calls.push({ url, method, body, key, options });
    const payload = respond ? await respond(url, method, body, normalResponse, options) : normalResponse(url, method, body);
    return { data: payload, status: 200 };
  }
  vm.createContext(sandbox); vm.runInContext(source, sandbox, { filename: 'metrics.js' });
  const metrics = sandbox.__hub.metrics;
  assert.ok(metrics, 'Expose the real metrics controller.');
  const attached = metrics.attach({ api, getWorlds: () => enrolled, isActive: () => active });
  assert.equal(attached, metrics);
  return { metrics, helpers: metrics.helpers, document, window: sandbox, calls, id: (id) => nodes.get(id), normalResponse,
    setActive(value) { active = value; }, setWorlds(value) { enrolled = structuredClone(value); }, advance(ms) { now += ms; },
    async event(type) { events.get(type)?.({}); await settle(); }, async flush() { await settle(); await settle(); },
  };
}

const helperHarness = harness();
const h = helperHarness.helpers;
const catalog = h.normalizeCatalog(CATALOG);

test('Zero is a measured value; null, strings, booleans, negative and nonfinite values are missing', () => {
  for (const value of [0, 0.000001, 42, 1e18]) assert.equal(h.finiteValue(value), true);
  for (const value of [null, undefined, '0', false, true, NaN, Infinity, -1, 1e19]) assert.equal(h.finiteValue(value), false);
  const points = h.normalizePoints([point(1, null), point(2, false), point(3, '0'), point(4, 0), point(5, 1), point(6, Infinity)]);
  assert.deepEqual(plain(points), [point(4, 0), point(5, 1)]);
  assert.equal(h.summarize([]).latest, null); assert.equal(h.summarize([point(1, 0)]).latest, 0);
  assert.equal(h.formatValue(0, '%'), '0 %'); assert.equal(h.formatValue(null, '%'), '—');
});

test('Points are sorted, deduplicated and bounded; invalid dates, weights and range endpoints are dropped', () => {
  const points = h.normalizePoints([point(8, 8), point(2, 1), point(2, 0), point(3, 5, 0), point(4, 5, 1.5), point(-1, 5), point(10, 5), point(5.5, 5), null], 2, 10);
  assert.deepEqual(plain(points), [point(2, 0), point(8, 8)]);
  assert.equal(h.normalizePoints(Array.from({ length: 2000 }, (_, index) => point(index, 0))).length, 720);
  assert.deepEqual(plain(h.normalizePoints({ points: [] })), []);
});

test('Weighted averages retain real zero, and interval sums are not multiplied by their weights', () => {
  const points = [point(1, 0, 3), point(2, 8, 1)];
  assert.equal(h.summarize(points).average, 2);
  assert.equal(h.summarize(points, 'sum').total, 8);
  assert.equal(h.summarize(points, 'sum').average, 4);
  assert.equal(h.summarize(points).latest, 8);
});

test('Catalog and settings reject malformed envelopes; only catalog world metrics are valid card selections', () => {
  for (const payload of [null, {}, { items: [] }, { items: [null, { id: 'bad' }] }]) assert.equal(h.normalizeCatalog(payload), null);
  const mixed = h.normalizeCatalog({ items: [...CATALOG.items, CATALOG.items[0], { ...CATALOG.items[0], id: '<script>' }], defaultMetrics: ['unknown', DEFAULTS[0], 'agent.loops.active'] });
  assert.equal(mixed.items.length, CATALOG.items.length); assert.deepEqual(plain(mixed.defaultMetrics), [DEFAULTS[0]]);
  for (const retentionDays of [0, 366, 1.5, '7', null]) assert.equal(h.normalizeSettings({ retentionDays }), null);
  for (const value of ['0', '366', '1.2', '1e2', ' 7 ', '', '-1']) assert.equal(h.parseRetention(value), null);
  assert.equal(h.parseRetention('365'), 365); assert.equal(h.parseRetention(1), 1);
  assert.equal(h.validSelection([], catalog), null); assert.equal(h.validSelection([DEFAULTS[0], DEFAULTS[0]], catalog), null);
  assert.equal(h.validSelection(['agent.loops.active'], catalog, 3, true), null);
  assert.deepEqual(plain(h.validSelection(DEFAULTS, catalog, 3, true)), DEFAULTS);
});

test('Query normalization never merges worlds or conversation dimensions, and rejects malformed/cross-world rows', () => {
  const data = queryData({ metrics: ['agent.loops.completed'] });
  data.series.push({ ...data.series[0], conversationId: 'c_other', points: [point(data.from, 0)] });
  data.series.push({ ...data.series[0], worldId: 'untrusted' }, { ...data.series[0], metricId: 'unknown' }, { ...data.series[0], agentId: null });
  const clean = h.normalizeQuery(data, catalog, ['test-a', 'test-b']);
  assert.equal(clean.series.length, 3); assert.ok(clean.omitted >= 3);
  assert.equal(clean.series[2].points[0].value, 0);
  assert.equal(clean.series[0].worldId, 'test-a'); assert.equal(clean.series[1].worldId, 'test-b');
  for (const payload of [null, {}, { ...data, to: data.from }, { ...data, from: '0' }, { ...data, series: {} }]) assert.equal(h.normalizeQuery(payload, catalog), null);
  const limited = h.normalizeQuery({ ...data, series: Array.from({ length: 100 }, (_, index) => ({ ...data.series[0], conversationId: `c_${index}` })) }, catalog);
  assert.equal(limited.series.length, 64); assert.equal(limited.truncated, true);
});

test('Tile normalization ignores inherited or unenrolled worlds, never adds agent data or fabricates zero', () => {
  const payload = tileData(); payload.worlds['test-a'].metrics = ['agent.loops.active'];
  const clean = h.normalizeTiles(payload, catalog, ['test-a', 'test-b']);
  assert.equal(clean.worlds.has('test-a'), false); assert.equal(clean.worlds.has('test-b'), true);
  assert.equal(clean.worlds.get('test-b').series[0].points.at(-1).value, 0);
  const inherited = { ...payload, worlds: Object.create({ 'test-b': payload.worlds['test-b'] }) };
  assert.equal(h.normalizeTiles(inherited, catalog, ['test-b']).worlds.size, 0);
  assert.equal(h.normalizeTiles({ from: 0, to: 1, worlds: [] }, catalog, ['test-a']), null);
});

test('Constant-zero, constant-positive, singleton and empty plots have finite readable domains and positions', () => {
  for (const points of [[], [point(1, 0)], [point(1, 9)], [point(1, 9), point(2, 9)], [point(1, NaN)], [point(1, 1e18)]]) {
    const model = h.plotModel(points, { from: 0, to: 10, metric: CATALOG.items[2], bucketMs: 1, intervalMs: 1 });
    assert.ok(model.domain.max > 0); assert.ok(model.domain.ticks.length >= 2);
    assert.ok(model.paths.every((value) => !/NaN|Infinity|undefined/.test(value)));
    for (const segment of model.segments) for (const item of segment) { assert.ok(Number.isFinite(model.x(item.timestamp))); assert.ok(Number.isFinite(model.y(item.value))); }
  }
  assert.deepEqual(plain(h.niceDomain([0, 25, 99], CATALOG.items[0]).ticks), [0, 25, 50, 75, 100]);
  const model = h.plotModel([point(0, 0), point(999, 9)], { from: 0, to: 1000 });
  assert.equal(model.x(0), model.left); assert.ok(model.x(999) < model.width - model.right); assert.equal(model.y(0), model.height - model.bottom);
});

test('A missing bucket or long collection outage splits paths, while true zero stays on the plotted baseline', () => {
  const points = [point(0, 2), point(30000, 0), point(90000, 4), point(3600000, 0)];
  const model = h.plotModel(points, { from: 0, to: 3700000, bucketMs: 30000, intervalMs: 30000 });
  assert.equal(model.paths.length, 3); assert.equal(model.segments[0].length, 2);
  assert.equal(model.paths[0].match(/L/g).length, 1); assert.equal(model.segments[0][1].value, 0);
  assert.equal(h.seriesHealth({ points, latestAt: 3600000 }, 0, 3700000, 30000).stale, true);
  assert.equal(h.seriesHealth({ points, latestAt: 3600000 }, 0, 3610000, 30000).gaps, true);
  assert.equal(h.seriesHealth({ points: [] }, 0, 3610000, 30000).missing, true);
  assert.equal(h.nearestPoint(points, 2000000, 60000), null);
  assert.equal(h.nearestPoint(points, 30001, 60000).value, 0);
});

test('Values use compact binary units, explicit percentages, counts and durations; unlike units never share a chart', () => {
  assert.equal(h.formatValue(1048576, 'bytes'), '1 MiB');
  assert.equal(h.formatValue(1024, 'bytes/s'), '1 KiB/s');
  assert.equal(h.formatValue(86400, 'seconds'), '1 d');
  assert.equal(h.formatValue(12000, 'ms'), '12 s');
  assert.equal(h.formatValue(7, 'count'), '7');
  assert.equal(h.unitKind(CATALOG.items[0]), 'percent'); assert.equal(h.unitKind(CATALOG.items[1]), 'bytes');
  assert.match(source, /for \(const id of state\.selected\)/); assert.match(source, /series\.metricId === id/);
});

test('Range presets use the full saved retention by default; custom filters are encoded exactly, not widened', () => {
  assert.equal(h.rangeFor('full', 7, NOW).to - h.rangeFor('full', 7, NOW).from, 7 * DAY);
  assert.equal(h.rangeFor('full', 365, NOW).from, NOW - 365 * DAY);
  assert.equal(h.rangeFor('1h', 7, NOW).from, NOW - 3600000); assert.equal(h.rangeFor('24h', 7, NOW).from, NOW - DAY);
  assert.equal(h.rangeFor('7d', 1, NOW).from, NOW - 7 * DAY);
  assert.equal(h.rangeFor('custom', 7, NOW, { from: 'bad', to: '2026-01-01' }), null);
  assert.equal(h.rangeFor('custom', 7, NOW, { from: '2026-01-02', to: '2026-01-01' }), null);
  const url = new URL(h.buildQuery({ worldId: 'test-a', agentId: 'helper', conversationId: 'c_x&worldId=other' }, [DEFAULTS[0], 'agent.loops.active'], { from: 0, to: NOW }), 'https://hub.example.test');
  assert.deepEqual(url.searchParams.getAll('metricId'), [DEFAULTS[0], 'agent.loops.active']);
  assert.deepEqual(url.searchParams.getAll('worldId'), ['test-a']); assert.equal(url.searchParams.get('conversationId'), 'c_x&worldId=other');
  assert.equal(h.buildQuery({}, [], { from: 0, to: NOW }), null);
  assert.equal(new URL(h.buildQuery({}, [DEFAULTS[0]], { from: 0, to: NOW }), 'https://hub.example.test').searchParams.has('worldId'), false);
});

test('Unknown world, agent and conversation filters show a clear error; truncated dimension lists do not discard IDs', () => {
  const dimensions = { agents: [{ worldId: 'test-a', agentId: 'helper' }], conversations: [{ worldId: 'test-a', agentId: 'helper', conversationId: 'c_alpha' }] };
  for (const filters of [{ worldId: 'missing' }, { worldId: 'test-b', agentId: 'helper' }, { worldId: 'test-a', conversationId: 'missing' }]) assert.match(h.validateFilters(filters, WORLDS, dimensions), /Filters have not been widened/);
  assert.equal(h.validateFilters({ worldId: 'test-a', agentId: 'helper', conversationId: 'c_alpha' }, WORLDS, dimensions), '');
  assert.equal(h.validateFilters({ worldId: 'test-a', agentId: 'not-listed' }, WORLDS, { ...dimensions, truncated: true }), '');
  assert.equal(h.parseRoute('#other'), null); assert.equal(h.parseRoute('#metrics?worldId=test-a').worldId, 'test-a');
});

test('The parent hook is exact, lazy, storage independent and coalesces many world tiles into one 30-second read', async () => {
  const t = harness(); assert.equal(t.calls.length, 0);
  for (const key of ['attach', 'tile', 'update', 'clear', 'open', 'close']) assert.equal(typeof t.metrics[key], 'function');
  const a = t.metrics.tile(WORLDS[0]); const b = t.metrics.tile(WORLDS[1]);
  assert.equal(t.metrics.tile(WORLDS[0]), a); assert.equal(a.querySelectorAll('.metrics-spark-row').length, 3);
  await Promise.all([t.metrics.update(), t.metrics.update(), t.metrics.update()]);
  assert.equal(t.calls.filter((call) => call.url === '/api/metrics/catalog').length, 1);
  assert.equal(t.calls.filter((call) => call.url === '/api/metrics/settings').length, 1);
  assert.equal(t.calls.filter((call) => call.url === '/api/metrics/tiles').length, 1);
  assert.ok(a.querySelectorAll('.metrics-spark-value').every((node) => /^0(?:\s|$)/.test(node.textContent)));
  assert.match(b.textContent, /World offline/);
  assert.ok(t.calls.every((call) => call.method === 'GET' && call.options.signal instanceof AbortSignal));
  for (let index = 0; index < 5; index += 1) { t.advance(5000); await t.metrics.update(); }
  assert.equal(t.calls.filter((call) => call.url === '/api/metrics/tiles').length, 1);
  t.advance(5000); await t.metrics.update();
  assert.equal(t.calls.filter((call) => call.url === '/api/metrics/tiles').length, 2);
});

test('Opening history requests the full retention, separates metric units and worlds, labels axes and offers numeric fallback', async () => {
  const t = harness({ retentionDays: 21 }); await t.metrics.update(); t.metrics.open(); await t.flush();
  assert.equal(t.id('metrics-section').hidden, false); assert.equal(t.id('metrics-button').getAttribute('aria-expanded'), 'true');
  const call = t.calls.find((call) => call.url.startsWith('/api/metrics/query?')); const query = new URL(call.url, 'https://hub.example.test');
  assert.equal(Number(query.searchParams.get('to')) - Number(query.searchParams.get('from')), 21 * DAY);
  assert.equal(t.id('metrics-charts').children.length, 3);
  const charts = t.id('metrics-charts').children;
  assert.match(charts[0].textContent, /Gateway CPU/); assert.doesNotMatch(charts[0].querySelector('.metrics-legend').textContent, /MiB/);
  assert.equal(charts[0].querySelectorAll('.metrics-legend-row').length, 2);
  assert.match(charts[0].textContent, /Test A/); assert.match(charts[0].textContent, /Test B/);
  assert.match(charts[0].textContent, /Latest.*Average/); assert.match(charts[0].textContent, /gaps/);
  const svg = charts[0].querySelector('.metrics-plot'); assert.equal(svg.getAttribute('tabindex'), '0'); assert.equal(svg.getAttribute('role'), 'img');
  assert.ok(svg.querySelector('title')); assert.ok(svg.querySelectorAll('text').length >= 7);
  const fallback = charts[0].querySelector('.metrics-data-disclosure'); fallback.open = true; await fallback.fire('toggle');
  assert.ok(fallback.querySelector('table')); assert.match(fallback.textContent, /Missing is not zero/);
  const buckets = fallback.querySelector('.metrics-bucket-disclosure'); buckets.open = true; await buckets.fire('toggle');
  assert.match(buckets.textContent, /Only measured buckets/); assert.ok(buckets.querySelector('table'));
  const requestCount = t.calls.length; t.metrics.close(); assert.equal(t.id('metrics-section').hidden, true); await t.metrics.update();
  assert.equal(t.calls.length, requestCount);
});

test('SVG tooltips work with keyboard, numeric zero and missing-time inspection', async () => {
  const t = harness(); await t.metrics.update(); t.metrics.open('test-a'); await t.flush();
  const svg = t.id('metrics-charts').querySelector('.metrics-plot'); const tooltip = t.id('metrics-charts').querySelector('.metrics-tooltip');
  svg.focus(); assert.equal(tooltip.hidden, false);
  await svg.fire('keydown', { key: 'Home' }); assert.match(tooltip.textContent, /0 %/);
  await svg.fire('pointermove', { clientX: 350 }); assert.match(tooltip.textContent, /No sample/);
  await svg.fire('keydown', { key: 'Escape' }); assert.equal(tooltip.hidden, true);
});

test('Unknown routes stay explicit and never fall back to all worlds', async () => {
  const t = harness({ hash: '#metrics?worldId=removed' }); await t.metrics.update(); await t.flush();
  assert.equal(t.id('metrics-section').hidden, false); assert.match(t.id('metrics-error').textContent, /Unknown or removed world/);
  assert.equal(t.id('metrics-world').value, 'removed');
  assert.equal(t.calls.filter((call) => call.url.startsWith('/api/metrics/query?')).length, 0);
});

test('Agent filters keep world metrics unavailable rather than showing a misleading world aggregate', async () => {
  const t = harness(); await t.metrics.update(); t.metrics.open('test-a'); await t.flush();
  t.id('metrics-agent').value = 'helper'; await t.id('metrics-agent').fire('change'); await t.flush();
  assert.match(t.id('metrics-summary').textContent, /World metrics do not apply/);
  assert.match(t.id('metrics-charts').textContent, /Clear agent and conversation filters/);
  t.id('metrics-add').value = 'agent.loops.completed'; await t.id('metrics-add').fire('change'); await t.id('metrics-add-button').fire('click'); await t.flush();
  assert.equal(t.id('metrics-charts').children.length, 4); assert.match(t.id('metrics-charts').textContent, /c_alpha/);
  assert.match(t.id('metrics-charts').textContent, /Avg \/ bucket/);
  assert.ok(t.calls.filter((call) => call.url.startsWith('/api/metrics/query?')).every((call) => !call.url.includes('worldId=all')));
});

test('History chart selection has explicit add/remove controls and remains between one and six metrics', async () => {
  const t = harness(); await t.metrics.update(); t.metrics.open(); await t.flush();
  for (const id of ['gateway.threads.count', 'gateway.uptime.seconds', 'agent.loops.active']) {
    t.id('metrics-add').value = id; await t.id('metrics-add').fire('change'); await t.id('metrics-add-button').fire('click'); await t.flush();
  }
  assert.equal(t.id('metrics-selected').children.length, 6); assert.equal(t.id('metrics-add').disabled, true);
  for (let index = 0; index < 6; index += 1) { await t.id('metrics-selected').querySelector('.metrics-chip-remove').fire('click'); await t.flush(); }
  assert.equal(t.id('metrics-selected').children.length, 1); assert.equal(t.id('metrics-selected').querySelector('.metrics-chip-remove').disabled, true);
  assert.equal(t.calls.some((call) => call.method !== 'GET'), false);
});

test('Card selection reads shared API settings and writes once only on explicit Save, never on a checkbox change', async () => {
  const t = harness(); const tile = t.metrics.tile(WORLDS[0]); await t.metrics.update();
  await tile.querySelector('.metrics-configure').fire('click'); await t.flush();
  assert.equal(t.id('metrics-config-dialog').open, true); assert.equal(t.id('metrics-config-choices').querySelectorAll('input').filter((input) => input.checked).length, 3);
  const inputs = t.id('metrics-config-choices').querySelectorAll('input');
  inputs[0].checked = false; await inputs[0].fire('change');
  assert.equal(t.calls.some((call) => call.method === 'PUT'), false);
  await t.id('metrics-config-form').fire('submit');
  const writes = t.calls.filter((call) => call.method === 'PUT'); assert.equal(writes.length, 1);
  assert.equal(writes[0].url, '/api/worlds/test-a/metrics'); assert.deepEqual(plain(writes[0].body.metrics), DEFAULTS.slice(1));
  assert.match(t.id('metrics-config-feedback').textContent, /Saved for everyone/);
  assert.equal(tile.querySelectorAll('.metrics-spark-row').length, 2);
  await t.metrics.update(); assert.equal(t.calls.filter((call) => call.method === 'PUT').length, 1);
});

test('Unconfirmed shared card saves cannot retry on refresh or a second form submission', async () => {
  const t = harness({ respond(url, method, body, normal) { if (method === 'PUT') throw new Error('Unconfirmed response'); return normal(url, method, body); } });
  const tile = t.metrics.tile(WORLDS[0]); await t.metrics.update(); await tile.querySelector('.metrics-configure').fire('click'); await t.flush();
  await t.id('metrics-config-form').fire('submit'); await t.id('metrics-config-form').fire('submit'); t.advance(30000); await t.metrics.update();
  assert.equal(t.calls.filter((call) => call.method === 'PUT').length, 1); assert.equal(t.id('metrics-config-save').disabled, true);
  assert.match(t.id('metrics-config-error').textContent, /not retried/);
});

test('Retention is validated, saved only explicitly, and re-read after an uncertain outcome before another mutation', async () => {
  let fail = true;
  const t = harness({ respond(url, method, body, normal) { if (method === 'PUT' && fail) { fail = false; throw new Error('Unknown outcome'); } return normal(url, method, body); } });
  await t.metrics.update(); t.metrics.open(); await t.flush();
  t.id('metrics-retention').value = '366'; await t.id('metrics-retention-form').fire('submit'); assert.equal(t.calls.some((call) => call.method === 'PUT'), false);
  t.id('metrics-retention').value = '14'; await t.id('metrics-retention-form').fire('submit');
  await t.id('metrics-retention-form').fire('submit'); t.advance(30000); await t.metrics.update();
  assert.equal(t.calls.filter((call) => call.method === 'PUT').length, 1); assert.equal(t.id('metrics-retention-save').disabled, true);
  assert.match(t.id('metrics-retention-feedback').textContent, /not retried/);
  await t.id('metrics-retention-check').fire('click'); assert.equal(t.id('metrics-retention-save').disabled, false);
  t.id('metrics-retention').value = '14'; await t.id('metrics-retention-form').fire('submit');
  assert.deepEqual(plain(t.calls.filter((call) => call.method === 'PUT').at(-1).body), { retentionDays: 14 });
  assert.match(t.id('metrics-retention-feedback').textContent, /Saved: 14 days/);
  await t.metrics.update(); assert.equal(t.calls.filter((call) => call.method === 'PUT').length, 2);
});

test('Hidden or inactive views make no requests; active refresh aborts on hidden and restarts only after visibility returns', async () => {
  const late = deferred(); let hold = false;
  const t = harness({ respond(url, method, body, normal) { if (hold && url === '/api/metrics/tiles') return late.promise; return normal(url, method, body); } });
  await t.metrics.update(); const count = t.calls.length;
  t.document.visibilityState = 'hidden'; await t.event('visibilitychange'); t.advance(30000); await t.metrics.update(); assert.equal(t.calls.length, count);
  hold = true; t.document.visibilityState = 'visible'; await t.event('visibilitychange');
  const call = t.calls.at(-1); assert.equal(call.url, '/api/metrics/tiles'); assert.equal(call.options.signal.aborted, false);
  t.document.visibilityState = 'hidden'; await t.event('visibilitychange'); assert.equal(call.options.signal.aborted, true);
  late.resolve(tileData()); await t.flush();
  t.setActive(false); await t.metrics.update(); assert.equal(t.id('metrics-button').hidden, true);
});

test('Clear aborts pending history and wipes tiles, filters, charts, dialog names and every private async response', async () => {
  const late = deferred(); let hold = false;
  const t = harness({ respond(url, method, body, normal) { if (hold && url.startsWith('/api/metrics/query?')) return late.promise; return normal(url, method, body); } });
  const tile = t.metrics.tile(WORLDS[0]); await t.metrics.update(); t.metrics.open('test-a'); await t.flush();
  hold = true; const pending = t.id('metrics-refresh').fire('click'); await t.flush();
  const inFlight = t.calls.filter((call) => call.url.startsWith('/api/metrics/query?')).at(-1);
  t.metrics.clear(); assert.equal(inFlight.options.signal.aborted, true);
  assert.equal(tile.textContent, ''); assert.equal(tile.getAttribute('aria-label'), null);
  for (const id of ['metrics-charts', 'metrics-world', 'metrics-agent', 'metrics-conversation', 'metrics-config-world', 'metrics-storage', 'metrics-summary']) assert.equal(t.id(id).textContent, '', id);
  assert.equal(t.id('metrics-section').hidden, true); assert.equal(t.id('metrics-config-dialog').open, false); assert.equal(t.id('metrics-button').hidden, true);
  const query = new URL(inFlight.url, 'https://hub.example.test'); late.resolve(queryData({ from: Number(query.searchParams.get('from')), to: Number(query.searchParams.get('to')) }));
  await pending; await t.flush(); assert.equal(t.id('metrics-charts').children.length, 0); assert.equal(t.id('metrics-summary').textContent, '');
});

test('Late metadata and mutation replies cannot restore saved state after clear', async () => {
  const lateMetadata = deferred();
  const t = harness({ respond(url, method, body, normal) { if (url === '/api/metrics/catalog') return lateMetadata.promise; return normal(url, method, body); } });
  const reading = t.metrics.update(); await t.flush(); t.metrics.clear(); lateMetadata.resolve(CATALOG); await reading;
  assert.equal(t.id('metrics-button').hidden, true); assert.equal(t.id('metrics-storage').textContent, '');
  const lateMutation = deferred();
  const m = harness({ respond(url, method, body, normal) { if (method === 'PUT') return lateMutation.promise; return normal(url, method, body); } });
  await m.metrics.update(); m.metrics.open(); await m.flush(); m.id('metrics-retention').value = '14';
  const saving = m.id('metrics-retention-form').fire('submit'); await m.flush(); m.metrics.clear(); lateMutation.resolve({ ...SETTINGS, retentionDays: 14 }); await saving;
  assert.equal(m.id('metrics-retention-feedback').textContent, ''); assert.equal(m.id('metrics-retention').value, '');
});

test('A parent 403 access handler remains authoritative and cannot be bypassed by metrics retries', async () => {
  let t;
  t = harness({ respond(url, method, body, normal) {
    if (url === '/api/metrics/tiles') { t.setActive(false); t.metrics.clear(); throw Object.assign(new Error('Access denied'), { status: 403, kind: 'access' }); }
    return normal(url, method, body);
  } });
  const tile = t.metrics.tile(WORLDS[0]); await t.metrics.update(); const count = t.calls.length;
  t.advance(60000); await t.metrics.update(); await t.metrics.update();
  assert.equal(t.calls.length, count); assert.equal(tile.textContent, ''); assert.equal(t.id('metrics-button').hidden, true);
});

test('Malformed APIs show a readable unavailable state without blocking world cards or creating fake values', async () => {
  const t = harness({ respond(url, method, body, normal) { if (url.startsWith('/api/metrics/tiles')) return { worlds: [] }; return normal(url, method, body); } });
  const tile = t.metrics.tile(WORLDS[0]); await t.metrics.update(); assert.match(tile.textContent, /Metrics could not be loaded/);
  assert.ok(tile.querySelectorAll('.metrics-spark-value').every((node) => node.textContent === '—'));
  t.metrics.open(); await t.flush(); assert.equal(t.id('metrics-charts').children.length, 3);
});

test('HTML hooks, SVG labels and mobile styles keep metrics separate from dashboard access and preserve theme storage', () => {
  for (const id of ['metrics-button', 'metrics-section', 'metrics-title', 'metrics-world', 'metrics-agent', 'metrics-conversation', 'metrics-from', 'metrics-to', 'metrics-config-dialog', 'metrics-retention-save']) assert.ok(html.includes(`id="${id}"`), id);
  assert.ok(html.indexOf('/static/theme.js') < html.indexOf('/static/metrics.js')); assert.ok(html.indexOf('/static/metrics.js') < html.indexOf('/static/app.js'));
  assert.match(html, /id="metrics-section"[^>]*hidden/); assert.match(html, /id="metrics-button"[^>]*hidden/);
  assert.match(html, /data-metrics-range="full" aria-pressed="true"/); assert.match(html, /min="1" max="365"/);
  assert.doesNotMatch(source, /localStorage|sessionStorage|indexedDB|\.fetch\s*\(|setInterval|\.(?:innerHTML|outerHTML)\s*=|insertAdjacentHTML|document\.write|\beval\s*\(/);
  assert.doesNotMatch(css, /#[\da-f]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\s*\(/i);
  assert.doesNotMatch(css, /(?:color|background(?:-color)?|border-color):\s*(?:white|black|gray|grey|red|blue|transparent)\b/i);
  const tokens = new Set([...fs.readFileSync(path.join(staticDir, 'styles.css'), 'utf8').matchAll(/(--[\w-]+)\s*:/g)].map((match) => match[1]));
  for (const match of css.matchAll(/var\((--[\w-]+)\)/g)) assert.ok(tokens.has(match[1]), `Unknown theme token ${match[1]}`);
  assert.match(css, /:focus-visible/); assert.match(css, /max-width:\s*560px/); assert.match(css, /minmax\(0, 1fr\)/);
  assert.doesNotMatch(html, /(?:src|href)="https?:\/\//i);
});


test('Rapid world changes abort previous queries and a late old response cannot overwrite the latest world', async () => {
  const late = deferred(); let hold = true; let oldQuery;
  const t = harness({ respond(url, method, body, normal) {
    if (hold && url.startsWith('/api/metrics/query?') && new URL(url, 'https://hub.example.test').searchParams.get('worldId') === 'test-a') { oldQuery = url; return late.promise; }
    return normal(url, method, body);
  } });
  await t.metrics.update(); t.metrics.open('test-a'); await t.flush();
  const old = t.calls.find((call) => call.url === oldQuery);
  t.id('metrics-world').value = 'test-b'; await t.id('metrics-world').fire('change'); await t.flush();
  assert.equal(old.options.signal.aborted, true); assert.equal(t.id('metrics-world').value, 'test-b');
  assert.match(t.id('metrics-charts').textContent, /Test B/); assert.doesNotMatch(t.id('metrics-charts').textContent, /Test A/);
  hold = false; late.resolve(t.normalResponse(oldQuery, 'GET')); await t.flush();
  assert.match(t.id('metrics-charts').textContent, /Test B/); assert.doesNotMatch(t.id('metrics-charts').textContent, /Test A/);
});

test('Removing a world immediately clears its old tile and history; the filter stays explicitly invalid', async () => {
  const t = harness(); const tile = t.metrics.tile(WORLDS[0]); await t.metrics.update(); t.metrics.open('test-a'); await t.flush();
  assert.match(t.id('metrics-charts').textContent, /Test A/);
  t.setWorlds([WORLDS[1]]); const updating = t.metrics.update();
  assert.equal(tile.textContent, ''); assert.equal(tile.getAttribute('aria-label'), null); assert.equal(t.id('metrics-charts').textContent, '');
  await updating; await t.flush(); assert.match(t.id('metrics-error').textContent, /Unknown or removed world/);
  assert.equal(t.id('metrics-world').value, 'test-a'); assert.equal(t.id('metrics-charts').textContent, '');
});

test('Editing dates cancels old history, waits for Apply, and cannot show old charts below new date fields', async () => {
  const late = deferred(); let hold = false; let oldUrl;
  const t = harness({ respond(url, method, body, normal) {
    if (hold && url.startsWith('/api/metrics/query?')) { oldUrl = url; return late.promise; }
    return normal(url, method, body);
  } });
  await t.metrics.update(); t.metrics.open('test-a'); await t.flush();
  hold = true; await t.id('metrics-refresh').fire('click'); await t.flush();
  const old = t.calls.filter((call) => call.url === oldUrl).at(-1);
  t.id('metrics-from').value = '2026-11-16T00:00:00'; t.id('metrics-to').value = '2026-11-17T00:00:00';
  await t.id('metrics-from').fire('change'); assert.equal(old.options.signal.aborted, true);
  assert.equal(t.id('metrics-charts').children.length, 0); assert.match(t.id('metrics-status').textContent, /Apply dates/);
  hold = false; late.resolve(t.normalResponse(oldUrl, 'GET')); await t.flush(); assert.equal(t.id('metrics-charts').children.length, 0);
  const count = t.calls.filter((call) => call.url.startsWith('/api/metrics/query?')).length;
  t.advance(30000); await t.metrics.update(); assert.equal(t.calls.filter((call) => call.url.startsWith('/api/metrics/query?')).length, count);
  await t.id('metrics-filter-form').fire('submit'); await t.flush();
  const query = new URL(t.calls.filter((call) => call.url.startsWith('/api/metrics/query?')).at(-1).url, 'https://hub.example.test');
  assert.equal(Number(query.searchParams.get('from')), Date.parse('2026-11-16T00:00:00'));
  assert.equal(Number(query.searchParams.get('to')), Date.parse('2026-11-17T00:00:00'));
  assert.equal(t.id('metrics-charts').children.length, 3);
});

test('Explicit presets replace a custom range and produce the exact selected interval', async () => {
  const t = harness(); await t.metrics.update(); t.metrics.open(); await t.flush();
  for (const [range, span] of [['1h', 3600000], ['24h', DAY], ['7d', 7 * DAY], ['full', 7 * DAY]]) {
    await t.id('metrics-presets').children.find((node) => node.getAttribute('data-metrics-range') === range).fire('click'); await t.flush();
    const query = new URL(t.calls.filter((call) => call.url.startsWith('/api/metrics/query?')).at(-1).url, 'https://hub.example.test');
    assert.equal(Number(query.searchParams.get('to')) - Number(query.searchParams.get('from')), span);
    assert.equal(t.id('metrics-presets').children.filter((node) => node.getAttribute('aria-pressed') === 'true').length, 1);
  }
});

test('Invalid shared card payloads never silently replace the saved selection with defaults', async () => {
  const t = harness({ respond(url, method, body, normal) { return url.startsWith('/api/metrics/tiles?') ? { from: NOW - 3600000, to: NOW, worlds: { 'test-a': { metrics: ['unknown'], series: [] } } } : normal(url, method, body); } });
  const tile = t.metrics.tile(WORLDS[0]); await t.metrics.update(); await tile.querySelector('.metrics-configure').fire('click'); await t.flush();
  assert.equal(t.id('metrics-config-save').disabled, true); assert.equal(t.id('metrics-config-choices').children.length, 0);
  assert.match(t.id('metrics-config-error').textContent, /could not be read/);
});

test('Closing configuration discards a late read, while an in-flight save blocks a second save until confirmed', async () => {
  const lateRead = deferred(); let readUrl;
  const t = harness({ respond(url, method, body, normal) { if (url.startsWith('/api/metrics/tiles?')) { readUrl = url; return lateRead.promise; } return normal(url, method, body); } });
  const tile = t.metrics.tile(WORLDS[0]); await t.metrics.update(); await tile.querySelector('.metrics-configure').fire('click'); await t.flush();
  const read = t.calls.find((call) => call.url === readUrl); await t.id('metrics-config-close').fire('click');
  assert.equal(read.options.signal.aborted, true); lateRead.resolve(t.normalResponse(readUrl, 'GET')); await t.flush();
  assert.equal(t.id('metrics-config-dialog').open, false); assert.equal(t.id('metrics-config-choices').textContent, '');
  const lateSave = deferred();
  const m = harness({ respond(url, method, body, normal) { return method === 'PUT' ? lateSave.promise : normal(url, method, body); } });
  const card = m.metrics.tile(WORLDS[0]); await m.metrics.update(); await card.querySelector('.metrics-configure').fire('click'); await m.flush();
  const saving = m.id('metrics-config-form').fire('submit'); await m.flush();
  await m.id('metrics-config-close').fire('click'); await m.id('metrics-config-form').fire('submit');
  assert.equal(m.id('metrics-config-dialog').open, true); assert.equal(m.calls.filter((call) => call.method === 'PUT').length, 1);
  lateSave.resolve({ worldId: 'test-a', metrics: DEFAULTS }); await saving; assert.equal(m.id('metrics-config-close').disabled, false);
});

test('A bounded tile response is visibly limited instead of claiming omitted worlds have no samples', async () => {
  const t = harness({ respond(url, method, body, normal) { if (url === '/api/metrics/tiles') return { ...tileData(['test-a']), truncated: true }; return normal(url, method, body); } });
  const tile = t.metrics.tile(WORLDS[1]); await t.metrics.update();
  assert.match(tile.textContent, /Tile limit reached/); assert.match(tile.textContent, /Open history for this world/);
  assert.doesNotMatch(tile.textContent, /No samples in this range/);
});

test('Positive near-zero values never format as a real zero, sum buckets can exceed raw limits, and invalid percentages stay missing', () => {
  assert.notEqual(h.formatValue(0.000000001, '%'), '0 %'); assert.match(h.formatValue(0.000000001, '%'), /e-/);
  const tiny = h.plotModel([point(1, Number.MIN_VALUE)], { from: 0, to: 2 });
  assert.ok(tiny.paths.every((value) => !/NaN|Infinity/.test(value)));
  const sumData = queryData({ metrics: ['agent.loops.completed'] }); sumData.series[0].points = [point(sumData.from, 2e18), point(sumData.from + 1, 2e18)];
  const sum = h.normalizeQuery(sumData, catalog, ['test-a', 'test-b']).series[0];
  assert.equal(sum.points.length, 2); assert.equal(h.summarize(sum.points, 'sum').total, 4e18); assert.notEqual(h.formatValue(4e18, 'count'), '—');
  const percentData = queryData({ metrics: [DEFAULTS[0]] }); percentData.series[0].points = [point(percentData.from, 0), point(percentData.from + 1, 101), point(percentData.from + 2, 1e19)];
  const percent = h.normalizeQuery(percentData, catalog, ['test-a', 'test-b']); assert.deepEqual(plain(percent.series[0].points), [point(percentData.from, 0)]); assert.ok(percent.omitted > 0);
});

test('Overlong route IDs remain invalid rather than becoming a valid shorter world or agent', () => {
  const worldId = 'a'.repeat(64); const route = h.parseRoute(`#metrics?worldId=${worldId}suffix&agentId=${'b'.repeat(129)}`);
  assert.notEqual(route.worldId, worldId); assert.match(h.validateFilters(route, [{ id: worldId }], null), /Invalid worldId/);
});

test('Unchanged tile nodes keep their keyboard controls between parent five-second renders', async () => {
  const t = harness(); const tile = t.metrics.tile(WORLDS[0]); await t.metrics.update();
  const control = tile.querySelector('.metrics-configure'); control.focus(); t.advance(5000); t.metrics.tile(WORLDS[0]); await t.metrics.update();
  assert.equal(tile.querySelector('.metrics-configure'), control); assert.equal(t.document.activeElement, control);
});


test('Automatic history refresh preserves focused chart/table inspection, but explicit Refresh still works', async () => {
  const t = harness(); await t.metrics.update(); t.metrics.open(); await t.flush();
  const svg = t.id('metrics-charts').querySelector('.metrics-plot'); svg.focus();
  const count = t.calls.filter((call) => call.url.startsWith('/api/metrics/query?')).length;
  t.advance(30000); await t.metrics.update();
  assert.equal(t.calls.filter((call) => call.url.startsWith('/api/metrics/query?')).length, count);
  assert.equal(t.id('metrics-charts').querySelector('.metrics-plot'), svg); assert.match(t.id('metrics-status').textContent, /paused while you inspect/);
  await t.id('metrics-refresh').fire('click'); await t.flush();
  assert.equal(t.calls.filter((call) => call.url.startsWith('/api/metrics/query?')).length, count + 1);
});

test('Unsaved retention survives automatic reads, and saved API changes control subsequent Full history ranges', async () => {
  let saved = 7;
  const t = harness({ respond(url, method, body, normal) { return url === '/api/metrics/settings' ? { ...SETTINGS, retentionDays: saved } : normal(url, method, body); } });
  await t.metrics.update(); t.metrics.open(); await t.flush();
  t.id('metrics-retention').value = '21'; await t.id('metrics-retention').fire('input');
  saved = 14; t.advance(30000); await t.metrics.update();
  assert.equal(t.id('metrics-retention').value, '21'); assert.match(t.id('metrics-retention-feedback').textContent, /Unsaved change/);
  const query = new URL(t.calls.filter((call) => call.url.startsWith('/api/metrics/query?')).at(-1).url, 'https://hub.example.test');
  assert.equal(Number(query.searchParams.get('to')) - Number(query.searchParams.get('from')), 14 * DAY);
  assert.equal(t.calls.some((call) => call.method === 'PUT'), false);
  await t.id('metrics-retention-check').fire('click'); assert.equal(t.id('metrics-retention').value, '14');
});

test('An old settings read cannot overwrite a confirmed retention save', async () => {
  const late = deferred(); let hold = false;
  const t = harness({ respond(url, method, body, normal) { return hold && url === '/api/metrics/settings' && method === 'GET' ? late.promise : normal(url, method, body); } });
  await t.metrics.update(); t.metrics.open(); await t.flush(); hold = true;
  const reading = t.id('metrics-retention-check').fire('click'); await t.flush();
  t.id('metrics-retention').value = '14'; await t.id('metrics-retention-form').fire('submit');
  late.resolve({ ...SETTINGS, retentionDays: 7 }); await reading; await t.flush();
  assert.equal(t.id('metrics-retention').value, '14'); assert.match(t.id('metrics-retention-feedback').textContent, /Saved: 14 days/);
});


test('Mobile 320/390 and desktop SVG models keep axis text at a readable scale without invalid coordinates', async () => {
  for (const width of [320, 390, 1440]) {
    const t = harness(); t.window.innerWidth = width; await t.metrics.update(); t.metrics.open(); await t.flush();
    const svgs = t.id('metrics-charts').querySelectorAll('.metrics-plot'); assert.equal(svgs.length, 3);
    for (const svg of svgs) {
      const viewBox = svg.getAttribute('viewBox').split(' ').map(Number);
      assert.ok(viewBox[2] >= 230); assert.ok(viewBox[2] < width); assert.equal(viewBox[3], 246);
      assert.ok(svg.querySelectorAll('.metrics-axis-label').length >= 6);
      assert.ok(svg.querySelectorAll('path').every((node) => !/NaN|Infinity|undefined/.test(node.getAttribute('d'))));
    }
  }
  assert.match(css, /\.metrics-axis-label\s*\{[^}]*font:\s*11px/);
});

test('External names and descriptions remain text in tiles, charts and shared configuration', async () => {
  const hostile = '<img src=x onerror=alert(1)>';
  const worlds = structuredClone(WORLDS); worlds[0].name = hostile;
  const t = harness({ initialWorlds: worlds, respond(url, method, body, normal) {
    return url === '/api/metrics/catalog' ? { ...CATALOG, items: CATALOG.items.map((row) => ({ ...row, description: hostile })) } : normal(url, method, body);
  } });
  const tile = t.metrics.tile(worlds[0]); await t.metrics.update(); t.metrics.open('test-a'); await t.flush();
  assert.ok(t.id('metrics-charts').textContent.includes(hostile)); assert.equal(t.id('metrics-charts').querySelectorAll('img').length, 0);
  assert.ok(tile.getAttribute('aria-label').includes(hostile));
  await tile.querySelector('.metrics-configure').fire('click'); await t.flush();
  assert.ok(t.id('metrics-config-world').textContent.includes(hostile)); assert.equal(t.id('metrics-config-choices').querySelectorAll('img').length, 0);
});

test('All metric hooks have matching unique HTML IDs, and new files have no trailing whitespace', () => {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(new Set(ids).size, ids.length, 'HTML IDs must be unique.');
  for (const match of source.matchAll(/byId\('(metrics-[^']+)'\)/g)) assert.ok(ids.includes(match[1]), `Missing DOM hook ${match[1]}`);
  for (const [name, content] of [['metrics.js', source], ['metrics.css', css], ['metrics.test.cjs', fs.readFileSync(__filename, 'utf8')]]) {
    assert.doesNotMatch(content, /[\t ]+(?=\r?$)/m, `${name}: trailing whitespace`); assert.ok(content.endsWith('\n'), `${name}: final newline`);
  }
});


test('Series cadence keeps a healthy five-minute world distinct from a stale thirty-second world', () => {
  const t = harness(); const h = t.metrics.helpers;
  const from = NOW - 900000; const to = NOW;
  const points = [point(NOW - 720000, 0), point(NOW - 420000, 1), point(NOW - 120000, 0)];
  const slow = { points, latestAt: NOW - 120000, sampleIntervalSeconds: 300 };
  const fast = { ...slow, sampleIntervalSeconds: 30 };
  assert.equal(h.seriesHealth(slow, from, to, 60000).stale, false);
  assert.equal(h.seriesHealth(slow, from, to, 60000).segments.length, 1);
  assert.equal(h.seriesHealth(fast, from, to, 60000).stale, true);
  const payload = queryData(); payload.series[0].sampleIntervalSeconds = 300;
  assert.equal(h.normalizeQuery(payload, CATALOG, ['test-a','test-b']).series[0].sampleIntervalSeconds, 300);
});
