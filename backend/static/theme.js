/* Agentarium theme — "night field notes". Vanilla, tanpa dependensi.
 *
 * Konvensi (lihat I18N_CONVENTION.md, section THEME):
 *  - Semua warna halaman memakai CSS variables dari :root (light) dan
 *    [data-theme="dark"] (gelap). Jangan hardcode warna di CSS baru.
 *  - Default: 'dark'. Bila ada pilihan tersimpan di localStorage ('agentarium-theme')
 *    -> pakai itu. Bila tidak -> dark, KECUALI OS eksplisit light
 *    (matchMedia '(prefers-color-scheme: light)').
 *  - Diterapkan via document.documentElement.setAttribute('data-theme', ...).
 *  - <meta name="theme-color"> diupdate mengikuti tema.
 *  - Toggle: <button data-theme-toggle> berisi dua ikon SVG:
 *      <svg class="icon-moon"> (tampil saat tema light) dan
 *      <svg class="icon-sun" hidden> (tampil saat tema dark).
 *  - initTheme() berjalan otomatis saat DOMContentLoaded; juga tersedia manual.
 */
(function () {
  'use strict';

  var THEME_KEY = 'agentarium-theme';
  var META_COLORS = { dark: '#14160f', light: '#f4f1ea' };

  function storedTheme() {
    try {
      var v = window.localStorage.getItem(THEME_KEY);
      if (v === 'dark' || v === 'light') return v;
    } catch (e) { /* abaikan: pakai preferensi */ }
    return null;
  }

  function preferredTheme() {
    var s = storedTheme();
    if (s) return s;
    try {
      if (window.matchMedia &&
          window.matchMedia('(prefers-color-scheme: light)').matches) {
        return 'light';
      }
    } catch (e) { /* abaikan */ }
    return 'dark'; /* default: night field notes */
  }

  function applyTheme(theme) {
    if (theme !== 'dark' && theme !== 'light') theme = 'dark';
    document.documentElement.setAttribute('data-theme', theme);
    try { window.localStorage.setItem(THEME_KEY, theme); } catch (e) { /* abaikan */ }

    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta && meta.setAttribute) meta.setAttribute('content', META_COLORS[theme]);

    var toggles = document.querySelectorAll('[data-theme-toggle]');
    for (var i = 0; i < toggles.length; i++) {
      var moon = toggles[i].querySelector('.icon-moon');
      var sun = toggles[i].querySelector('.icon-sun');
      if (moon) moon.hidden = (theme !== 'light'); /* bulan = klik untuk gelap */
      if (sun) sun.hidden = (theme !== 'dark');     /* matahari = klik untuk terang */
    }
  }

  function toggleTheme() {
    var cur = document.documentElement.getAttribute('data-theme');
    applyTheme(cur === 'dark' ? 'light' : 'dark');
  }

  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
  }

  function initTheme() {
    var toggles = document.querySelectorAll('[data-theme-toggle]');
    for (var i = 0; i < toggles.length; i++) {
      (function (btn) {
        btn.addEventListener('click', toggleTheme);
      })(toggles[i]);
    }
    /* Sinkronkan ulang setelah DOM siap (ikon toggle sudah ada). */
    applyTheme(document.documentElement.getAttribute('data-theme') || preferredTheme());
  }

  window.AgentariumTheme = {
    apply: applyTheme,
    toggle: toggleTheme,
    init: initTheme,
    current: currentTheme
  };

  /* Terapkan sedini mungkin (script dimuat di <head>) agar tidak ada flash tema. */
  applyTheme(preferredTheme());

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initTheme);
  } else {
    initTheme();
  }
})();
