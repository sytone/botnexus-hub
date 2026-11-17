(function () {
  'use strict';

  const DAY = 86400000;
  const REFRESH_MS = 30000;
  const MAX_SERIES = 64;
  const MAX_POINTS = 720;
  const DEFAULT_METRICS = ['gateway.cpu.percent', 'gateway.memory.rss_bytes', 'gateway.loops.active'];
  const record = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
  const text = (value, fallback = '') => typeof value === 'string' ? value : fallback;
  const finiteValue = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1e18;
  // Query sums can exceed one raw sample's wire limit (1e18).
  const plotValue = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1e30;
  const timestamp = (value) => Number.isSafeInteger(value) && value >= 0 && value <= 8640000000000000;
  const identifier = (value, max = 256) => typeof value === 'string' && value.length > 0 && value.length <= max && !/[\u0000-\u001f\u007f]/.test(value);
  const metricId = (value) => identifier(value, 128) && /^[a-z][a-z0-9_.]*$/.test(value);
  const bounded = (value, fallback, min, max) => Number.isFinite(value) && value >= min && value <= max ? value : fallback;
  const unique = (values) => [...new Set(values)];

  function normalizeCatalog(payload) {
    if (!record(payload) || !Array.isArray(payload.items)) return null;
    const items = [];
    const seen = new Set();
    for (const item of payload.items.slice(0, 100)) {
      if (!record(item) || !metricId(item.id) || !identifier(item.name, 160) || !identifier(item.unit, 40) ||
          !['world', 'agent'].includes(item.scope) || !['mean', 'sum'].includes(item.aggregation) || seen.has(item.id)) continue;
      seen.add(item.id);
      items.push({ id: item.id, name: item.name, unit: item.unit, scope: item.scope, aggregation: item.aggregation, description: text(item.description).slice(0, 1200) });
    }
    if (!items.length) return null;
    const supplied = Array.isArray(payload.defaultMetrics) ? payload.defaultMetrics : DEFAULT_METRICS;
    let defaults = unique(supplied.filter((id) => items.some((item) => item.id === id && item.scope === 'world'))).slice(0, 3);
    if (!defaults.length) defaults = items.filter((item) => item.scope === 'world').slice(0, 3).map((item) => item.id);
    return defaults.length ? { items, defaultMetrics: defaults } : null;
  }

  function normalizeSettings(payload) {
    if (!record(payload) || !Number.isInteger(payload.retentionDays) || payload.retentionDays < 1 || payload.retentionDays > 365) return null;
    const counter = (value) => Number.isSafeInteger(value) && value >= 0 ? value : 0;
    return {
      retentionDays: payload.retentionDays,
      sampleIntervalSeconds: bounded(payload.sampleIntervalSeconds, 30, 15, 3600),
      rowCount: counter(payload.rowCount), databaseBytes: counter(payload.databaseBytes),
      maxRows: counter(payload.maxRows), rejectedBatches: counter(payload.rejectedBatches),
      oldestAt: timestamp(payload.oldestAt) ? payload.oldestAt : null,
      newestAt: timestamp(payload.newestAt) ? payload.newestAt : null,
    };
  }

  function normalizePoints(points, from = 0, to = 8640000000000000) {
    if (!Array.isArray(points)) return [];
    const byTime = new Map();
    for (const point of points.slice(0, MAX_POINTS * 2)) {
      if (!record(point) || !timestamp(point.timestamp) || point.timestamp < from || point.timestamp >= to || !plotValue(point.value)) continue;
      const count = point.count === undefined ? 1 : point.count;
      if (!Number.isSafeInteger(count) || count < 1) continue;
      byTime.set(point.timestamp, { timestamp: point.timestamp, value: point.value, count });
    }
    return [...byTime.values()].sort((a, b) => a.timestamp - b.timestamp).slice(0, MAX_POINTS);
  }

  function normalizeQuery(payload, catalog, allowedWorlds) {
    if (!record(payload) || !timestamp(payload.from) || !timestamp(payload.to) || payload.from >= payload.to || !Array.isArray(payload.series)) return null;
    const items = new Map((catalog?.items || []).map((item) => [item.id, item]));
    const worlds = allowedWorlds ? new Set(allowedWorlds) : null;
    const series = [];
    const keys = new Set();
    let omitted = 0;
    for (const row of payload.series.slice(0, MAX_SERIES)) {
      const metric = record(row) ? items.get(row.metricId) : null;
      if (!metric || !identifier(row.worldId, 64) || (worlds && !worlds.has(row.worldId))) { omitted += 1; continue; }
      const agentId = row.agentId ?? null;
      const conversationId = row.conversationId ?? null;
      if ((agentId !== null && !identifier(agentId, 128)) || (conversationId !== null && !identifier(conversationId)) ||
          (conversationId !== null && agentId === null) || (metric.scope === 'world' && (agentId !== null || conversationId !== null)) ||
          (metric.scope === 'agent' && agentId === null) || !Array.isArray(row.points)) { omitted += 1; continue; }
      const key = JSON.stringify([row.metricId, row.worldId, agentId, conversationId]);
      if (keys.has(key)) { omitted += 1; continue; }
      keys.add(key);
      const points = normalizePoints(row.points, payload.from, payload.to).filter((point) =>
        (metric.aggregation === 'sum' || finiteValue(point.value)) && (unitKind(metric) !== 'percent' || point.value <= 100));
      const latestAt = timestamp(row.latestAt) && row.latestAt >= payload.from && row.latestAt < payload.to ? row.latestAt : (points.at(-1)?.timestamp ?? null);
      series.push({ metricId: row.metricId, worldId: row.worldId, agentId, conversationId, points, latestAt,
        sampleIntervalSeconds: bounded(row.sampleIntervalSeconds, 30, 15, 3600) });
      if (points.length !== row.points.length) omitted += 1;
    }
    return {
      from: payload.from, to: payload.to,
      bucketMs: bounded(payload.bucketMs, 60000, 1, DAY * 365),
      retentionDays: bounded(payload.retentionDays, 7, 1, 365), series,
      truncated: payload.truncated === true || payload.series.length > MAX_SERIES,
      omitted,
    };
  }

  function normalizeTiles(payload, catalog, allowedWorlds) {
    if (!record(payload) || !record(payload.worlds) || !timestamp(payload.from) || !timestamp(payload.to) || payload.from >= payload.to) return null;
    const worlds = new Map();
    for (const id of allowedWorlds) {
      const row = Object.prototype.hasOwnProperty.call(payload.worlds, id) ? payload.worlds[id] : null;
      if (!record(row)) continue;
      const metrics = validSelection(row.metrics, catalog, 3, true);
      if (!metrics) continue;
      const query = normalizeQuery({ from: payload.from, to: payload.to, bucketMs: payload.bucketMs || Math.ceil((payload.to - payload.from) / 60),
        retentionDays: payload.retentionDays, series: row.series, truncated: row.truncated }, catalog, [id]);
      if (query) worlds.set(id, { metrics, ...query, series: query.series.filter((series) => metrics.includes(series.metricId)) });
    }
    return { from: payload.from, to: payload.to, worlds, truncated: payload.truncated === true };
  }

  function normalizeDimensions(payload, allowedWorlds) {
    if (!record(payload) || !Array.isArray(payload.worlds) || !Array.isArray(payload.agents) || !Array.isArray(payload.conversations)) return null;
    const allowed = new Set(allowedWorlds);
    const worlds = unique(payload.worlds.filter((id) => identifier(id, 64) && allowed.has(id))).slice(0, 1000);
    const agents = [];
    const conversations = [];
    const seenAgents = new Set();
    const seenConversations = new Set();
    for (const row of payload.agents.slice(0, 5000)) {
      if (!record(row) || !allowed.has(row.worldId) || !identifier(row.agentId, 128)) continue;
      const key = JSON.stringify([row.worldId, row.agentId]);
      if (!seenAgents.has(key)) { agents.push({ worldId: row.worldId, agentId: row.agentId }); seenAgents.add(key); }
    }
    for (const row of payload.conversations.slice(0, 5000)) {
      if (!record(row) || !allowed.has(row.worldId) || !identifier(row.agentId, 128) || !identifier(row.conversationId)) continue;
      const key = JSON.stringify([row.worldId, row.agentId, row.conversationId]);
      if (!seenConversations.has(key)) { conversations.push({ worldId: row.worldId, agentId: row.agentId, conversationId: row.conversationId }); seenConversations.add(key); }
    }
    return { worlds, agents, conversations, truncated: payload.truncated === true || payload.agents.length > 5000 || payload.conversations.length > 5000 };
  }

  function validSelection(values, catalog, maximum = 6, worldOnly = false) {
    if (!Array.isArray(values) || values.length < 1 || values.length > maximum || unique(values).length !== values.length) return null;
    return values.every((id) => catalog?.items.some((item) => item.id === id && (!worldOnly || item.scope === 'world'))) ? [...values] : null;
  }

  function parseRetention(value) {
    if (!/^[1-9]\d{0,2}$/.test(String(value))) return null;
    const days = Number(value);
    return days <= 365 ? days : null;
  }

  function summarize(points, aggregation = 'mean') {
    const clean = normalizePoints(points);
    if (!clean.length) return { latest: null, average: null, total: null, count: 0, latestAt: null, minimum: null, maximum: null };
    const count = clean.reduce((sum, point) => sum + point.count, 0);
    const total = clean.reduce((sum, point) => sum + point.value, 0);
    const average = aggregation === 'sum' ? total / clean.length : clean.reduce((sum, point) => sum + point.value * (point.count / count), 0);
    return { latest: clean.at(-1).value, average, total: aggregation === 'sum' ? total : null, count, latestAt: clean.at(-1).timestamp,
      minimum: Math.min(...clean.map((point) => point.value)), maximum: Math.max(...clean.map((point) => point.value)) };
  }

  function splitSegments(points, bucketMs, intervalMs = 30000) {
    const clean = normalizePoints(points);
    const step = Math.max(bounded(bucketMs, 60000, 1, DAY * 365), bounded(intervalMs, 30000, 1, DAY));
    const segments = [];
    for (const point of clean) {
      const last = segments.at(-1);
      if (!last || point.timestamp - last.at(-1).timestamp > step * 1.75) segments.push([point]);
      else last.push(point);
    }
    return segments;
  }

  function seriesHealth(series, from, to, bucketMs, intervalMs = 30000) {
    const points = normalizePoints(series?.points, from, to);
    if (!points.length) return { missing: true, stale: false, gaps: false, segments: [] };
    intervalMs = bounded(series?.sampleIntervalSeconds, intervalMs / 1000, 15, 3600) * 1000;
    const step = Math.max(bucketMs || 60000, intervalMs);
    const segments = splitSegments(points, bucketMs, intervalMs);
    const last = timestamp(series.latestAt) ? series.latestAt : points.at(-1).timestamp;
    return { missing: false, stale: to - last > Math.max(intervalMs * 3, 90000),
      gaps: segments.length > 1 || points[0].timestamp - from > step * 2 || to - points.at(-1).timestamp > step * 2, segments };
  }

  function unitKind(metric) {
    const unit = text(typeof metric === 'string' ? metric : metric?.unit).toLowerCase();
    const id = text(metric?.id);
    if (unit === '%' || unit === 'percent' || id.endsWith('.percent') || id.endsWith('_percent')) return 'percent';
    if (/bytes?.*(?:second|\/s)|bytes_per_second/.test(unit) || id.endsWith('_bytes_per_second')) return 'rate';
    if (/bytes?|^(?:b|kib|mib|gib)$/.test(unit) || id.endsWith('_bytes')) return 'bytes';
    if (/^(?:ms|milliseconds?)$/.test(unit) || id.endsWith('_ms')) return 'milliseconds';
    if (/^(?:s|seconds?)$/.test(unit) || id.endsWith('.seconds')) return 'seconds';
    if (/^(?:count|number|loops?|connections?|threads?|tools?)$/.test(unit)) return 'count';
    return 'plain';
  }

  function displayScale(metric, maximum = 0) {
    const kind = unitKind(metric);
    if (kind === 'bytes' || kind === 'rate') {
      const index = maximum > 0 ? Math.min(6, Math.max(0, Math.floor(Math.log(maximum) / Math.log(1024)))) : 0;
      return { divisor: 1024 ** index, unit: ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB'][index] + (kind === 'rate' ? '/s' : '') };
    }
    if (kind === 'seconds') {
      if (maximum >= DAY / 1000) return { divisor: 86400, unit: 'd' };
      if (maximum >= 3600) return { divisor: 3600, unit: 'h' };
      if (maximum >= 60) return { divisor: 60, unit: 'min' };
      return { divisor: 1, unit: 's' };
    }
    if (kind === 'milliseconds') return maximum >= 10000 ? { divisor: 1000, unit: 's' } : { divisor: 1, unit: 'ms' };
    if (kind === 'percent') return { divisor: 1, unit: '%' };
    if (kind === 'count') return { divisor: 1, unit: '' };
    return { divisor: 1, unit: text(typeof metric === 'string' ? metric : metric?.unit) };
  }

  function formatValue(value, metric, scale) {
    if (!plotValue(value)) return '—';
    const display = scale || displayScale(metric, value);
    const number = value / display.divisor;
    const digits = number === 0 || number >= 100 ? 0 : (number >= 10 ? 1 : (number >= 0.1 ? 2 : 4));
    const formatted = number > 0 && number < 0.0001 ? number.toExponential(2) : new Intl.NumberFormat('en', { maximumFractionDigits: digits, notation: number >= 100000 ? 'compact' : 'standard' }).format(number);
    return `${formatted}${display.unit ? ` ${display.unit}` : ''}`;
  }

  function niceDomain(values, metric) {
    const clean = values.filter(plotValue);
    const maximum = clean.length ? Math.max(...clean) : 0;
    if (unitKind(metric) === 'percent' && maximum <= 100) return { min: 0, max: 100, ticks: [0, 25, 50, 75, 100] };
    if (maximum === 0) return { min: 0, max: 1, ticks: [0, 0.25, 0.5, 0.75, 1] };
    const raw = maximum / 4;
    if (!raw) return { min: 0, max: maximum, ticks: [0, maximum] };
    const power = 10 ** Math.floor(Math.log10(raw));
    if (!power) return { min: 0, max: maximum, ticks: [0, maximum] };
    const factor = raw / power;
    const step = ([1, 2, 2.5, 5, 10].find((candidate) => candidate >= factor) || 10) * power;
    const max = Math.ceil(maximum / step) * step;
    const ticks = Array.from({ length: Math.round(max / step) + 1 }, (_, index) => index * step);
    return { min: 0, max, ticks };
  }

  function plotModel(points, options = {}) {
    const from = timestamp(options.from) ? options.from : 0;
    const to = timestamp(options.to) && options.to > from ? options.to : from + 3600000;
    const width = bounded(options.width, 600, 100, 2000);
    const height = bounded(options.height, 220, 30, 1000);
    const left = bounded(options.left, 58, 0, width - 20);
    const right = bounded(options.right, 14, 0, width - left - 1);
    const top = bounded(options.top, Math.min(12, height - 2), 0, height - 2);
    const bottom = bounded(options.bottom, Math.min(44, height - top - 1), 0, height - top - 1);
    const clean = normalizePoints(points, from, to);
    const domain = options.domain && Number.isFinite(options.domain.max) && options.domain.max > 0 ? options.domain : niceDomain(clean.map((point) => point.value), options.metric);
    const x = (time) => left + (time - from) / (to - from) * (width - left - right);
    const y = (value) => top + (1 - value / domain.max) * (height - top - bottom);
    const segments = splitSegments(clean, options.bucketMs, options.intervalMs);
    const paths = segments.map((segment) => segment.map((point, index) => `${index ? 'L' : 'M'}${x(point.timestamp).toFixed(2)},${y(point.value).toFixed(2)}`).join(' '));
    return { width, height, from, to, left, right, top, bottom, domain, segments, paths, x, y };
  }

  function nearestPoint(points, target, tolerance = Infinity) {
    if (!Array.isArray(points) || !points.length || !Number.isFinite(target)) return null;
    let low = 0;
    let high = points.length;
    while (low < high) { const mid = Math.floor((low + high) / 2); if (points[mid].timestamp < target) low = mid + 1; else high = mid; }
    const candidates = [points[low - 1], points[low]].filter(Boolean).filter((point) => plotValue(point.value));
    candidates.sort((a, b) => Math.abs(a.timestamp - target) - Math.abs(b.timestamp - target));
    return candidates.length && Math.abs(candidates[0].timestamp - target) <= tolerance ? candidates[0] : null;
  }

  function rangeFor(preset, retentionDays = 7, now = Date.now(), custom = {}) {
    if (preset === 'custom') {
      const from = Date.parse(custom.from);
      const to = Date.parse(custom.to);
      return timestamp(from) && timestamp(to) && from < to ? { from, to } : null;
    }
    const duration = { '1h': 3600000, '24h': DAY, '7d': DAY * 7, full: DAY * bounded(retentionDays, 7, 1, 365) }[preset];
    return duration && timestamp(now) ? { from: Math.max(0, now - duration), to: now } : null;
  }

  function buildQuery(filters, metrics, range, maxPoints = 240) {
    if (!Array.isArray(metrics) || metrics.length < 1 || metrics.length > 6 || !metrics.every(metricId) || unique(metrics).length !== metrics.length ||
        !range || !timestamp(range.from) || !timestamp(range.to) || range.from >= range.to) return null;
    const query = new URLSearchParams();
    metrics.forEach((id) => query.append('metricId', id));
    for (const key of ['worldId', 'agentId', 'conversationId']) if (filters[key]) query.set(key, filters[key]);
    query.set('from', String(range.from)); query.set('to', String(range.to));
    query.set('maxPoints', String(Math.round(bounded(maxPoints, 240, 16, MAX_POINTS))));
    return `/api/metrics/query?${query}`;
  }

  function validateFilters(filters, worlds, dimensions) {
    for (const [key, maximum] of [['worldId', 64], ['agentId', 128], ['conversationId', 256]]) {
      if (filters[key] && !identifier(filters[key], maximum)) return `Invalid ${key} filter. Choose an available value. Filters have not been widened.`;
    }
    if (filters.worldId && !worlds.some((world) => world.id === filters.worldId)) return `Unknown or removed world “${filters.worldId}”. Choose an enrolled world. Filters have not been widened.`;
    const scoped = (row) => !filters.worldId || row.worldId === filters.worldId;
    if (filters.agentId && dimensions && !dimensions.truncated && !dimensions.agents.some((row) => scoped(row) && row.agentId === filters.agentId)) return `No known agent matches “${filters.agentId}” in this scope. Choose another agent or clear the filters. Filters have not been widened.`;
    if (filters.conversationId && dimensions && !dimensions.truncated && !dimensions.conversations.some((row) => scoped(row) && (!filters.agentId || row.agentId === filters.agentId) && row.conversationId === filters.conversationId)) return `No known conversation matches “${filters.conversationId}” in this scope. Choose another conversation or clear the filters. Filters have not been widened.`;
    return '';
  }

  function parseRoute(hash) {
    if (!/^#metrics(?:\?|$)/.test(text(hash))) return null;
    const query = new URLSearchParams(hash.slice('#metrics'.length).replace(/^\?/, ''));
    const result = {};
    // Keep an overlong ID invalid, rather than trimming it into another valid ID.
    for (const key of ['worldId', 'agentId', 'conversationId', 'from', 'to']) result[key] = text(query.get(key)).slice(0, 1024);
    return result;
  }

  const helpers = Object.freeze({ finiteValue, normalizeCatalog, normalizeSettings, normalizePoints, normalizeQuery, normalizeTiles, normalizeDimensions,
    validSelection, parseRetention, summarize, splitSegments, seriesHealth, unitKind, displayScale, formatValue, niceDomain, plotModel, nearestPoint,
    rangeFor, buildQuery, validateFilters, parseRoute });

  let environment = null;
  let ui = null;
  let bound = false;
  const state = {
    epoch: 0, viewEpoch: 0, configEpoch: 0, settingsEpoch: 0, tileRevision: 0, worldSignature: null, controllers: new Set(), tiles: new Map(),
    catalog: null, settings: null, tileData: null, dimensions: null, history: null,
    metadataPromise: null, tilesPromise: null, historyPromise: null,
    lastTilesAttempt: -Infinity, lastHistoryAttempt: -Infinity, lastSettingsRead: -Infinity, tilesError: '',
    open: false, routeChecked: false, datePending: false, selected: [], preset: 'full', filters: { worldId: '', agentId: '', conversationId: '', from: '', to: '' },
    configWorld: '', configSelection: new Set(), configSaving: false, configUnknown: false,
    retentionSaving: false, retentionUnknown: false, retentionDirty: false, selectionSignature: '', chartSequence: 0,
  };
  const active = () => Boolean(environment && environment.isActive());
  const visible = () => document.visibilityState !== 'hidden';
  const worlds = () => {
    const value = environment ? environment.getWorlds() : [];
    return Array.isArray(value) ? value.filter((world) => record(world) && identifier(world.id, 64)).slice(0, 1000) : [];
  };
  const worldById = (id) => worlds().find((world) => world.id === id);
  const metricById = (id) => state.catalog?.items.find((metric) => metric.id === id);
  const intervalMs = () => (state.settings?.sampleIntervalSeconds || 30) * 1000;
  const cancelled = () => Object.assign(new Error('Metrics view changed.'), { name: 'AbortError' });
  const current = (epoch) => active() && epoch === state.epoch;
  const currentView = (epoch, viewEpoch) => current(epoch) && visible() && state.open && viewEpoch === state.viewEpoch;

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value);
    return node;
  }

  function svgElement(tag, attributes = {}, value) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [name, content] of Object.entries(attributes)) node.setAttribute(name, String(content));
    if (value !== undefined) node.textContent = String(value);
    return node;
  }

  function button(label, className, action) {
    const node = element('button', className, label);
    node.type = 'button';
    node.addEventListener('click', action);
    return node;
  }

  function notice(node, message) {
    node.textContent = message || '';
    node.hidden = !message;
  }

  function abortReads(kind) {
    for (const entry of state.controllers) if (!kind || entry.kind === kind) entry.controller.abort();
  }

  // The fifth API argument is optional. A parent which forwards its signal can
  // cancel transport too; the local abort race and epoch guard always apply.
  async function request(path, method = 'GET', body, kind = 'metadata') {
    if (!active()) throw cancelled();
    const epoch = state.epoch;
    const entry = { controller: new AbortController(), kind };
    state.controllers.add(entry);
    const signal = entry.controller.signal;
    let onAbort;
    const abort = new Promise((_, reject) => { onAbort = () => reject(cancelled()); signal.addEventListener('abort', onAbort, { once: true }); });
    try {
      const response = await Promise.race([Promise.resolve(environment.api(path, method, body, undefined, { signal })), abort]);
      if (!current(epoch) || signal.aborted) throw cancelled();
      return record(response) && Object.prototype.hasOwnProperty.call(response, 'data') ? response.data : response;
    } finally {
      signal.removeEventListener('abort', onAbort);
      state.controllers.delete(entry);
    }
  }

  async function metadata() {
    if (state.catalog && state.settings && (state.retentionSaving || Date.now() - state.lastSettingsRead < REFRESH_MS)) return;
    if (state.metadataPromise) return state.metadataPromise;
    const epoch = state.epoch; const settingsEpoch = state.settingsEpoch;
    const pending = (async () => {
      const [catalog, settings] = await Promise.all([state.catalog || request('/api/metrics/catalog'), request('/api/metrics/settings')]);
      if (!current(epoch)) throw cancelled();
      const cleanCatalog = normalizeCatalog(catalog);
      const cleanSettings = normalizeSettings(settings);
      if (!cleanCatalog || !cleanSettings) throw new Error('Invalid metrics metadata.');
      state.catalog = cleanCatalog;
      if (settingsEpoch === state.settingsEpoch && !state.retentionSaving) { state.settings = cleanSettings; state.lastSettingsRead = Date.now(); }
      if (!state.selected.length) state.selected = [...cleanCatalog.defaultMetrics];
    })();
    state.metadataPromise = pending;
    try { await pending; } finally { if (state.metadataPromise === pending) state.metadataPromise = null; }
  }

  function friendlyTime(value) {
    return timestamp(value) ? new Date(value).toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : 'No sample';
  }

  function age(value, now = Date.now()) {
    if (!timestamp(value)) return 'No samples';
    const seconds = Math.max(0, Math.floor((now - value) / 1000));
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  }

  function localInput(value) {
    const date = new Date(value);
    const pad = (part) => String(part).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function writeRoute() {
    if (!window.history?.replaceState || !window.location) return;
    const query = new URLSearchParams();
    if (state.filters.worldId) query.set('worldId', state.filters.worldId);
    const hash = state.open ? `#metrics${query.size ? `?${query}` : ''}` : '';
    window.history.replaceState(null, '', `${window.location.pathname || '/'}${window.location.search || ''}${hash}`);
  }

  function tile(world) {
    if (!identifier(world?.id, 64)) return element('div', 'metrics-tile');
    let node = state.tiles.get(world.id);
    if (!node) { node = element('section', 'metrics-tile'); state.tiles.set(world.id, node); }
    renderTile(node, world);
    return node;
  }

  const tileSignatures = new WeakMap();

  function renderTile(node, world) {
    const signature = JSON.stringify([active(), world.id, world.name, world.connected, state.tileRevision, Boolean(state.catalog), state.tilesError]);
    if (tileSignatures.get(node) === signature) return;
    tileSignatures.set(node, signature);
    node.replaceChildren();
    node.hidden = !active();
    if (!active()) return;
    node.setAttribute('aria-label', `Metrics for ${text(world.name, world.id)}`);
    const head = element('div', 'metrics-tile-heading');
    const history = button('Last hour ↗', 'metrics-tile-link', () => open(world.id));
    history.setAttribute('aria-label', `Open full metric history for ${text(world.name, world.id)}`);
    const configure = button('Configure', 'button button-quiet button-small metrics-configure', () => { void openConfiguration(world.id); });
    configure.setAttribute('aria-label', `Configure shared world card metrics for ${text(world.name, world.id)}`);
    head.append(history, configure); node.append(head);
    const data = state.tileData?.worlds.get(world.id);
    const selection = data?.metrics || state.catalog?.defaultMetrics || DEFAULT_METRICS;
    for (const id of selection) {
      const metric = metricById(id) || { id, name: { 'gateway.cpu.percent': 'Gateway CPU', 'gateway.memory.rss_bytes': 'Gateway memory', 'gateway.loops.active': 'Active loops' }[id] || id, unit: '', scope: 'world', aggregation: 'mean' };
      const series = data?.series.find((row) => row.metricId === id);
      const summary = summarize(series?.points, metric.aggregation);
      const health = seriesHealth(series, data?.from || Date.now() - 3600000, Date.now(), data?.bucketMs || 60000, intervalMs());
      const row = button('', 'metrics-spark-row', () => open(world.id));
      row.setAttribute('aria-label', `${metric.name}: ${formatValue(summary.latest, metric)}. ${health.missing ? 'No samples' : health.stale ? 'Stale sample' : 'Last hour'}. Open full metric history.`);
      const label = element('span', 'metrics-spark-label', metric.name);
      const status = state.tilesError ? 'Unavailable' : !state.tileData ? 'Loading…' : !data && state.tileData.truncated ? 'Tile limit reached' : health.missing ? 'No samples in this range' : `${health.stale ? 'Stale · ' : health.gaps ? 'Gaps · ' : ''}${age(series.latestAt)}`;
      label.append(element('span', `metrics-spark-state${health.stale ? ' is-stale' : ''}`, status));
      const number = element('strong', 'metrics-spark-value', formatValue(summary.latest, metric));
      number.title = summary.latest === null ? 'Missing samples are not zero.' : `Latest bucket: ${summary.latest} ${metric.unit}. Last observation: ${friendlyTime(series.latestAt)}.`;
      const svg = svgElement('svg', { viewBox: '0 0 96 30', class: 'metrics-spark', 'aria-hidden': 'true', focusable: 'false' });
      if (series?.points.length) {
        const model = plotModel(series.points, { from: data.from, to: data.to, width: 100, height: 30, left: 2, right: 6, top: 3, bottom: 3,
          bucketMs: data.bucketMs, intervalMs: (series.sampleIntervalSeconds || 30) * 1000, metric });
        svg.setAttribute('viewBox', '0 0 100 30');
        for (const path of model.paths) svg.append(svgElement('path', { d: path, class: 'metrics-spark-path' }));
        for (const segment of model.segments) if (segment.length === 1) svg.append(svgElement('circle', { cx: model.x(segment[0].timestamp), cy: model.y(segment[0].value), r: 2, class: 'metrics-spark-point' }));
      } else svg.append(svgElement('line', { x1: 2, x2: 94, y1: 15, y2: 15, class: 'metrics-spark-empty' }));
      row.append(label, number, svg); node.append(row);
    }
    if (state.tilesError) node.append(element('p', 'metrics-tile-note', state.tilesError));
    else if (state.tileData?.truncated && !data) node.append(element('p', 'metrics-tile-note', 'The tile response is limited. Open history for this world to read its samples.'));
    else if (!world.connected) node.append(element('p', 'metrics-tile-note', 'World offline · stored samples only'));
  }

  function renderTiles() {
    const enrolled = new Map(worlds().map((world) => [world.id, world]));
    for (const [id, node] of state.tiles) {
      if (!enrolled.has(id)) { node.replaceChildren(); node.hidden = true; node.removeAttribute('aria-label'); state.tiles.delete(id); state.tileData?.worlds.delete(id); }
      else renderTile(node, enrolled.get(id));
    }
    if (state.configWorld && !enrolled.has(state.configWorld)) closeConfiguration(true);
  }

  async function refreshTiles(force = false) {
    if (!active() || !visible()) return;
    if (state.tilesPromise) return state.tilesPromise;
    if (!force && Date.now() - state.lastTilesAttempt < REFRESH_MS) return;
    state.lastTilesAttempt = Date.now();
    const epoch = state.epoch;
    const pending = (async () => {
      try {
        await metadata();
        const payload = await request('/api/metrics/tiles', 'GET', undefined, 'tiles');
        if (!current(epoch) || !visible()) return;
        const clean = normalizeTiles(payload, state.catalog, worlds().map((world) => world.id));
        if (!clean) throw new Error('Invalid tile response.');
        state.tileData = clean; state.tilesError = ''; state.tileRevision += 1;
      } catch (error) {
        if (!current(epoch) || error.name === 'AbortError' || !visible()) return;
        state.tileData = null; state.tilesError = 'Metrics could not be loaded. Worlds are still available.'; state.tileRevision += 1;
      }
      if (current(epoch) && visible()) renderTiles();
    })();
    state.tilesPromise = pending;
    try { await pending; } finally { if (state.tilesPromise === pending) state.tilesPromise = null; }
  }

  async function update() {
    if (!active()) { clear(); return; }
    if (!ui) return;
    ui.nav.hidden = false;
    const enrolledIds = worlds().map((world) => world.id).sort();
    const signature = JSON.stringify(enrolledIds);
    if (state.worldSignature !== signature) {
      if (state.worldSignature !== null) {
        abortReads('tiles'); state.tilesPromise = null; state.lastTilesAttempt = -Infinity;
        state.dimensions = null;
        if (state.tileData) for (const id of state.tileData.worlds.keys()) if (!enrolledIds.includes(id)) state.tileData.worlds.delete(id);
        if (state.open) { invalidateHistory(); syncFilters(); }
      }
      state.worldSignature = signature;
    }
    renderTiles();
    if (!visible()) return;
    if (!state.routeChecked) {
      state.routeChecked = true;
      const route = parseRoute(window.location?.hash);
      if (route) { openRoute(route); }
    }
    await Promise.all([refreshTiles(), state.open ? refreshHistory() : Promise.resolve()]);
  }

  const filterSignatures = new WeakMap();

  function fillSelect(node, options, value, allLabel) {
    const signature = JSON.stringify([options, value, allLabel]);
    if (node.children.length && node.value === (value || '') && filterSignatures.get(node) === signature) return;
    filterSignatures.set(node, signature);
    node.replaceChildren();
    const all = element('option', '', allLabel); all.value = ''; node.append(all);
    for (const option of options) { const item = element('option', '', option.label); item.value = option.value; node.append(item); }
    if (value && !options.some((option) => option.value === value)) {
      const missing = element('option', '', `Not available: ${value}`); missing.value = value; node.append(missing);
    }
    node.value = value || '';
  }

  function agentLabel(worldId, agentId) {
    const world = worldById(worldId);
    const agent = Array.isArray(world?.agents) ? world.agents.find((row) => row.id === agentId) : null;
    return text(agent?.name, agentId);
  }

  function seriesLabel(series) {
    const world = worldById(series.worldId);
    let label = text(world?.name, series.worldId);
    if (world?.name && world.name !== series.worldId) label += ` (${series.worldId})`;
    if (series.agentId) label += ` · ${agentLabel(series.worldId, series.agentId)}${agentLabel(series.worldId, series.agentId) !== series.agentId ? ` (${series.agentId})` : ''}`;
    if (series.conversationId) {
      const conversation = Array.isArray(world?.conversations) ? world.conversations.find((row) => row.id === series.conversationId && row.agentId === series.agentId) : null;
      label += ` · ${text(conversation?.title, series.conversationId)}${conversation?.title && conversation.title !== series.conversationId ? ` (${series.conversationId})` : ''}`;
    }
    return label;
  }

  function syncFilters() {
    const enrolled = worlds();
    fillSelect(ui.world, enrolled.map((world) => ({ value: world.id, label: `${text(world.name, world.id)}${world.name && world.name !== world.id ? ` · ${world.id}` : ''}` })), state.filters.worldId, 'All worlds · separate series');
    const inWorld = (row) => !state.filters.worldId || row.worldId === state.filters.worldId;
    const agentRows = state.dimensions ? state.dimensions.agents : enrolled.flatMap((world) => (Array.isArray(world.agents) ? world.agents : []).map((agent) => ({ worldId: world.id, agentId: agent.id })));
    const groupedAgents = new Map();
    for (const row of agentRows.filter(inWorld)) {
      if (!identifier(row.agentId, 128)) continue;
      const existing = groupedAgents.get(row.agentId);
      if (existing) existing.worlds.add(row.worldId);
      else groupedAgents.set(row.agentId, { label: agentLabel(row.worldId, row.agentId), worlds: new Set([row.worldId]) });
    }
    fillSelect(ui.agent, [...groupedAgents].map(([id, value]) => ({ value: id, label: `${value.label}${value.label !== id ? ` · ${id}` : ''}${value.worlds.size > 1 ? ` · ${value.worlds.size} worlds` : ''}` })), state.filters.agentId, 'All visible agents');
    const conversationRows = state.dimensions ? state.dimensions.conversations : enrolled.flatMap((world) => (Array.isArray(world.conversations) ? world.conversations : []).map((conversation) => ({ worldId: world.id, agentId: conversation.agentId, conversationId: conversation.id })));
    const groupedConversations = new Map();
    for (const row of conversationRows.filter((item) => inWorld(item) && (!state.filters.agentId || item.agentId === state.filters.agentId))) {
      if (!identifier(row.conversationId)) continue;
      const existing = groupedConversations.get(row.conversationId);
      if (existing) existing.worlds.add(row.worldId);
      else {
        const world = worldById(row.worldId);
        const conversation = Array.isArray(world?.conversations) ? world.conversations.find((item) => item.id === row.conversationId && item.agentId === row.agentId) : null;
        groupedConversations.set(row.conversationId, { label: text(conversation?.title, row.conversationId), worlds: new Set([row.worldId]) });
      }
    }
    fillSelect(ui.conversation, [...groupedConversations].map(([id, value]) => ({ value: id, label: `${value.label}${value.label !== id ? ` · ${id}` : ''}${value.worlds.size > 1 ? ` · ${value.worlds.size} worlds` : ''}` })), state.filters.conversationId, 'All visible conversations');
    ui.from.value = state.filters.from; ui.to.value = state.filters.to;
    for (const preset of ui.presets.querySelectorAll('[data-metrics-range]')) preset.setAttribute('aria-pressed', String(preset.getAttribute('data-metrics-range') === state.preset));
    notice(ui.dimensionsHelp, state.dimensions?.truncated ? 'The filter list is limited. Existing filter IDs are kept, never silently replaced. Narrow by world to see more dimensions.' : '');
    const world = state.filters.worldId ? worldById(state.filters.worldId) : null;
    ui.scope.textContent = world ? text(world.name, world.id) : state.filters.worldId ? `Unknown world: ${state.filters.worldId}` : 'All worlds';
  }

  function renderSelection() {
    const signature = JSON.stringify([state.selected, Boolean(state.catalog)]);
    if (signature === state.selectionSignature && ui.selected.children.length) return;
    state.selectionSignature = signature;
    ui.selected.replaceChildren();
    for (const id of state.selected) {
      const metric = metricById(id);
      if (!metric) continue;
      const chip = element('span', 'metrics-chip');
      chip.append(element('span', '', metric.name));
      const remove = button('×', 'metrics-chip-remove', () => {
        if (state.selected.length <= 1) return;
        state.selected = state.selected.filter((selected) => selected !== id); invalidateHistory(); renderSelection(); void refreshHistory(true);
      });
      remove.disabled = state.selected.length <= 1;
      remove.setAttribute('aria-label', `Remove ${metric.name}`);
      chip.append(remove); ui.selected.append(chip);
    }
    ui.add.replaceChildren();
    const placeholder = element('option', '', state.selected.length >= 6 ? 'Six metrics selected' : 'Choose a metric to add…'); placeholder.value = ''; ui.add.append(placeholder);
    for (const scope of ['world', 'agent']) {
      const group = element('optgroup'); group.label = scope === 'world' ? 'World metrics' : 'Agent & conversation metrics';
      for (const metric of state.catalog?.items || []) if (metric.scope === scope && !state.selected.includes(metric.id)) {
        const option = element('option', '', `${metric.name} · ${metric.unit}`); option.value = metric.id; group.append(option);
      }
      if (group.children.length) ui.add.append(group);
    }
    ui.add.disabled = state.selected.length >= 6;
    ui.addButton.disabled = state.selected.length >= 6 || !ui.add.value;
    ui.metricCount.textContent = `${state.selected.length} / 6`;
  }

  function renderSettings() {
    if (!state.settings) return;
    if (!state.retentionDirty && document.activeElement !== ui.retention && !state.retentionSaving) ui.retention.value = String(state.settings.retentionDays);
    ui.retentionSave.disabled = state.retentionSaving || state.retentionUnknown;
    ui.retention.disabled = state.retentionSaving || state.retentionUnknown;
    const numbers = new Intl.NumberFormat('en');
    ui.storage.textContent = `${state.settings.retentionDays}-day retention · ${numbers.format(state.settings.rowCount)} / ${numbers.format(state.settings.maxRows)} stored samples · ${formatValue(state.settings.databaseBytes, 'bytes')} · ${numbers.format(state.settings.rejectedBatches)} rejected batches${state.settings.oldestAt !== null ? ` · Oldest: ${friendlyTime(state.settings.oldestAt)} · Newest: ${friendlyTime(state.settings.newestAt)}` : ' · Collection starts when a connector reports samples'}`;
  }

  function invalidateHistory() {
    state.viewEpoch += 1;
    abortReads('history'); state.historyPromise = null; state.lastHistoryAttempt = -Infinity;
    state.history = null; ui.charts.replaceChildren();
  }

  function historyRange() {
    return rangeFor(state.preset, state.settings?.retentionDays || 7, Date.now(), state.filters);
  }

  function setRangeInputs(range) {
    if (state.preset === 'custom' || !range) return;
    state.filters.from = localInput(range.from); state.filters.to = localInput(range.to);
    ui.from.value = state.filters.from; ui.to.value = state.filters.to;
  }

  function scopeMessage() {
    return (state.filters.agentId || state.filters.conversationId) && state.selected.some((id) => metricById(id)?.scope === 'world')
      ? ' World metrics do not apply to agent or conversation filters. Add an agent metric or clear those filters.' : '';
  }

  async function refreshHistory(force = false) {
    if (!active() || !visible() || !state.open || state.datePending || !ui) return;
    if (state.historyPromise) return state.historyPromise;
    if (!force && Date.now() - state.lastHistoryAttempt < REFRESH_MS) return;
    if (!force && (ui.charts.contains?.(document.activeElement) || ui.filterForm.contains?.(document.activeElement) || document.activeElement === ui.add)) {
      ui.status.textContent = 'History refresh paused while you inspect this chart or table, or edit filters. Use Refresh for new samples.';
      return;
    }
    state.lastHistoryAttempt = Date.now();
    const epoch = state.epoch;
    const viewEpoch = state.viewEpoch;
    ui.section.setAttribute('aria-busy', 'true'); ui.status.textContent = state.history ? 'Updating history…' : 'Loading metric history…';
    notice(ui.error, '');
    const pending = (async () => {
      try {
        await metadata();
        if (!currentView(epoch, viewEpoch)) return;
        if (!validSelection(state.selected, state.catalog)) state.selected = [...state.catalog.defaultMetrics];
        syncFilters(); renderSelection(); renderSettings();
        const range = historyRange();
        if (!range) throw Object.assign(new Error('Choose a valid From and To time. From must be before To. Times use your local time zone.'), { display: true });
        const filterError = validateFilters(state.filters, worlds(), null);
        if (filterError) throw Object.assign(new Error(filterError), { display: true });
        setRangeInputs(range);
        const dimensionsPath = `/api/metrics/dimensions${state.filters.worldId ? `?worldId=${encodeURIComponent(state.filters.worldId)}` : ''}`;
        const query = buildQuery(state.filters, state.selected, range);
        if (!query) throw new Error('Invalid metric selection.');
        const [rawDimensions, rawHistory] = await Promise.all([request(dimensionsPath, 'GET', undefined, 'history'), request(query, 'GET', undefined, 'history')]);
        if (!currentView(epoch, viewEpoch)) return;
        const allowed = worlds().map((world) => world.id);
        const dimensions = normalizeDimensions(rawDimensions, allowed);
        const history = normalizeQuery(rawHistory, state.catalog, allowed);
        if (!dimensions || !history || history.from !== range.from || history.to !== range.to) throw new Error('Invalid metric history.');
        state.dimensions = dimensions; syncFilters();
        const error = validateFilters(state.filters, worlds(), dimensions);
        if (error) throw Object.assign(new Error(error), { display: true });
        history.series = history.series.filter((series) => state.selected.includes(series.metricId) &&
          (!state.filters.worldId || series.worldId === state.filters.worldId) && (!state.filters.agentId || series.agentId === state.filters.agentId) &&
          (!state.filters.conversationId || series.conversationId === state.filters.conversationId));
        state.history = history; renderCharts();
        const span = `${friendlyTime(history.from)} – ${friendlyTime(history.to)}`;
        const timezone = new Intl.DateTimeFormat().resolvedOptions().timeZone || 'local time';
        const bucket = formatValue(history.bucketMs / 1000, 'seconds');
        ui.rangeHelp.textContent = `${span} · ${timezone}. Up to 240 buckets per exact series; ${bucket} buckets here. Means are weighted by measured sample counts; completed loops are summed per bucket. Missing samples stay as gaps, never zero. Collection is not historical backfill.`;
        const count = history.series.filter((series) => series.points.length).length;
        const warnings = `${history.truncated ? ' Results are limited to 64 series. Narrow the filters.' : ''}${history.omitted ? ' Invalid or unavailable samples were omitted.' : ''}${scopeMessage()}`;
        ui.summary.textContent = count ? `${count} separate series · worlds and dimensions are never combined.${count > 8 ? ' Narrow the filters to compare fewer lines.' : ''}${warnings}`
          : `No samples match these exact filters and dates. Try another range, check the connector, or clear the filters.${warnings}`;
        ui.status.textContent = `Updated ${new Date().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })} · refreshes at most every 30s`;
      } catch (error) {
        if (!currentView(epoch, viewEpoch) || error.name === 'AbortError') return;
        state.history = null; ui.charts.replaceChildren(); ui.summary.textContent = ''; ui.rangeHelp.textContent = '';
        notice(ui.error, error.display ? error.message : 'Metric history could not be loaded. Your filters have not been changed. Try Refresh to read it again.');
        ui.status.textContent = 'History not available';
      } finally {
        if (currentView(epoch, viewEpoch)) ui.section.removeAttribute('aria-busy');
      }
    })();
    state.historyPromise = pending;
    try { await pending; } finally { if (state.historyPromise === pending) state.historyPromise = null; }
  }

  function timeTicks(svg, model) {
    const count = model.width < 430 ? 3 : 4;
    const showDate = model.to - model.from >= DAY;
    for (let index = 0; index < count; index += 1) {
      const time = Math.round(model.from + (model.to - model.from) * index / (count - 1));
      const x = model.x(time);
      const anchor = index === 0 ? 'start' : index === count - 1 ? 'end' : 'middle';
      const date = new Date(time);
      svg.append(svgElement('line', { x1: x, x2: x, y1: model.height - model.bottom, y2: model.height - model.bottom + 5, class: 'metrics-axis-mark' }));
      svg.append(svgElement('text', { x, y: model.height - (showDate ? 24 : 16), 'text-anchor': anchor, class: 'metrics-axis-label' }, date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })));
      if (showDate) svg.append(svgElement('text', { x, y: model.height - 8, 'text-anchor': anchor, class: 'metrics-axis-label' }, date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })));
    }
  }

  function makeTooltip(svg, wrapper, model, series, metric, history) {
    const tooltip = element('div', 'metrics-tooltip'); tooltip.hidden = true;
    tooltip.setAttribute('role', 'status'); tooltip.setAttribute('aria-live', 'polite'); tooltip.setAttribute('aria-atomic', 'true');
    const cursor = svgElement('line', { x1: model.left, x2: model.left, y1: model.top, y2: model.height - model.bottom, class: 'metrics-crosshair', hidden: '' });
    svg.append(cursor); wrapper.append(tooltip);
    const times = unique(series.flatMap((row) => row.points.map((point) => point.timestamp))).sort((a, b) => a - b);
    let selectedTime = times.at(-1);
    function hide() { tooltip.hidden = true; cursor.setAttribute('hidden', ''); }
    function show(time) {
      if (!times.length) return;
      selectedTime = Math.min(history.to - 1, Math.max(history.from, time));
      tooltip.replaceChildren(element('strong', 'metrics-tooltip-time', friendlyTime(selectedTime)));
      let found = false;
      for (const [index, row] of series.slice(0, 8).entries()) {
        const point = nearestPoint(row.points, selectedTime, Math.max(history.bucketMs, intervalMs()) * 0.75);
        const line = element('div', 'metrics-tooltip-row');
        const label = element('span', '', seriesLabel(row));
        const swatch = element('span', 'metrics-series-swatch'); swatch.setAttribute('data-series', String(index % 8)); swatch.setAttribute('aria-hidden', 'true');
        line.append(swatch, label, element('strong', '', point ? formatValue(point.value, metric) : 'No sample'));
        if (point) { found = true; line.title = `${point.value} ${metric.unit} · ${point.count} measured samples · ${friendlyTime(point.timestamp)}`; }
        tooltip.append(line);
      }
      if (series.length > 8) tooltip.append(element('p', '', `${series.length - 8} more series · see the data table below`));
      if (!found) tooltip.append(element('p', '', 'A collection gap, not a measured zero.'));
      tooltip.hidden = false; cursor.removeAttribute('hidden');
      cursor.setAttribute('x1', model.x(selectedTime)); cursor.setAttribute('x2', model.x(selectedTime));
    }
    svg.addEventListener('pointermove', (event) => {
      const bounds = svg.getBoundingClientRect();
      if (!bounds.width) return;
      const x = (event.clientX - bounds.left) / bounds.width * model.width;
      if (x < model.left || x > model.width - model.right) { hide(); return; }
      show(model.from + (x - model.left) / (model.width - model.left - model.right) * (model.to - model.from));
    });
    svg.addEventListener('pointerleave', () => { if (document.activeElement !== svg) hide(); });
    svg.addEventListener('focus', () => show(times.at(-1)));
    svg.addEventListener('blur', hide);
    svg.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { hide(); return; }
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || !times.length) return;
      event.preventDefault();
      const nearest = times.reduce((best, time, index) => Math.abs(time - selectedTime) < Math.abs(times[best] - selectedTime) ? index : best, 0);
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? times.length - 1 : Math.max(0, Math.min(times.length - 1, nearest + (event.key === 'ArrowLeft' ? -1 : 1)));
      show(times[index]);
    });
  }

  function chartTable(metric, series, history) {
    const details = element('details', 'metrics-data-disclosure');
    const summary = element('summary', '', `Series & data table · ${series.length}`); details.append(summary);
    let populated = false;
    details.addEventListener('toggle', () => {
      if (!details.open || populated) return;
      populated = true;
      const scroll = element('div', 'metrics-table-scroll'); scroll.tabIndex = 0; scroll.setAttribute('role', 'region'); scroll.setAttribute('aria-label', `${metric.name} series summary`);
      const table = element('table', 'metrics-table');
      const caption = element('caption', '', `${metric.name}: ${metric.aggregation === 'sum' ? 'average per populated bucket; total is the sum of those buckets' : 'average weighted by measured sample counts'}. Missing is not zero.`);
      const head = element('thead'); const heading = element('tr');
      const headings = ['World / dimensions', 'Latest bucket', metric.aggregation === 'sum' ? 'Average / bucket' : 'Average', ...(metric.aggregation === 'sum' ? ['Total'] : []), 'Last observation', 'Coverage'];
      for (const label of headings) { const cell = element('th', '', label); cell.scope = 'col'; heading.append(cell); }
      head.append(heading); table.append(caption, head);
      const body = element('tbody');
      for (const row of series) {
        const stats = summarize(row.points, metric.aggregation);
        const health = seriesHealth(row, history.from, history.to, history.bucketMs, intervalMs());
        const tr = element('tr'); const name = element('th', '', seriesLabel(row)); name.scope = 'row'; tr.append(name);
        for (const value of [stats.latest, stats.average, ...(metric.aggregation === 'sum' ? [stats.total] : [])]) {
          const cell = element('td', 'metrics-number', formatValue(value, metric)); cell.title = value === null ? 'No measured sample' : `${value} ${metric.unit}`; tr.append(cell);
        }
        tr.append(element('td', '', friendlyTime(row.latestAt)), element('td', '', health.missing ? 'No samples' : [health.stale ? 'Stale at range end' : 'Sampled', health.gaps ? 'gaps' : ''].filter(Boolean).join(' · ')));
        body.append(tr);
      }
      table.append(body); scroll.append(table); details.append(scroll);
      const buckets = element('details', 'metrics-bucket-disclosure'); buckets.append(element('summary', '', 'View plotted bucket values'));
      let bucketTable = null;
      buckets.addEventListener('toggle', () => {
        if (!buckets.open || bucketTable) return;
        const label = element('label', '', 'Series');
        const select = element('select'); const selectId = `metrics-data-series-${++state.chartSequence}`; select.id = selectId; label.htmlFor = selectId;
        series.forEach((row, index) => { const option = element('option', '', seriesLabel(row)); option.value = String(index); select.append(option); });
        const container = element('div', 'metrics-table-scroll'); container.tabIndex = 0;
        bucketTable = element('table', 'metrics-table'); container.append(bucketTable); buckets.append(label, select, container);
        function renderBuckets() {
          const row = series[Number(select.value) || 0];
          bucketTable.replaceChildren();
          bucketTable.append(element('caption', '', `${seriesLabel(row)} · ${metric.name}. Only measured buckets are listed; absent times are gaps.`));
          const thead = element('thead'); const tr = element('tr');
          for (const title of ['Bucket start', `Value (${metric.unit})`, 'Sample count / weight']) { const th = element('th', '', title); th.scope = 'col'; tr.append(th); }
          thead.append(tr); bucketTable.append(thead);
          const tbody = element('tbody');
          for (const point of row.points) { const line = element('tr'); line.append(element('td', '', friendlyTime(point.timestamp)), element('td', 'metrics-number', String(point.value)), element('td', 'metrics-number', String(point.count))); tbody.append(line); }
          bucketTable.append(tbody);
        }
        select.addEventListener('change', renderBuckets); renderBuckets();
      });
      if (series.length) details.append(buckets);
    });
    return details;
  }

  function chart(metric, series, history, width) {
    const card = element('article', 'metrics-chart panel');
    const id = `metrics-chart-${++state.chartSequence}`;
    const heading = element('div', 'metrics-chart-heading');
    const name = element('h2', '', metric.name); name.id = `${id}-title`;
    const scope = element('span', 'metrics-unit-badge', `${metric.scope === 'world' ? 'World' : 'Agent'} · ${metric.unit}`);
    heading.append(name, scope); card.append(heading); card.setAttribute('aria-labelledby', name.id);
    const description = element('p', 'metrics-chart-description', metric.description); description.id = `${id}-description`; card.append(description);
    const measured = series.filter((row) => row.points.length);
    if (!measured.length) {
      const empty = element('div', 'metrics-chart-empty');
      empty.append(element('strong', '', 'No measured samples'), element('p', '', metric.scope === 'world' && (state.filters.agentId || state.filters.conversationId)
        ? 'This is a world metric. Clear agent and conversation filters, or add an agent metric.'
        : 'No data in this range and scope. Check the connector or choose another range. Missing does not mean zero.'));
      card.append(empty); return card;
    }
    const values = measured.flatMap((row) => row.points.map((point) => point.value));
    const domain = niceDomain(values, metric);
    const scale = displayScale(metric, domain.max);
    const model = plotModel([], { from: history.from, to: history.to, metric, domain, width, height: 246, left: 54, right: 12, top: 28, bottom: 52, bucketMs: history.bucketMs });
    const wrapper = element('div', 'metrics-plot-wrap');
    const svg = svgElement('svg', { viewBox: `0 0 ${model.width} ${model.height}`, class: 'metrics-plot', role: 'img', tabindex: '0',
      'aria-labelledby': `${id}-svg-title`, 'aria-describedby': `${id}-description ${id}-hint` });
    svg.append(svgElement('title', { id: `${id}-svg-title` }, `${metric.name} over time, ${measured.length} separate series. Units: ${metric.unit}. Arrow keys inspect samples. A full data table follows.`));
    svg.append(svgElement('text', { x: model.left, y: 14, class: 'metrics-axis-label metrics-axis-unit' }, scale.unit || metric.unit));
    const ticks = model.width < 360 && domain.ticks.length > 4 ? domain.ticks.filter((_, index) => index % 2 === 0) : domain.ticks;
    for (const tick of ticks) {
      const y = model.y(tick);
      svg.append(svgElement('line', { x1: model.left, x2: model.width - model.right, y1: y, y2: y, class: 'metrics-gridline' }));
      svg.append(svgElement('text', { x: model.left - 9, y: y + 4, 'text-anchor': 'end', class: 'metrics-axis-label' }, formatValue(tick, metric, { divisor: scale.divisor, unit: '' })));
    }
    svg.append(svgElement('line', { x1: model.left, x2: model.width - model.right, y1: model.height - model.bottom, y2: model.height - model.bottom, class: 'metrics-axis-mark' }));
    timeTicks(svg, model);
    measured.forEach((row, index) => {
      const plotted = plotModel(row.points, { from: history.from, to: history.to, metric, domain, width: model.width, height: model.height, left: model.left, right: model.right, top: model.top, bottom: model.bottom, bucketMs: history.bucketMs, intervalMs: (row.sampleIntervalSeconds || 30) * 1000 });
      const group = svgElement('g', { 'data-series': index % 8, class: 'metrics-series' });
      group.append(svgElement('title', {}, seriesLabel(row)));
      for (const path of plotted.paths) group.append(svgElement('path', { d: path, class: 'metrics-line' }));
      for (const segment of plotted.segments) {
        const point = segment.at(-1);
        group.append(svgElement('circle', { cx: plotted.x(point.timestamp), cy: plotted.y(point.value), r: segment.length === 1 ? 2.8 : 2.2, class: 'metrics-point' }));
      }
      svg.append(group);
    });
    wrapper.append(svg); makeTooltip(svg, wrapper, model, measured, metric, history); card.append(wrapper);
    const hint = element('p', 'metrics-chart-hint', 'Hover or focus the chart. Use ← → to inspect; Home / End jump to the first / last sample.'); hint.id = `${id}-hint`; card.append(hint);
    const legend = element('ul', 'metrics-legend'); legend.setAttribute('aria-label', `${metric.name} series, latest values and averages`);
    measured.forEach((row, index) => {
      const stats = summarize(row.points, metric.aggregation);
      const health = seriesHealth(row, history.from, history.to, history.bucketMs, intervalMs());
      const item = element('li', 'metrics-legend-row');
      const swatch = element('span', 'metrics-series-swatch'); swatch.setAttribute('data-series', String(index % 8)); swatch.setAttribute('aria-hidden', 'true');
      const identity = element('div', 'metrics-legend-identity'); identity.append(element('strong', '', seriesLabel(row)));
      const freshness = element('span', `metrics-coverage${health.stale ? ' is-stale' : ''}`, `${health.stale ? 'Stale at range end' : 'Sampled'}${health.gaps ? ' · gaps' : ''} · ${friendlyTime(row.latestAt)}`); identity.append(freshness);
      const statsNode = element('div', 'metrics-legend-values');
      const latest = element('span', '', 'Latest '); latest.append(element('strong', '', formatValue(stats.latest, metric))); latest.title = `Latest measured bucket: ${stats.latest} ${metric.unit}`;
      const average = element('span', '', metric.aggregation === 'sum' ? 'Avg / bucket ' : 'Average '); average.append(element('strong', '', formatValue(stats.average, metric)));
      average.title = metric.aggregation === 'sum' ? 'Average of populated bucket totals. Missing buckets are excluded.' : 'Average weighted by measured sample counts. Missing buckets are excluded.';
      statsNode.append(latest, average); item.append(swatch, identity, statsNode); legend.append(item);
    });
    card.append(legend, chartTable(metric, series, history)); return card;
  }

  function renderCharts() {
    if (!state.history || !state.open || !active()) return;
    ui.charts.replaceChildren();
    const containerWidth = ui.charts.clientWidth || Math.max(240, (window.innerWidth || 1000) - 72);
    const columns = (window.innerWidth || 1000) >= 1120 ? 2 : 1;
    const width = Math.max(230, Math.min(1100, Math.round((containerWidth - (columns - 1) * 18) / columns - 32)));
    for (const id of state.selected) {
      const metric = metricById(id);
      if (metric) ui.charts.append(chart(metric, state.history.series.filter((series) => series.metricId === id), state.history, width));
    }
  }

  function syncConfiguration() {
    const count = state.configSelection.size;
    ui.configCount.textContent = `${count} / 3 selected · shared by everyone`;
    ui.configSave.disabled = count < 1 || count > 3 || state.configSaving || state.configUnknown;
    ui.configSave.textContent = state.configSaving ? 'Saving once…' : 'Save card metrics';
    ui.configClose.disabled = state.configSaving; ui.configCancel.disabled = state.configSaving;
    for (const input of ui.configChoices.querySelectorAll('input')) input.disabled = state.configSaving || state.configUnknown || (count >= 3 && !input.checked);
  }

  function closeConfiguration(force = false) {
    if (state.configSaving && !force) return;
    state.configEpoch += 1; state.configWorld = ''; state.configSelection.clear(); state.configSaving = false; state.configUnknown = false;
    abortReads('configuration');
    if (!ui) return;
    if (ui.configDialog.open) ui.configDialog.close();
    ui.configChoices.replaceChildren(); ui.configName.textContent = ''; ui.configCount.textContent = '';
    ui.configClose.disabled = false; ui.configCancel.disabled = false;
    notice(ui.configError, ''); notice(ui.configFeedback, '');
  }

  async function openConfiguration(worldId) {
    if (!active() || !visible() || !ui || state.configSaving || !worldById(worldId)) return;
    closeConfiguration();
    const epoch = state.epoch;
    const configEpoch = state.configEpoch;
    state.configWorld = worldId;
    ui.configName.textContent = `${text(worldById(worldId)?.name, worldId)} · shared world card`;
    ui.configCount.textContent = 'Reading saved selection…'; ui.configSave.disabled = true;
    ui.configDialog.showModal();
    try {
      await metadata();
      const payload = await request(`/api/metrics/tiles?worldId=${encodeURIComponent(worldId)}`, 'GET', undefined, 'configuration');
      if (!current(epoch) || configEpoch !== state.configEpoch || state.configWorld !== worldId || !worldById(worldId)) return;
      const tiles = normalizeTiles(payload, state.catalog, [worldId]);
      if (!tiles || !tiles.worlds.has(worldId)) throw new Error('Invalid shared card selection.');
      const selection = tiles.worlds.get(worldId).metrics;
      state.configSelection = new Set(selection);
      ui.configChoices.replaceChildren();
      for (const metric of state.catalog.items.filter((item) => item.scope === 'world')) {
        const label = element('label', 'metrics-choice');
        const input = element('input'); input.type = 'checkbox'; input.value = metric.id; input.checked = state.configSelection.has(metric.id);
        const name = element('span', 'metrics-choice-copy'); name.append(element('strong', '', metric.name), element('span', '', `${metric.unit} · ${metric.description}`));
        input.addEventListener('change', () => {
          if (input.checked) state.configSelection.add(metric.id); else state.configSelection.delete(metric.id);
          notice(ui.configFeedback, ''); syncConfiguration();
        });
        label.append(input, name); ui.configChoices.append(label);
      }
      syncConfiguration();
    } catch (error) {
      if (!current(epoch) || configEpoch !== state.configEpoch || error.name === 'AbortError') return;
      ui.configCount.textContent = 'Saved selection not available';
      notice(ui.configError, 'The saved card metrics could not be read. Close and reopen this dialog to try again. No settings were changed.');
    }
  }

  async function saveConfiguration(event) {
    event.preventDefault();
    if (!active() || !state.configWorld || state.configSaving || state.configUnknown) return;
    const selection = validSelection([...state.configSelection], state.catalog, 3, true);
    if (!selection) { notice(ui.configError, 'Choose between one and three world metrics.'); return; }
    const epoch = state.epoch; const configEpoch = state.configEpoch; const worldId = state.configWorld;
    state.configSaving = true; notice(ui.configError, ''); notice(ui.configFeedback, ''); syncConfiguration();
    try {
      const payload = await request(`/api/worlds/${encodeURIComponent(worldId)}/metrics`, 'PUT', { metrics: selection }, 'mutation');
      if (!current(epoch) || configEpoch !== state.configEpoch) return;
      const saved = record(payload) && payload.worldId === worldId ? validSelection(payload.metrics, state.catalog, 3, true) : null;
      if (!saved) throw new Error('Unconfirmed metric selection.');
      state.configSelection = new Set(saved);
      for (const input of ui.configChoices.querySelectorAll('input')) input.checked = state.configSelection.has(input.value);
      notice(ui.configFeedback, 'Saved for everyone. Only this world card was changed.');
      abortReads('tiles'); state.tilesPromise = null; state.lastTilesAttempt = -Infinity;
      await refreshTiles(true);
    } catch (error) {
      if (!current(epoch) || configEpoch !== state.configEpoch || error.name === 'AbortError') return;
      state.configUnknown = true;
      notice(ui.configError, 'Save was not confirmed and was not retried. Close and reopen this dialog to read the saved selection before sending another change.');
    } finally {
      if (current(epoch) && configEpoch === state.configEpoch) { state.configSaving = false; syncConfiguration(); }
    }
  }

  async function saveRetention(event) {
    event.preventDefault();
    if (!active() || !state.settings || state.retentionSaving || state.retentionUnknown) return;
    const retentionDays = parseRetention(ui.retention.value);
    if (retentionDays === null) { notice(ui.retentionFeedback, 'Enter a whole number from 1 to 365 days.'); return; }
    const epoch = state.epoch;
    state.settingsEpoch += 1;
    state.retentionSaving = true; notice(ui.retentionFeedback, ''); renderSettings();
    try {
      const payload = await request('/api/metrics/settings', 'PUT', { retentionDays }, 'mutation');
      if (!current(epoch)) return;
      const settings = normalizeSettings(payload);
      if (!settings) throw new Error('Unconfirmed retention.');
      state.settings = settings; state.lastSettingsRead = Date.now(); state.retentionDirty = false;
      ui.retention.value = String(settings.retentionDays);
      notice(ui.retentionFeedback, `Saved: ${settings.retentionDays} days for everyone. Retention is stored by the Hub, not this browser.`);
      if (state.open) { invalidateHistory(); await refreshHistory(true); }
    } catch (error) {
      if (!current(epoch) || error.name === 'AbortError') return;
      state.retentionUnknown = true;
      notice(ui.retentionFeedback, 'Save was not confirmed and was not retried. Read saved settings before sending another change.');
    } finally {
      if (current(epoch)) { state.retentionSaving = false; renderSettings(); }
    }
  }

  async function readSettings() {
    if (!active() || state.retentionSaving) return;
    const epoch = state.epoch; const settingsEpoch = ++state.settingsEpoch; ui.retentionCheck.disabled = true;
    try {
      const settings = normalizeSettings(await request('/api/metrics/settings'));
      if (!current(epoch) || settingsEpoch !== state.settingsEpoch) return;
      if (!settings) throw new Error('Invalid settings.');
      state.settings = settings; state.retentionUnknown = false; state.lastSettingsRead = Date.now(); state.retentionDirty = false;
      ui.retention.value = String(settings.retentionDays);
      notice(ui.retentionFeedback, `Saved setting: ${settings.retentionDays} days. No change was sent.`); renderSettings();
    } catch (error) {
      if (current(epoch) && settingsEpoch === state.settingsEpoch && error.name !== 'AbortError') notice(ui.retentionFeedback, 'Saved settings could not be read. No change was sent.');
    } finally { if (current(epoch)) ui.retentionCheck.disabled = false; }
  }

  function showSection() {
    state.open = true; ui.section.hidden = false; ui.nav.setAttribute('aria-expanded', 'true');
    syncFilters(); renderSelection(); renderSettings(); writeRoute();
    ui.title.focus();
    if (typeof ui.section.scrollIntoView === 'function') ui.section.scrollIntoView({ block: 'start' });
    void refreshHistory(true);
  }

  function open(worldId) {
    if (!active() || !ui) return;
    closeConfiguration(); invalidateHistory();
    state.routeChecked = true; state.datePending = false; state.preset = 'full'; state.dimensions = null;
    state.filters = { worldId: typeof worldId === 'string' ? worldId : '', agentId: '', conversationId: '', from: '', to: '' };
    const saved = state.tileData?.worlds.get(state.filters.worldId)?.metrics;
    state.selected = saved ? [...saved] : [...(state.catalog?.defaultMetrics || [])];
    showSection();
  }

  function openRoute(route) {
    if (!active() || !ui) return;
    closeConfiguration(); invalidateHistory(); state.datePending = false; state.preset = 'full'; state.dimensions = null;
    state.filters = { worldId: route.worldId, agentId: route.agentId, conversationId: route.conversationId, from: '', to: '' };
    if (route.from || route.to) {
      state.preset = 'custom';
      for (const key of ['from', 'to']) {
        const value = /^\d+$/.test(route[key]) ? Number(route[key]) : Date.parse(route[key]);
        state.filters[key] = timestamp(value) ? localInput(value) : route[key];
      }
    }
    showSection();
  }

  function close() {
    if (!ui) return;
    state.open = false; state.viewEpoch += 1; abortReads('history'); state.historyPromise = null; state.history = null; state.dimensions = null;
    ui.section.hidden = true; ui.section.removeAttribute('aria-busy'); ui.charts.replaceChildren();
    ui.nav.setAttribute('aria-expanded', 'false'); writeRoute();
    if (active()) ui.nav.focus();
  }

  function clear() {
    state.epoch += 1; state.viewEpoch += 1; state.configEpoch += 1; state.settingsEpoch += 1; state.worldSignature = null; abortReads();
    state.open = false; state.catalog = null; state.settings = null; state.tileData = null; state.dimensions = null; state.history = null;
    state.metadataPromise = null; state.tilesPromise = null; state.historyPromise = null;
    state.lastTilesAttempt = -Infinity; state.lastHistoryAttempt = -Infinity; state.lastSettingsRead = -Infinity; state.tilesError = ''; state.tileRevision += 1;
    state.routeChecked = false; state.datePending = false; state.selected = []; state.preset = 'full';
    state.filters = { worldId: '', agentId: '', conversationId: '', from: '', to: '' };
    state.retentionSaving = false; state.retentionUnknown = false; state.retentionDirty = false; state.selectionSignature = '';
    for (const node of state.tiles.values()) { node.replaceChildren(); node.removeAttribute('aria-label'); node.hidden = true; }
    state.tiles.clear();
    if (!ui) return;
    closeConfiguration(true);
    ui.nav.hidden = true; ui.nav.setAttribute('aria-expanded', 'false'); ui.section.hidden = true; ui.section.removeAttribute('aria-busy');
    for (const node of [ui.charts, ui.selected, ui.world, ui.agent, ui.conversation, ui.add]) node.replaceChildren();
    for (const node of [ui.summary, ui.scope, ui.status, ui.rangeHelp, ui.storage, ui.metricCount]) node.textContent = '';
    for (const node of [ui.error, ui.dimensionsHelp, ui.retentionFeedback]) notice(node, '');
    ui.from.value = ''; ui.to.value = ''; ui.retention.value = ''; ui.retentionCheck.disabled = false;
    writeRoute();
  }

  function bindUi() {
    if (bound) return;
    bound = true;
    ui.nav.addEventListener('click', () => open()); ui.close.addEventListener('click', close);
    ui.filterForm.addEventListener('submit', (event) => {
      event.preventDefault();
      state.filters = { worldId: ui.world.value, agentId: ui.agent.value, conversationId: ui.conversation.value, from: ui.from.value, to: ui.to.value };
      state.datePending = false;
      invalidateHistory(); writeRoute(); void refreshHistory(true);
    });
    ui.world.addEventListener('change', () => {
      state.filters.worldId = ui.world.value; state.filters.agentId = ''; state.filters.conversationId = ''; state.dimensions = null;
      invalidateHistory(); syncFilters(); writeRoute(); void refreshHistory(true);
    });
    ui.agent.addEventListener('change', () => {
      state.filters.agentId = ui.agent.value; state.filters.conversationId = '';
      invalidateHistory(); syncFilters(); void refreshHistory(true);
    });
    ui.conversation.addEventListener('change', () => {
      state.filters.conversationId = ui.conversation.value; invalidateHistory(); void refreshHistory(true);
    });
    function editDates() {
      if (state.datePending && state.filters.from === ui.from.value && state.filters.to === ui.to.value) return;
      state.preset = 'custom'; state.datePending = true; state.filters.from = ui.from.value; state.filters.to = ui.to.value;
      invalidateHistory(); ui.section.removeAttribute('aria-busy'); ui.summary.textContent = ''; ui.rangeHelp.textContent = '';
      ui.status.textContent = 'Dates changed · choose Apply dates to load this range.';
      for (const preset of ui.presets.querySelectorAll('[data-metrics-range]')) preset.setAttribute('aria-pressed', 'false');
    }
    for (const input of [ui.from, ui.to]) { input.addEventListener('input', editDates); input.addEventListener('change', editDates); }
    ui.clearFilters.addEventListener('click', () => {
      state.filters = { worldId: '', agentId: '', conversationId: '', from: '', to: '' }; state.datePending = false; state.preset = 'full'; state.dimensions = null;
      invalidateHistory(); syncFilters(); writeRoute(); void refreshHistory(true);
    });
    for (const preset of ui.presets.querySelectorAll('[data-metrics-range]')) preset.addEventListener('click', () => {
      state.datePending = false; state.preset = preset.getAttribute('data-metrics-range'); invalidateHistory(); setRangeInputs(historyRange()); syncFilters(); void refreshHistory(true);
    });
    ui.refresh.addEventListener('click', () => { state.datePending = false; void refreshHistory(true); });
    ui.add.addEventListener('change', () => { ui.addButton.disabled = !ui.add.value || state.selected.length >= 6; });
    ui.addButton.addEventListener('click', () => {
      if (!metricById(ui.add.value) || state.selected.includes(ui.add.value) || state.selected.length >= 6) return;
      state.selected.push(ui.add.value); invalidateHistory(); renderSelection(); void refreshHistory(true);
    });
    ui.configForm.addEventListener('submit', saveConfiguration);
    ui.configClose.addEventListener('click', () => closeConfiguration()); ui.configCancel.addEventListener('click', () => closeConfiguration());
    ui.configDialog.addEventListener('cancel', (event) => { event.preventDefault(); closeConfiguration(); });
    ui.retentionForm.addEventListener('submit', saveRetention); ui.retentionCheck.addEventListener('click', () => { void readSettings(); });
    ui.retention.addEventListener('input', () => { state.retentionDirty = true; notice(ui.retentionFeedback, 'Unsaved change. Choose Save retention to send it.'); });
    document.addEventListener('visibilitychange', () => {
      if (!visible()) {
        state.viewEpoch += 1; abortReads('history'); abortReads('tiles'); abortReads('metadata');
        if (state.configWorld && !state.configSaving) closeConfiguration();
        state.historyPromise = null; state.tilesPromise = null; state.metadataPromise = null;
        state.lastTilesAttempt = -Infinity; state.lastHistoryAttempt = -Infinity;
      } else if (active()) { void update(); }
    });
    window.addEventListener('hashchange', () => {
      const route = parseRoute(window.location?.hash);
      if (route && active()) openRoute(route); else if (state.open) close();
    });
    let resizing = false;
    window.addEventListener('resize', () => {
      if (resizing || !active() || !visible() || !state.open) return;
      resizing = true;
      const repaint = () => { resizing = false; if (active() && visible() && state.open) renderCharts(); };
      if (window.requestAnimationFrame) window.requestAnimationFrame(repaint); else repaint();
    });
  }

  function attach(options) {
    if (!record(options) || !['api', 'getWorlds', 'isActive'].every((key) => typeof options[key] === 'function')) throw new TypeError('Metrics requires api, getWorlds and isActive functions.');
    if (environment) clear();
    environment = options;
    const byId = (id) => document.getElementById(id);
    if (!byId('metrics-section')) return controller;
    ui = {
      section: byId('metrics-section'), nav: byId('metrics-button'), close: byId('metrics-close'), title: byId('metrics-title'), scope: byId('metrics-scope'),
      status: byId('metrics-status'), error: byId('metrics-error'), summary: byId('metrics-summary'), charts: byId('metrics-charts'), rangeHelp: byId('metrics-range-help'),
      filterForm: byId('metrics-filter-form'), world: byId('metrics-world'), agent: byId('metrics-agent'), conversation: byId('metrics-conversation'),
      from: byId('metrics-from'), to: byId('metrics-to'), presets: byId('metrics-presets'), clearFilters: byId('metrics-clear-filters'), dimensionsHelp: byId('metrics-dimensions-help'),
      selected: byId('metrics-selected'), add: byId('metrics-add'), addButton: byId('metrics-add-button'), metricCount: byId('metrics-count'), refresh: byId('metrics-refresh'),
      retentionForm: byId('metrics-retention-form'), retention: byId('metrics-retention'), retentionSave: byId('metrics-retention-save'), retentionCheck: byId('metrics-retention-check'),
      retentionFeedback: byId('metrics-retention-feedback'), storage: byId('metrics-storage'),
      configDialog: byId('metrics-config-dialog'), configForm: byId('metrics-config-form'), configChoices: byId('metrics-config-choices'), configName: byId('metrics-config-world'),
      configCount: byId('metrics-config-count'), configSave: byId('metrics-config-save'), configClose: byId('metrics-config-close'), configCancel: byId('metrics-config-cancel'),
      configError: byId('metrics-config-error'), configFeedback: byId('metrics-config-feedback'),
    };
    bindUi();
    ui.nav.hidden = !active();
    return controller;
  }

  const controller = Object.freeze({ attach, tile, update, clear, open, close, helpers });
  window.__hub = window.__hub || {};
  window.__hub.metrics = controller;
}());
