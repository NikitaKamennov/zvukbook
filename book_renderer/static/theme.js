// Runs before styles to apply the saved palette before the first page paint.
(() => {
  'use strict';
  const key = 'zvukbook-theme';
  const root = document.documentElement;
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  const valid = value => value === 'dark' || value === 'light';
  let preference = null;
  try {
    const saved = localStorage.getItem(key);
    if (valid(saved)) preference = saved;
  } catch (_) {
    // A blocked browser store must not prevent the UI from opening.
  }

  function syncWindow() {
    if (window.location.origin !== 'http://wails.localhost') return;
    const runtime = window.runtime;
    const method = root.dataset.theme === 'dark' ? 'WindowSetDarkTheme' : 'WindowSetLightTheme';
    if (runtime && typeof runtime[method] === 'function') {
      try { runtime[method](); } catch (_) { /* The page can paint before Wails is ready. */ }
    }
  }

  function apply() {
    const dark = (preference || (system.matches ? 'dark' : 'light')) === 'dark';
    root.dataset.theme = dark ? 'dark' : 'light';
    const toggle = document.getElementById('theme-toggle');
    if (toggle) {
      toggle.setAttribute('aria-checked', String(dark));
      toggle.title = dark ? 'Включить светлую тему' : 'Включить тёмную тему';
    }
    syncWindow();
  }

  apply();
  document.addEventListener('DOMContentLoaded', () => {
    const toggle = document.getElementById('theme-toggle');
    if (toggle) toggle.addEventListener('click', () => {
      preference = root.dataset.theme === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem(key, preference); } catch (_) { /* Use the choice for this window. */ }
      apply();
    });
    apply();
  }, {once: true});
  window.addEventListener('load', syncWindow, {once: true});
  system.addEventListener('change', () => { if (preference === null) apply(); });
  window.addEventListener('storage', event => {
    if (event.key !== key && event.key !== null) return;
    preference = valid(event.newValue) ? event.newValue : null;
    apply();
  });
})();
