(function () {
  'use strict';

  const key = 'botnexushub.theme';
  const modes = ['system', 'light', 'dark'];
  const root = document.documentElement;
  let mode = 'system';
  let media = null;

  try {
    const saved = window.localStorage.getItem(key);
    if (modes.includes(saved)) mode = saved;
  } catch (_) {
    // Theme changes still work when a browser blocks storage.
  }
  try {
    if (typeof window.matchMedia === 'function') media = window.matchMedia('(prefers-color-scheme: dark)');
  } catch (_) {
    // A light system fallback also works in restricted browser contexts.
  }

  const button = document.getElementById('theme-toggle');
  const label = button && button.querySelector('[data-theme-label]');
  const icon = button && button.querySelector('[data-theme-icon]');
  const names = { system: 'System', light: 'Light', dark: 'Dark' };
  const icons = { system: '◐', light: '☼', dark: '☾' };

  function resolved() {
    return mode === 'system' ? (media && media.matches ? 'dark' : 'light') : mode;
  }

  function render() {
    const actual = resolved();
    const next = modes[(modes.indexOf(mode) + 1) % modes.length];
    if (mode === 'system') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', mode);
    if (label) label.textContent = names[mode];
    if (icon) icon.textContent = icons[mode];
    if (button) {
      button.setAttribute('aria-label', `Theme: ${mode}${mode === 'system' ? ` (${actual})` : ''}. Switch to ${next}.`);
      button.setAttribute('title', `${names[mode]} theme${mode === 'system' ? ` · currently ${actual}` : ''}. Switch to ${next}.`);
    }
  }

  function setMode(value) {
    mode = modes.includes(value) ? value : 'system';
    try { window.localStorage.setItem(key, mode); } catch (_) { /* A preference is not required for the control to work. */ }
    render();
    return mode;
  }

  function cycle() {
    return setMode(modes[(modes.indexOf(mode) + 1) % modes.length]);
  }

  if (media) {
    const onChange = () => { if (mode === 'system') render(); };
    if (typeof media.addEventListener === 'function') media.addEventListener('change', onChange);
    else if (typeof media.addListener === 'function') media.addListener(onChange);
  }
  if (button) button.addEventListener('click', cycle);
  window.__hub = window.__hub || {};
  window.__hub.theme = Object.freeze({ get mode() { return mode; }, get resolved() { return resolved(); }, setMode, cycle });
  render();
}());
