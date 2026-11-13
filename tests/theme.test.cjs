'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const staticDir = path.resolve(__dirname, '../hub/static');

function themeHarness(options = {}) {
  const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');
  const inline = html.match(/<script\b[^>]*id="theme-init"[^>]*>([\s\S]*?)<\/script>/);
  assert.ok(inline, 'The no-flash bootstrap must be an inline script with id=theme-init.');
  const attributes = new Map();
  const clicks = new Map();
  const mediaListeners = new Set();
  const writes = [];
  const label = { textContent: '' };
  const icon = { textContent: '' };
  const button = {
    attributes: new Map(),
    setAttribute(name, value) { this.attributes.set(name, String(value)); },
    addEventListener(name, callback) { clicks.set(name, callback); },
    querySelector(selector) { return selector === '[data-theme-label]' ? label : icon; },
  };
  const root = {
    setAttribute(name, value) {
      assert.equal(name, 'data-theme', 'Themes must not mutate root classes or inline styles.');
      attributes.set(name, String(value));
    },
    getAttribute(name) { return attributes.get(name) ?? null; },
    removeAttribute(name) { attributes.delete(name); },
    classList: { add() { assert.fail('Do not use theme classes.'); }, remove() { assert.fail('Do not use theme classes.'); } },
    style: new Proxy({}, { set() { assert.fail('Do not use inline theme colors.'); } }),
  };
  let saved = options.saved ?? null;
  const storage = {
    getItem(key) {
      assert.equal(key, 'botnexushub.theme');
      if (options.readThrows) throw new Error('Storage read denied');
      return saved;
    },
    setItem(key, value) {
      assert.equal(key, 'botnexushub.theme');
      if (options.writeThrows) throw new Error('Storage write denied');
      saved = value;
      writes.push(value);
    },
  };
  const media = { matches: Boolean(options.dark) };
  if (options.legacyMedia) {
    media.addListener = (callback) => mediaListeners.add(callback);
  } else {
    media.addEventListener = (type, callback) => {
      assert.equal(type, 'change');
      mediaListeners.add(callback);
    };
  }
  const sandbox = {
    document: { documentElement: root, getElementById(id) { return id === 'theme-toggle' ? button : null; } },
    console,
  };
  sandbox.window = sandbox;
  if (!options.noMedia) sandbox.matchMedia = (query) => {
    assert.equal(query, '(prefers-color-scheme: dark)');
    return media;
  };
  Object.defineProperty(sandbox, 'localStorage', {
    get() {
      if (options.storageGetterThrows) throw new Error('Storage getter denied');
      return storage;
    },
  });
  vm.createContext(sandbox);
  vm.runInContext(inline[1], sandbox, { filename: 'index.html#theme-init' });
  const bootstrapTheme = root.getAttribute('data-theme');
  vm.runInContext(fs.readFileSync(path.join(staticDir, 'theme.js'), 'utf8'), sandbox, { filename: 'theme.js' });
  assert.ok(sandbox.__hub?.theme, 'Expose the real theme controller as window.__hub.theme.');
  return {
    theme: sandbox.__hub.theme,
    bootstrapTheme,
    root,
    button,
    label,
    icon,
    writes,
    click() { clicks.get('click')(); },
    changeSystem(dark) {
      media.matches = dark;
      for (const listener of mediaListeners) listener({ matches: dark });
    },
  };
}

test('1. System mode starts light on a light OS, including the no-flash bootstrap', () => {
  const h = themeHarness();
  assert.equal(h.bootstrapTheme, null);
  assert.equal(h.theme.mode, 'system');
  assert.equal(h.theme.resolved, 'light');
  assert.equal(h.root.getAttribute('data-theme'), null);
  assert.match(h.button.attributes.get('aria-label'), /system.*light/i);
});

test('2. System mode starts dark on a dark OS before the main script runs', () => {
  const h = themeHarness({ dark: true });
  assert.equal(h.bootstrapTheme, null);
  assert.equal(h.theme.mode, 'system');
  assert.equal(h.theme.resolved, 'dark');
  assert.equal(h.root.getAttribute('data-theme'), null);
});

test('3. Saved light and dark modes override the OS in both theme scripts', () => {
  for (const saved of ['light', 'dark']) {
    const h = themeHarness({ saved, dark: saved === 'light' });
    assert.equal(h.bootstrapTheme, saved);
    assert.equal(h.theme.mode, saved);
    assert.equal(h.theme.resolved, saved);
  }
});

test('4. Missing, invalid and explicit system preferences safely follow the OS', () => {
  for (const saved of [null, '', 'system', 'sepia', 'DARK', '<img src=x onerror=alert(1)>', '{"mode":"dark"}']) {
    const h = themeHarness({ saved, dark: true });
    assert.equal(h.bootstrapTheme, null);
    assert.equal(h.theme.mode, 'system');
    assert.equal(h.theme.resolved, 'dark');
  }
});

test('5. The real button cycles system → light → dark → system and persists only valid modes', () => {
  const h = themeHarness({ dark: true });
  for (const expected of ['light', 'dark', 'system']) {
    h.click();
    assert.equal(h.theme.mode, expected);
    assert.equal(h.theme.resolved, expected === 'system' ? 'dark' : expected);
    assert.match(h.label.textContent, new RegExp(expected, 'i'));
    assert.match(h.button.attributes.get('aria-label'), /switch to/i);
  }
  assert.deepEqual(h.writes, ['light', 'dark', 'system']);
  h.theme.setMode('untrusted');
  assert.equal(h.theme.mode, 'system');
  assert.equal(h.writes.at(-1), 'system');
});

test('6. Live OS changes update system mode, but never override explicit light or dark', () => {
  const h = themeHarness();
  h.changeSystem(true);
  assert.equal(h.theme.resolved, 'dark');
  assert.equal(h.root.getAttribute('data-theme'), null);
  assert.match(h.button.attributes.get('aria-label'), /system.*dark/i);
  h.theme.setMode('light');
  h.changeSystem(false);
  h.changeSystem(true);
  assert.equal(h.theme.resolved, 'light');
  h.theme.setMode('dark');
  h.changeSystem(false);
  assert.equal(h.theme.resolved, 'dark');
  h.theme.setMode('system');
  assert.equal(h.theme.resolved, 'light');
});

test('7. Denied storage getters, reads and writes cannot break startup or theme changes', () => {
  for (const failure of [{ storageGetterThrows: true }, { readThrows: true, writeThrows: true }, { writeThrows: true, saved: 'dark' }]) {
    const h = themeHarness({ ...failure, dark: false });
    assert.doesNotThrow(() => { h.click(); h.click(); h.click(); });
    assert.ok([null, 'light', 'dark'].includes(h.root.getAttribute('data-theme')));
  }
});

test('8. Missing matchMedia falls back to light, and older media listeners stay live', () => {
  const missing = themeHarness({ noMedia: true });
  assert.equal(missing.bootstrapTheme, null);
  assert.equal(missing.theme.resolved, 'light');
  missing.theme.setMode('dark');
  assert.equal(missing.theme.resolved, 'dark');
  const legacy = themeHarness({ legacyMedia: true });
  legacy.changeSystem(true);
  assert.equal(legacy.theme.resolved, 'dark');
});
