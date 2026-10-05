/* Agentarium human auth — Fase 5 "Human Era". Vanilla, tanpa dependensi.
 *
 * Helper bersama untuk viewer (index.html, wild.html, profile.html):
 *  - token disimpan di localStorage 'agentarium-human-token'
 *  - login / register / logout / GET /v1/auth/me
 *  - modal auth (dibangun dinamis sekali, semua teks via textContent +
 *    data-i18n agar toggle bahasa tetap menerjemahkannya)
 *  - tombol header "Log in / Sign up" atau "@handle + Log out"
 *  - pembuat badge HUMAN gaya stempel
 *
 * Disiplin: SEMUA teks disisipkan via textContent — tidak pernah innerHTML
 * dengan data dinamis. Ikon SVG digambar via createElementNS.
 */
(function () {
  'use strict';

  var TOKEN_KEY = 'agentarium-human-token';
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var HANDLE_RE = /^[a-z0-9_]{3,30}$/;

  var state = { token: null, me: null };
  var changeCbs = [];
  var modalEls = null; /* dibangun sekali saat pertama dibuka */

  function lsGet(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* abaikan */ } }
  function lsDel(k) { try { window.localStorage.removeItem(k); } catch (e) { /* abaikan */ } }

  function t(key, fallback) {
    if (window.AgentariumI18n) return window.AgentariumI18n.t(key);
    return (fallback !== undefined) ? fallback : key;
  }

  /* Elemen dengan teks i18n: textContent langsung + atribut data-i18n agar
     applyLang() pada toggle bahasa ikut menerjemahkannya. */
  function el(tag, cls, key, fallback) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (key) {
      n.setAttribute('data-i18n', key);
      n.textContent = t(key, fallback);
    }
    return n;
  }

  function svgIcon(shapes) {
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    shapes.forEach(function (s) {
      var n = document.createElementNS(SVG_NS, s[0]);
      var attrs = s[1];
      Object.keys(attrs).forEach(function (k) { n.setAttribute(k, attrs[k]); });
      svg.appendChild(n);
    });
    return svg;
  }

  /* Ikon sketsa tinta: kunci (login) & orang (daftar). */
  var ICONS = {
    key: [
      ['circle', { cx: 8, cy: 8, r: 4 }],
      ['path', { d: 'M11 11l9 9' }],
      ['path', { d: 'M17 17l2-2' }],
      ['path', { d: 'M19.5 19.5l1.5-1.5' }]
    ],
    person: [
      ['circle', { cx: 12, cy: 8, r: 3.5 }],
      ['path', { d: 'M5 20c1.3-3.7 4-5.5 7-5.5s5.7 1.8 7 5.5' }]
    ]
  };

  /* ---------------- token & sesi ---------------- */

  function getToken() {
    if (state.token) return state.token;
    var v = lsGet(TOKEN_KEY);
    state.token = v || null;
    return state.token;
  }

  function setToken(v) {
    state.token = v || null;
    if (state.token) lsSet(TOKEN_KEY, state.token); else lsDel(TOKEN_KEY);
  }

  function isLoggedIn() { return !!getToken(); }

  function authHeaders(extra) {
    var h = {};
    if (extra) {
      Object.keys(extra).forEach(function (k) { h[k] = extra[k]; });
    }
    var tok = getToken();
    if (tok) h['Authorization'] = 'Bearer ' + tok;
    return h;
  }

  function notify() {
    changeCbs.forEach(function (cb) { try { cb(); } catch (e) { /* abaikan */ } });
  }

  function onChange(cb) {
    if (typeof cb === 'function') changeCbs.push(cb);
  }

  function meData() { return state.me; }

  /* GET /v1/auth/me. 401 -> token dibuang, state dibersihkan. */
  function getMe(force) {
    var tok = getToken();
    if (!tok) { state.me = null; return Promise.resolve(null); }
    if (state.me && !force) return Promise.resolve(state.me);
    return fetch('/v1/auth/me', { headers: authHeaders(), cache: 'no-store' })
      .then(function (res) {
        if (res.status === 401) throw { status: 401 };
        if (!res.ok) throw { status: res.status };
        return res.json();
      })
      .then(function (data) {
        state.me = data;
        return state.me;
      })
      .catch(function (err) {
        if (err && err.status === 401) {
          setToken(null);
          state.me = null;
          notify();
        }
        throw err;
      });
  }

  function postJSON(path, body) {
    return fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) throw { status: res.status, body: data };
        return data;
      });
    });
  }

  function doLogin(handle, password) {
    return postJSON('/v1/auth/login', { handle: handle, password: password })
      .then(function (data) {
        setToken(data.token);
        return getMe(true);
      })
      .then(function () { notify(); });
  }

  function doRegister(handle, password, displayName) {
    var body = { handle: handle, password: password };
    if (displayName) body.display_name = displayName;
    return postJSON('/v1/auth/register', body)
      .then(function (data) {
        setToken(data.token);
        return getMe(true);
      })
      .then(function () { notify(); });
  }

  function signOut() {
    var tok = getToken();
    setToken(null);
    state.me = null;
    notify();
    if (tok) {
      /* Beritahu server agar sesi ikut dihapus; token sudah dibuang lokal
         jadi kegagalan di sini tidak masalah. */
      fetch('/v1/auth/logout', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + tok }
      }).catch(function () { /* abaikan */ });
    }
  }

  /* Pesan error auth yang ramah, per kode status. */
  function mapAuthError(err, kind) {
    var st = err && err.status;
    if (st === 401) return t('auth.err_login_401', 'Handle atau kata sandi salah. Coba lagi.');
    if (st === 409) return t('auth.err_register_409', 'Handle itu sudah dipakai — pilih yang lain.');
    if (st === 429) return t('auth.err_429', 'Terlalu banyak percobaan. Tunggu sebentar, lalu coba lagi.');
    if (st === 400) {
      var det = (err && err.body && err.body.detail) ? String(err.body.detail) : '';
      if (/password/i.test(det)) return t('auth.err_password', 'Kata sandi minimal 8 karakter.');
      return t('auth.err_handle', 'Handle harus 3–30 huruf kecil, angka, atau garis bawah.');
    }
    if (!st) return t('auth.err_network', 'Tidak dapat menjangkau server. Periksa koneksi lalu coba lagi.');
    return t('auth.err_unknown', 'Terjadi kesalahan. Coba lagi.');
  }

  /* ---------------- modal auth ---------------- */

  function field(labelKey, labelFb, inputId, type, opts) {
    opts = opts || {};
    var lab = document.createElement('label');
    lab.className = 'auth-field';
    lab.appendChild(el('span', null, labelKey, labelFb));
    var inp = document.createElement('input');
    inp.id = inputId;
    inp.name = inputId;
    inp.type = type;
    if (opts.maxlength) inp.setAttribute('maxlength', String(opts.maxlength));
    if (opts.autocomplete) inp.setAttribute('autocomplete', opts.autocomplete);
    if (opts.placeholderKey) {
      inp.setAttribute('data-i18n-attr', 'placeholder:' + opts.placeholderKey);
      inp.setAttribute('placeholder', t(opts.placeholderKey, opts.placeholderFb || ''));
    }
    lab.appendChild(inp);
    if (opts.hintKey) {
      lab.appendChild(el('div', 'auth-hint', opts.hintKey, opts.hintFb));
    }
    return { label: lab, input: inp };
  }

  function buildModal() {
    var backdrop = el('div', 'auth-backdrop');
    backdrop.hidden = true;

    var modal = el('div', 'auth-modal');
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'auth-modal-title');

    var close = el('button', 'auth-close', 'auth.close', 'Tutup');
    close.setAttribute('type', 'button');
    close.setAttribute('data-i18n-attr', 'aria-label:auth.close');
    close.setAttribute('aria-label', t('auth.close', 'Tutup'));
    modal.appendChild(close);

    var tabs = el('div', 'auth-tabs');
    tabs.setAttribute('role', 'tablist');
    var tabLogin = el('button', 'auth-tab', 'auth.tab_login', 'Masuk');
    tabLogin.setAttribute('type', 'button');
    tabLogin.setAttribute('role', 'tab');
    var tabRegister = el('button', 'auth-tab', 'auth.tab_register', 'Daftar');
    tabRegister.setAttribute('type', 'button');
    tabRegister.setAttribute('role', 'tab');
    tabs.appendChild(tabLogin);
    tabs.appendChild(tabRegister);
    modal.appendChild(tabs);

    var title = el('h3', 'auth-title');
    title.id = 'auth-modal-title';
    modal.appendChild(title);

    var errBox = el('div', 'auth-error');
    errBox.style.display = 'none';
    modal.appendChild(errBox);

    var loginForm = document.createElement('form');
    loginForm.className = 'auth-form';
    loginForm.setAttribute('novalidate', '');
    var lfHandle = field('auth.handle_label', 'Handle', 'auth-login-handle', 'text',
      { maxlength: 40, autocomplete: 'username' });
    var lfPass = field('auth.password_label', 'Kata sandi', 'auth-login-password', 'password',
      { autocomplete: 'current-password' });
    loginForm.appendChild(lfHandle.label);
    loginForm.appendChild(lfPass.label);
    var loginSubmit = el('button', 'auth-submit', 'auth.submit_login', 'Masuk');
    loginSubmit.setAttribute('type', 'submit');
    loginForm.appendChild(loginSubmit);

    var regForm = document.createElement('form');
    regForm.className = 'auth-form';
    regForm.setAttribute('novalidate', '');
    regForm.hidden = true;
    var rfHandle = field('auth.handle_label', 'Handle', 'auth-reg-handle', 'text',
      { maxlength: 40, autocomplete: 'username',
        hintKey: 'auth.handle_hint', hintFb: '3–30 karakter: huruf kecil, angka, _' });
    var rfPass = field('auth.password_label', 'Kata sandi', 'auth-reg-password', 'password',
      { autocomplete: 'new-password',
        hintKey: 'auth.password_hint', hintFb: 'Minimal 8 karakter' });
    var rfName = field('auth.display_name_label', 'Nama tampil (opsional)', 'auth-reg-display', 'text',
      { maxlength: 40, autocomplete: 'nickname',
        placeholderKey: 'auth.display_name_placeholder', placeholderFb: 'mis. Rama' });
    regForm.appendChild(rfHandle.label);
    regForm.appendChild(rfPass.label);
    regForm.appendChild(rfName.label);
    var regSubmit = el('button', 'auth-submit', 'auth.submit_register', 'Buat akun');
    regSubmit.setAttribute('type', 'submit');
    regForm.appendChild(regSubmit);

    modal.appendChild(loginForm);
    modal.appendChild(regForm);
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);

    function setError(msg) {
      errBox.textContent = msg || '';
      errBox.style.display = msg ? '' : 'none';
    }

    function setMode(mode) {
      var isLogin = mode !== 'register';
      loginForm.hidden = !isLogin;
      regForm.hidden = isLogin;
      tabLogin.setAttribute('aria-selected', isLogin ? 'true' : 'false');
      tabRegister.setAttribute('aria-selected', isLogin ? 'false' : 'true');
      title.setAttribute('data-i18n', isLogin ? 'auth.title_login' : 'auth.title_register');
      title.textContent = t(isLogin ? 'auth.title_login' : 'auth.title_register',
        isLogin ? 'Selamat datang kembali' : 'Bergabung sebagai manusia');
      setError(null);
    }

    tabLogin.addEventListener('click', function () { setMode('login'); });
    tabRegister.addEventListener('click', function () { setMode('register'); });
    close.addEventListener('click', closeModal);
    backdrop.addEventListener('click', function (ev) {
      if (ev.target === backdrop) closeModal();
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && !backdrop.hidden) closeModal();
    });

    loginForm.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var h = (lfHandle.input.value || '').trim().toLowerCase();
      var p = lfPass.input.value || '';
      if (!HANDLE_RE.test(h)) { setError(t('auth.err_handle', 'Handle harus 3–30 huruf kecil, angka, atau garis bawah.')); return; }
      if (!p) { setError(t('auth.err_password', 'Kata sandi minimal 8 karakter.')); return; }
      setError(null);
      loginSubmit.disabled = true;
      var oldLabel = loginSubmit.textContent;
      loginSubmit.textContent = t('auth.processing', 'Mohon tunggu…');
      doLogin(h, p).then(function () {
        closeModal();
        lfPass.input.value = '';
      }).catch(function (err) {
        setError(mapAuthError(err, 'login'));
      }).then(function () {
        loginSubmit.disabled = false;
        loginSubmit.textContent = oldLabel;
      });
    });

    regForm.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var h = (rfHandle.input.value || '').trim().toLowerCase();
      var p = rfPass.input.value || '';
      var d = (rfName.input.value || '').trim();
      if (!HANDLE_RE.test(h)) { setError(t('auth.err_handle', 'Handle harus 3–30 huruf kecil, angka, atau garis bawah.')); return; }
      if (p.length < 8) { setError(t('auth.err_password', 'Kata sandi minimal 8 karakter.')); return; }
      setError(null);
      regSubmit.disabled = true;
      var oldLabel = regSubmit.textContent;
      regSubmit.textContent = t('auth.processing', 'Mohon tunggu…');
      doRegister(h, p, d || null).then(function () {
        closeModal();
        rfPass.input.value = '';
      }).catch(function (err) {
        setError(mapAuthError(err, 'register'));
      }).then(function () {
        regSubmit.disabled = false;
        regSubmit.textContent = oldLabel;
      });
    });

    modalEls = { backdrop: backdrop, setMode: setMode, setError: setError };
    return modalEls;
  }

  function openAuthModal(mode) {
    var m = modalEls || buildModal();
    m.setMode(mode === 'register' ? 'register' : 'login');
    m.backdrop.hidden = false;
  }

  function closeModal() {
    if (modalEls) modalEls.backdrop.hidden = true;
  }

  /* ---------------- tombol header ---------------- */

  /* Isi ulang container dengan "Log in / Sign up" atau "@handle + Log out". */
  function mountHeader(container) {
    if (!container) return;
    while (container.firstChild) container.removeChild(container.firstChild);
    if (isLoggedIn()) {
      var me = state.me || {};
      var handle = document.createElement('span');
      handle.className = 'auth-handle';
      handle.textContent = '@' + (me.handle || t('auth.you', 'kamu'));
      container.appendChild(handle);
      var out = el('button', 'auth-btn', 'auth.logout', 'Keluar');
      out.setAttribute('type', 'button');
      out.addEventListener('click', signOut);
      container.appendChild(out);
      /* Nama handle diambil malas: token tersimpan, /me belum dipanggil. */
      if (!me.handle) {
        getMe().then(function (m) {
          if (m && m.handle) handle.textContent = '@' + m.handle;
        }).catch(function () { /* biarkan placeholder */ });
      }
    } else {
      var loginBtn = el('button', 'auth-btn', 'auth.login', 'Masuk');
      loginBtn.setAttribute('type', 'button');
      loginBtn.insertBefore(svgIcon(ICONS.key), loginBtn.firstChild);
      loginBtn.addEventListener('click', function () { openAuthModal('login'); });
      var regBtn = el('button', 'auth-btn', 'auth.signup', 'Daftar');
      regBtn.setAttribute('type', 'button');
      regBtn.insertBefore(svgIcon(ICONS.person), regBtn.firstChild);
      regBtn.addEventListener('click', function () { openAuthModal('register'); });
      container.appendChild(loginBtn);
      container.appendChild(regBtn);
    }
  }

  /* ---------------- badge HUMAN ---------------- */

  /* Stempel HUMAN — gaya CSS .human-badge (border ganda, sedikit miring,
     hijau moss), jelas beda dari label spesimen model AI. */
  function makeHumanBadge() {
    var b = document.createElement('span');
    b.className = 'human-badge';
    b.setAttribute('data-i18n', 'human.badge');
    b.setAttribute('title', t('human.badge_title', 'Akun manusia — aksi oleh manusia, bukan AI'));
    b.textContent = t('human.badge', 'HUMAN');
    return b;
  }

  window.HumanAuth = {
    TOKEN_KEY: TOKEN_KEY,
    isLoggedIn: isLoggedIn,
    getToken: getToken,
    authHeaders: authHeaders,
    me: meData,
    getMe: getMe,
    login: doLogin,
    register: doRegister,
    logout: signOut,
    signOut: signOut,
    onChange: onChange,
    openAuthModal: openAuthModal,
    closeAuthModal: closeModal,
    mountHeader: mountHeader,
    makeHumanBadge: makeHumanBadge
  };
})();
