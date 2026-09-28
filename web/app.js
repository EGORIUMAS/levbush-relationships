/* Levbush Relationships — клиент карты отношений (статический, без сборки).
 * Контракт API: docs/api.md. Работает как сайт (Telegram Login Widget) и как Telegram Mini App. */
(() => {
  'use strict';

  const CFG = window.LEVBUSH || {};
  const QS = new URLSearchParams(location.search);
  const DEMO = QS.get('demo') === '1';
  const TG = window.Telegram && window.Telegram.WebApp;
  const TGUI = !!(TG && TG.initData);          // открыто внутри Telegram как Mini App
  const MINI = TGUI && !DEMO;                   // авторизация через initData
  const LS_KEY = 'levbush.login';
  const FONT = getComputedStyle(document.body).fontFamily || 'system-ui, sans-serif';
  const mqMobile = matchMedia('(max-width: 720px)');
  const mqDark = matchMedia('(prefers-color-scheme: dark)');

  const $ = (sel, root = document) => root.querySelector(sel);

  /* ------------------------------------------------------------------ utils */

  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  // незакрытые управляющие символы направления в никах (maxim("⁧(" прячет U+2067) переворачивают соседний текст
  const bidiClose = (t) => {
    const n = (re) => (t.match(re) || []).length;
    const iso = n(/[\u2066-\u2068]/g) - n(/\u2069/g), emb = n(/[\u202A\u202B\u202D\u202E]/g) - n(/\u202C/g);
    return t + '\u202C'.repeat(Math.max(0, emb)) + '\u2069'.repeat(Math.max(0, iso));
  };
  const esc = (s) => bidiClose(String(s == null ? '' : s)).replace(/[&<>"']/g, (c) => ESC[c]);
  const clamp01 = (x) => Math.max(0, Math.min(1, Number(x) || 0));
  const lkey = (a, b) => (a < b ? `${a}-${b}` : `${b}-${a}`);
  const isMobile = () => mqMobile.matches;

  const nfInt = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
  const nf1 = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const nf2 = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmtN = (n) => nfInt.format(Math.round(Number(n) || 0));
  const fmtS = (s) => (s == null || isNaN(s) ? '—' : Number(s).toFixed(2));

  function plural(n, one, few, many) {
    const a = Math.abs(Math.floor(n)) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b > 1 && b < 5) return few;
    if (b === 1) return one;
    return many;
  }

  function parseDate(v) {
    if (!v) return null;
    // "YYYY-MM-DD" — локальная дата (начало периода), иначе ISO-время
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
    const d = m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(v);
    return isNaN(d) ? null : d;
  }
  const fmtDate = (v) => { const d = parseDate(v); return d ? d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }) : '—'; };
  const fmtDateShort = (v) => { const d = parseDate(v); return d ? d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short', year: 'numeric' }) : '—'; };
  const fmtDateTime = (v) => {
    const d = parseDate(v);
    return d ? d.toLocaleString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
  };

  function relTime(v) {
    const d = parseDate(v);
    if (!d) return '—';
    const s = (Date.now() - d.getTime()) / 1000;
    if (s < 0) return fmtDateShort(v);
    if (s < 60) return 'только что';
    if (s < 3600) { const m = Math.floor(s / 60); return `${m} мин назад`; }
    if (s < 86400) { const h = Math.floor(s / 3600); return `${h} ч назад`; }
    const days = Math.floor(s / 86400);
    if (days === 1) return 'вчера';
    if (days < 7) return `${days} ${plural(days, 'день', 'дня', 'дней')} назад`;
    return fmtDateShort(v);
  }

  /** 95 → «1 мин 35 с» */
  function fmtDur(sec) {
    if (sec == null || isNaN(sec)) return '—';
    sec = Math.round(sec);
    if (sec < 60) return `${sec} с`;
    const m = Math.floor(sec / 60), s = sec % 60;
    if (m < 60) return s ? `${m} мин ${s} с` : `${m} мин`;
    const h = Math.floor(m / 60), mm = m % 60;
    if (h < 24) return mm ? `${h} ч ${mm} мин` : `${h} ч`;
    const d = Math.floor(h / 24), hh = h % 24;
    return hh ? `${d} д ${hh} ч` : `${d} д`;
  }

  /** секунды → «1 г 3 мес 5 д» */
  function fmtSpan(sec) {
    if (sec == null || isNaN(sec)) return '—';
    let days = Math.floor(sec / 86400);
    if (days < 1) return 'меньше дня';
    const y = Math.floor(days / 365.25);
    days -= Math.floor(y * 365.25);
    const mo = Math.floor(days / 30.44);
    days -= Math.floor(mo * 30.44);
    const parts = [];
    if (y) parts.push(`${y} г`);
    if (mo) parts.push(`${mo} мес`);
    if (days) parts.push(`${days} д`);
    return parts.join(' ') || 'меньше дня';
  }

  const fmtHourMin = (h) => {
    if (h == null || isNaN(h)) return '—';
    let total = Math.round(((h % 24) + 24) % 24 * 60);
    if (total >= 1440) total -= 1440;
    return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
  };

  function initials(name) {
    const words = String(name || '?').trim().split(/\s+/).filter(Boolean);
    const first = (w) => (w ? Array.from(w)[0] : '');
    return ((first(words[0]) || '?') + first(words[1])).toUpperCase();
  }

  function hueColor(id) {
    let h = 2166136261;
    const s = String(id);
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    const hue = (h >>> 0) % 360;
    return `hsl(${hue}, 42%, 50%)`;
  }

  const validAvatar = (a) => typeof a === 'string' && /^data:image\/(png|jpe?g|webp|gif|svg\+xml);base64,[A-Za-z0-9+/=\s]+$/i.test(a);
  const validUsername = (u) => typeof u === 'string' && /^[A-Za-z0-9_]{3,32}$/.test(u);

  function avatarHtml(id, name, avatar, size) {
    if (validAvatar(avatar)) return `<img class="av" src="${esc(avatar)}" alt="" style="--s:${size}px" loading="lazy">`;
    return `<span class="av av-ph" style="--s:${size}px;background:${hueColor(id)}" aria-hidden="true">${esc(initials(name))}</span>`;
  }

  const firstName = (name) => String(name || '').trim().split(/\s+/)[0] || '—';

  /* --------------------------------------------- strength colour scale (OKLab) */

  const STOPS = [[0, '#8793a8'], [0.28, '#4a88d6'], [0.56, '#f2b134'], [0.8, '#ef5f3c'], [1, '#d42a78']];
  const hexRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const toLin = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
  const fromLin = (c) => Math.round(Math.max(0, Math.min(1, c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055)) * 255);
  function rgbToOklab([R, G, B]) {
    const r = toLin(R), g = toLin(G), b = toLin(B);
    const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
    const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
    const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
    return [0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
      1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
      0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s];
  }
  function oklabToRgb([L, A, B]) {
    const l = (L + 0.3963377774 * A + 0.2158037573 * B) ** 3;
    const m = (L - 0.1055613458 * A - 0.0638541728 * B) ** 3;
    const s = (L - 0.0894841775 * A - 1.291485548 * B) ** 3;
    return [fromLin(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
      fromLin(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
      fromLin(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s)];
  }
  const LAB_STOPS = STOPS.map(([t, h]) => [t, rgbToOklab(hexRgb(h))]);
  const LUT = Array.from({ length: 101 }, (_, i) => {
    const t = i / 100;
    let j = 0;
    while (j < LAB_STOPS.length - 2 && t > LAB_STOPS[j + 1][0]) j++;
    const [t0, c0] = LAB_STOPS[j], [t1, c1] = LAB_STOPS[j + 1];
    const f = (t - t0) / (t1 - t0);
    return oklabToRgb(c0.map((v, k) => v + (c1[k] - v) * f));
  });
  const strengthRgb = (s) => LUT[Math.round(clamp01(s) * 100)];
  const strengthCss = (s) => { const [r, g, b] = strengthRgb(s); return `rgb(${r},${g},${b})`; };

  function sbarHtml(s, cls = '') {
    return `<div class="sbar ${cls}" role="img" aria-label="Сила связи ${fmtS(s)}"><i style="width:${(clamp01(s) * 100).toFixed(1)}%;background:${strengthCss(s)}"></i></div>`;
  }

  /* ------------------------------------------------------------ markdown */

  let purifyReady = false;
  function md(src) {
    if (!src) return '';
    if (!window.DOMPurify) return `<p>${esc(src).replace(/\n/g, '<br>')}</p>`;
    if (!purifyReady) {
      window.DOMPurify.addHook('afterSanitizeAttributes', (node) => {
        if (node.tagName === 'A') {
          node.setAttribute('target', '_blank');
          node.setAttribute('rel', 'noopener noreferrer');
        }
      });
      purifyReady = true;
    }
    let html;
    try {
      html = window.marked ? window.marked.parse(String(src), { gfm: true, breaks: false, async: false }) : `<p>${esc(src).replace(/\n/g, '<br>')}</p>`;
    } catch (e) {
      html = `<p>${esc(src).replace(/\n/g, '<br>')}</p>`;
    }
    return window.DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ['img', 'style', 'iframe', 'form', 'input', 'button', 'video', 'audio', 'svg', 'math'],
      FORBID_ATTR: ['style'],
    });
  }

  /* ---------------------------------------------------------------- auth */

  function loadLogin() {
    try { const s = localStorage.getItem(LS_KEY); const u = s ? JSON.parse(s) : null; return u && u.id && u.hash ? u : null; } catch (e) { return null; }
  }
  function saveLogin(u) { try { localStorage.setItem(LS_KEY, JSON.stringify(u)); } catch (e) { /* приватный режим */ } }
  function clearLogin() { try { localStorage.removeItem(LS_KEY); } catch (e) { /* ignore */ } }

  function b64utf8(str) {
    const bytes = new TextEncoder().encode(str);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }

  let login = null;
  function authHeader() {
    if (MINI) return 'tma ' + TG.initData;
    if (login) return 'tglogin ' + b64utf8(JSON.stringify(login));
    return null;
  }

  /* ----------------------------------------------------------------- API */

  class ApiError extends Error {
    constructor(status, code, message) { super(message || code || `HTTP ${status}`); this.status = status; this.code = code; }
  }

  const cache = new Map();
  function api(q, params = {}) {
    const qs = new URLSearchParams({ q, ...params }).toString();
    let p = cache.get(qs);
    if (!p) {
      p = DEMO
        ? window.LevbushDemo.api(q, params).catch((e) => { throw new ApiError(e.status || 0, e.code || 'demo', e.message); })
        : fetchApi(qs);
      cache.set(qs, p);
      p.catch(() => cache.delete(qs));
    }
    return p;
  }

  async function fetchApi(qs) {
    if (!CFG.api) throw new ApiError(0, 'config', 'Не задан адрес API (LEVBUSH.api в config.js)');
    const h = authHeader();
    let r;
    try {
      r = await fetch(CFG.api + (CFG.api.includes('?') ? '&' : '?') + qs, { headers: h ? { Authorization: h } : {} });
    } catch (e) {
      throw new ApiError(0, 'network', 'Нет связи с сервером');
    }
    let body = null;
    try { body = await r.json(); } catch (e) { /* не JSON */ }
    if (!r.ok) throw new ApiError(r.status, body && body.error, body && (body.message || body.error));
    if (body == null) throw new ApiError(r.status, 'parse', 'Сервер вернул некорректный ответ');
    return body;
  }

  function errText(e) {
    if (!(e instanceof ApiError)) return 'Что-то пошло не так: ' + (e && e.message ? e.message : e);
    if (e.code === 'network') return 'Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.';
    if (e.code === 'config') return e.message;
    if (e.status === 404) return 'Не найдено.';
    if (e.status === 429) return 'Слишком много запросов. Подождите немного.';
    if (e.status >= 500) return `Ошибка сервера (${e.status}). Попробуйте позже.`;
    return `Ошибка: ${e.message}`;
  }

  /** 401/403 → экран входа или «не участник». Возвращает true, если ошибка обработана. */
  function handleAuthError(e) {
    if (!(e instanceof ApiError)) return false;
    if (e.status === 401) {
      cache.clear();
      closePanel(true);
      if (MINI) {
        showMessage('🔒', 'Не удалось подтвердить вход',
          'Telegram не подтвердил подпись. Закройте мини-приложение и откройте его заново.', [{ label: 'Повторить', fn: start }]);
      } else {
        clearLogin();
        login = null;
        showLogin('Сессия истекла — войдите ещё раз.');
      }
      return true;
    }
    if (e.status === 403) {
      cache.clear();
      closePanel(true);
      const actions = MINI ? [] : [{ label: 'Войти другим аккаунтом', fn: logout, ghost: true }];
      showMessage('🚪', 'Вы не участник группы',
        'Карта доступна только участникам группы. Если вы недавно вступили, попробуйте позже.', actions);
      return true;
    }
    return false;
  }

  /* ------------------------------------------------------------- screens */

  const screenEl = $('#screen');
  const appEl = $('#app');

  const LOGO = `<svg class="screen-logo" viewBox="0 0 72 72" aria-hidden="true">
    <path d="M18 20 L54 27 L31 54 Z" fill="none" stroke="${strengthCss(0.6)}" stroke-width="3" stroke-linejoin="round"/>
    <path d="M18 20 L31 54" stroke="${strengthCss(0.95)}" stroke-width="5" stroke-linecap="round"/>
    <circle cx="18" cy="20" r="10" fill="${strengthCss(0.25)}"/><circle cx="54" cy="27" r="8" fill="${strengthCss(0.8)}"/>
    <circle cx="31" cy="54" r="11" fill="${strengthCss(1)}"/></svg>`;

  function setScreen(html) {
    screenEl.innerHTML = `<div class="screen-card">${html}</div>`;
    screenEl.hidden = false;
  }
  function hideScreen() { screenEl.hidden = true; screenEl.innerHTML = ''; }
  function showLoading(text) { setScreen(`<div class="spinner"></div><p>${esc(text)}</p>`); }

  function showMessage(icon, title, text, actions = []) {
    setScreen(`<div class="screen-icon" aria-hidden="true">${esc(icon)}</div><h1>${esc(title)}</h1><p>${esc(text)}</p>
      <div class="actions">${actions.map((a, i) => `<button type="button" class="btn ${a.ghost ? 'btn-ghost' : ''}" data-i="${i}">${esc(a.label)}</button>`).join('')}</div>`);
    screenEl.querySelectorAll('.actions button').forEach((b) => b.addEventListener('click', () => actions[+b.dataset.i].fn()));
  }

  function showError(text, retry) {
    showMessage('⚠️', 'Не получилось загрузить карту', text, retry ? [{ label: 'Повторить', fn: retry }] : []);
  }

  function showLogin(reason) {
    appEl.hidden = true;
    const bot = String(CFG.bot || '').replace(/^@/, '');
    const botOk = /^[A-Za-z0-9_]{3,64}$/.test(bot);
    const demoHref = `${location.pathname}?demo=1`;
    setScreen(`${LOGO}<h1>Levbush Relationships</h1>
      <p>Карта отношений участников группы. Войдите через Telegram — доступ есть только у участников.</p>
      ${reason ? `<p><b>${esc(reason)}</b></p>` : ''}
      ${botOk ? '<div class="login-widget" id="tg-login"><div class="spinner"></div></div>'
        : `<div class="config-hint"><b>Вход не настроен.</b> Укажите имя бота для Telegram Login Widget
           в <code>web/config.js</code> → <code>LEVBUSH.bot</code> (без @) и привяжите домен сайта к боту
           в @BotFather командой <code>/setdomain</code>.</div>`}
      <p class="small">Посмотреть интерфейс без входа: <a href="${esc(demoHref)}">демо-режим</a>.</p>`);
    if (!botOk) return;
    window.levbushOnAuth = (user) => {
      if (!user || !user.id || !user.hash) return;
      login = user;
      saveLogin(user);
      cache.clear();
      start();
    };
    const box = $('#tg-login');
    const s = document.createElement('script');
    s.async = true;
    s.src = 'https://telegram.org/js/telegram-widget.js?22';
    s.setAttribute('data-telegram-login', bot);
    s.setAttribute('data-size', 'large');
    s.setAttribute('data-radius', '12');
    s.setAttribute('data-userpic', 'true');
    s.setAttribute('data-onauth', 'levbushOnAuth(user)');
    s.onload = () => { const sp = box.querySelector('.spinner'); if (sp) sp.remove(); };
    s.onerror = () => { box.innerHTML = '<p class="empty">Не удалось загрузить виджет входа Telegram. Проверьте соединение.</p>'; };
    box.appendChild(s);
  }

  function logout() {
    clearLogin();
    login = null;
    cache.clear();
    closePanel(true);
    showLogin();
  }

  /* ---------------------------------------------------------- theme */

  const S = {
    G: null, data: null, byId: new Map(), links: [], linkByKey: new Map(),
    minStrength: 0, labels: true, hoverNode: null, hoverLink: null, focus: null, hl: null,
    touch: false, userMoved: false, fitted: false, pendingFocus: null, col: {},
  };

  function readColors() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n, d) => (cs.getPropertyValue(n) || '').trim() || d;
    S.col = { text: v('--text', '#222'), bg: v('--bg', '#fff'), surface: v('--surface', '#fff'), accent: v('--accent', '#2f6fed'), muted: v('--muted', '#888') };
  }

  const TG_MAP = [
    ['bg_color', '--bg'], ['bg_color', '--surface'], ['section_bg_color', '--surface'], ['secondary_bg_color', '--surface-2'],
    ['text_color', '--text'], ['hint_color', '--muted'], ['link_color', '--link'], ['button_color', '--accent'],
    ['button_text_color', '--accent-text'], ['destructive_text_color', '--danger'], ['section_separator_color', '--line'],
  ];
  function applyTgTheme() {
    const p = (TG && TG.themeParams) || {};
    const root = document.documentElement;
    root.dataset.theme = TG.colorScheme === 'dark' ? 'dark' : 'light';
    for (const [k, css] of TG_MAP) {
      const val = p[k];
      if (typeof val === 'string' && /^#[0-9a-f]{3,8}$/i.test(val)) root.style.setProperty(css, val);
    }
    try { TG.setHeaderColor && TG.setHeaderColor('bg_color'); TG.setBackgroundColor && TG.setBackgroundColor('bg_color'); } catch (e) { /* старый клиент */ }
    readColors();
  }

  function setupTelegram() {
    try { TG.ready(); } catch (e) { /* ignore */ }
    try { TG.expand(); } catch (e) { /* ignore */ }
    try { if (TG.disableVerticalSwipes) TG.disableVerticalSwipes(); } catch (e) { /* клиент < 7.7 */ }
    applyTgTheme();
    TG.onEvent('themeChanged', applyTgTheme);
    TG.onEvent('viewportChanged', () => resizeGraph());
    TG.BackButton.onClick(() => panelBack());
  }

  function updateTgBack() {
    if (!TGUI) return;
    try { if (P.stack.length) TG.BackButton.show(); else TG.BackButton.hide(); } catch (e) { /* ignore */ }
  }

  /* ---------------------------------------------------------------- graph */

  function forceCollide(pad) {
    let nodes = [];
    function force(alpha) {
      const n = nodes.length;
      for (let i = 0; i < n; i++) {
        const a = nodes[i];
        for (let j = i + 1; j < n; j++) {
          const b = nodes[j];
          let dx = b.x - a.x, dy = b.y - a.y;
          const min = a.r + b.r + pad;
          let d2 = dx * dx + dy * dy;
          if (d2 >= min * min) continue;
          if (d2 === 0) { dx = (Math.random() - 0.5) * 1e-3; dy = (Math.random() - 0.5) * 1e-3; d2 = dx * dx + dy * dy; }
          const d = Math.sqrt(d2);
          const push = ((min - d) / d) * 0.5 * Math.min(1, alpha * 4 + 0.3);
          dx *= push; dy *= push;
          b.x += dx; b.y += dy; a.x -= dx; a.y -= dy;
        }
      }
    }
    force.initialize = (ns) => { nodes = ns; };
    return force;
  }
  function forcePull(k) {
    let nodes = [];
    function force(alpha) { for (const n of nodes) { n.vx -= n.x * k * alpha; n.vy -= n.y * k * alpha; } }
    force.initialize = (ns) => { nodes = ns; };
    return force;
  }

  function nodeTooltip(n) {
    const sub = [validUsername(n.username) ? '@' + n.username : null, `${fmtN(n.msgs)} ${plural(n.msgs || 0, 'сообщение', 'сообщения', 'сообщений')}`, n.is_member === false ? 'вышел(а)' : null].filter(Boolean).join(' · ');
    return `<div class="tt-title">${esc(n.name)}</div><div class="tt-meta">${esc(sub)}</div>${n.summary ? `<div class="tt-sum">${esc(n.summary)}</div>` : ''}`;
  }
  function linkTooltip(l) {
    const A = S.byId.get(l.a), B = S.byId.get(l.b);
    const meta = [l.kind, l.tone].filter(Boolean).join(' · ');
    return `<div class="tt-title"><span class="tt-dot" style="background:${strengthCss(l.strength)}"></span>${esc(A ? A.name : l.a)} — ${esc(B ? B.name : l.b)}</div>
      <div class="tt-meta">сила ${fmtS(l.strength)}${meta ? ' · ' + esc(meta) : ''}</div>${l.summary ? `<div class="tt-sum">${esc(l.summary)}</div>` : ''}`;
  }

  const linkVisible = (l) => l.strength >= S.minStrength - 1e-9;

  function nodeHl(n) {
    if (!n) return null;
    const nodes = new Set([n.id]), links = new Set();
    for (const l of n.links) if (linkVisible(l)) { links.add(l); nodes.add(l.a); nodes.add(l.b); }
    return { nodes, links, primary: new Set([n.id]) };
  }
  function pairHl(a, b) {
    const l = S.linkByKey.get(lkey(a, b));
    return { nodes: new Set([a, b]), links: new Set(l ? [l] : []), primary: new Set([a, b]) };
  }
  function computeHl() {
    let hl = null;
    if (S.hoverNode) hl = nodeHl(S.hoverNode);
    else if (S.hoverLink) hl = pairHl(S.hoverLink.a, S.hoverLink.b);
    else if (S.focus && S.focus.type === 'relation') hl = pairHl(S.focus.a, S.focus.b);
    else if (S.focus && S.focus.type === 'person') hl = nodeHl(S.byId.get(S.focus.id));
    S.hl = hl;
  }

  function drawNode(n, ctx, k) {
    const hl = S.hl, C = S.col;
    const inHl = !hl || hl.nodes.has(n.id);
    const primary = !!(hl && hl.primary.has(n.id));
    const isMe = S.data && n.id === S.data.me;
    let alpha = n.is_member === false ? 0.5 : 1;
    if (!inHl) alpha *= 0.13;
    ctx.globalAlpha = alpha;
    const r = n.r;

    ctx.beginPath();
    ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
    if (n.imgOk) {
      ctx.save();
      ctx.clip();
      ctx.drawImage(n.img, n.x - r, n.y - r, 2 * r, 2 * r);
      ctx.restore();
    } else {
      ctx.fillStyle = n.color;
      ctx.fill();
      if (r * k > 7) {
        ctx.fillStyle = '#fff';
        ctx.font = `600 ${r * 0.78}px ${FONT}`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(n.initials, n.x, n.y + r * 0.05);
      }
    }

    ctx.beginPath();
    ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
    ctx.lineWidth = (primary ? 3 : isMe ? 2 : 1.2) / k;
    ctx.strokeStyle = primary || isMe ? C.accent : C.surface;
    if (n.is_member === false) ctx.setLineDash([3 / k, 2.5 / k]);
    ctx.stroke();
    ctx.setLineDash([]);

    const showLabel = primary || (S.labels && (k >= 1.6 || (k >= 0.85 && r >= 13) || (hl && inHl && k >= 0.6)));
    if (showLabel) {
      const fs = 12 / k;
      let label = n.name || '';
      if (label.length > 24) label = label.slice(0, 23) + '…';
      ctx.font = `${primary ? 600 : 500} ${fs}px ${FONT}`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      const y = n.y + r + 3 / k;
      ctx.lineJoin = 'round';
      ctx.lineWidth = 3.5 / k;
      ctx.strokeStyle = C.bg;
      ctx.strokeText(label, n.x, y);
      ctx.fillStyle = C.text;
      ctx.fillText(label, n.x, y);
    }
    ctx.globalAlpha = 1;
  }

  function linkColor(l) {
    const [r, g, b] = strengthRgb(l.strength);
    let a = 0.2 + 0.72 * l.strength;
    if (S.hl) a = S.hl.links.has(l) ? 0.95 : a * 0.1;
    return `rgba(${r},${g},${b},${a.toFixed(2)})`;
  }
  function linkWidth(l) {
    const w = 0.5 + 4.5 * l.strength;
    return S.hl && S.hl.links.has(l) ? w + 1 : w;
  }

  function createGraph() {
    const el = $('#graph');
    const G = new window.ForceGraph(el);
    G.width(el.clientWidth).height(el.clientHeight)
      .backgroundColor('rgba(0,0,0,0)')
      .nodeId('id')
      .nodeVal((n) => n.r * n.r)
      .nodeRelSize(1)
      .nodeCanvasObject(drawNode)
      .nodePointerAreaPaint((n, color, ctx, k) => {
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(n.x, n.y, Math.max(n.r + 2 / k, 11 / k), 0, 2 * Math.PI);
        ctx.fill();
      })
      .nodeLabel((n) => (S.touch ? '' : nodeTooltip(n)))
      .linkLabel((l) => (S.touch ? '' : linkTooltip(l)))
      .linkVisibility(linkVisible)
      .linkColor(linkColor)
      .linkWidth(linkWidth)
      .linkHoverPrecision(6)
      .autoPauseRedraw(false)
      .minZoom(0.15)
      .maxZoom(14)
      .warmupTicks(40)
      .cooldownTime(6000)
      .d3VelocityDecay(0.32)
      .onNodeHover((n) => {
        S.hoverNode = n && !S.touch && pointerOnGraph() ? n : null;
        el.style.cursor = n || S.hoverLink ? 'pointer' : '';
        computeHl();
      })
      .onLinkHover((l) => {
        S.hoverLink = l && !S.touch && pointerOnGraph() ? l : null;
        el.style.cursor = l || S.hoverNode ? 'pointer' : '';
        computeHl();
      })
      .onNodeClick((n) => openView({ type: 'person', id: n.id }))
      .onLinkClick((l) => openView({ type: 'relation', a: l.a, b: l.b }))
      .onEngineStop(() => {
        if (!S.fitted && !S.userMoved) { fitGraph(500); }
        S.fitted = true;
        if (S.pendingFocus != null) { const id = S.pendingFocus; S.pendingFocus = null; focusNode(id); }
      });

    G.d3Force('link')
      .distance((l) => 34 + 170 * Math.pow(1 - l.strength, 1.35))
      .strength((l) => (0.04 + 0.9 * l.strength * l.strength) / Math.sqrt(Math.max(1, Math.min(l.source.deg || 1, l.target.deg || 1))));
    G.d3Force('charge').strength((n) => -70 - 7 * n.r).distanceMax(600);
    G.d3Force('collide', forceCollide(4));
    G.d3Force('pull', forcePull(0.015));

    const markMoved = () => { S.userMoved = true; };
    el.addEventListener('wheel', markMoved, { passive: true });
    el.addEventListener('pointerdown', (e) => {
      S.touch = e.pointerType === 'touch';
      if (S.touch) { S.hoverNode = null; S.hoverLink = null; computeHl(); }
      markMoved();
    }, true);
    el.addEventListener('pointermove', (e) => { if (e.pointerType === 'mouse') S.touch = false; }, true);
    el.addEventListener('pointerleave', () => {
      if (!S.hoverNode && !S.hoverLink) return;
      S.hoverNode = null;
      S.hoverLink = null;
      el.style.cursor = '';
      computeHl();
    });
    return G;
  }

  // force-graph проверяет наведение по последней позиции курсора даже когда её закрыла панель
  const ptr = { x: -1, y: -1 };
  document.addEventListener('pointermove', (e) => { ptr.x = e.clientX; ptr.y = e.clientY; }, { passive: true, capture: true });
  function pointerOnGraph() {
    const t = document.elementFromPoint(ptr.x, ptr.y);
    return !!t && $('#graph').contains(t);
  }

  function fitGraph(ms = 500) {
    if (!S.G || !S.links) return;
    const pad = isMobile() ? 36 : 90;
    S.G.zoomToFit(ms, pad);
  }

  function resizeGraph() {
    if (!S.G) return;
    const el = $('#graph');
    S.G.width(el.clientWidth).height(el.clientHeight);
  }

  function panelCovers() { return P.el.classList.contains('open') && !isMobile(); }

  function focusNode(id, minZoom = 2.2) {
    const n = S.byId.get(id);
    if (!S.G || !n) return;
    if (n.x == null) { S.pendingFocus = id; return; }
    const k = Math.max(S.G.zoom(), minZoom);
    const off = panelCovers() ? P.el.offsetWidth / 2 / k : 0;
    S.G.centerAt(n.x + off, n.y, 600);
    S.G.zoom(k, 600);
  }

  function focusPair(a, b) {
    const A = S.byId.get(a), B = S.byId.get(b);
    if (!S.G || !A || !B || A.x == null || B.x == null) return;
    const k = S.G.zoom();
    const off = panelCovers() ? P.el.offsetWidth / 2 / k : 0;
    S.G.centerAt((A.x + B.x) / 2 + off, (A.y + B.y) / 2, 600);
  }

  function setData(data) {
    S.data = data;
    const nodesIn = Array.isArray(data.nodes) ? data.nodes : [];
    const linksIn = Array.isArray(data.links) ? data.links : [];
    const maxLog = Math.log1p(Math.max(1, ...nodesIn.map((n) => n.msgs || 0)));
    const byId = new Map();
    const nodes = nodesIn.map((n) => {
      const o = {
        ...n,
        r: 4 + 14 * (Math.log1p(Math.max(0, n.msgs || 0)) / maxLog),
        initials: initials(n.name),
        color: hueColor(n.id),
        links: [],
        deg: 0,
      };
      if (validAvatar(n.avatar)) {
        const img = new Image();
        img.onload = () => { o.imgOk = true; };
        img.src = n.avatar;
        o.img = img;
      }
      byId.set(n.id, o);
      return o;
    });
    const links = [];
    const seen = new Set();
    for (const l of linksIn) {
      const A = byId.get(l.a), B = byId.get(l.b);
      const key = lkey(l.a, l.b);
      if (!A || !B || l.a === l.b || seen.has(key)) continue;
      seen.add(key);
      const o = { ...l, strength: clamp01(l.strength), source: l.a, target: l.b, key };
      links.push(o);
      A.links.push(o);
      B.links.push(o);
    }
    links.sort((x, y) => x.strength - y.strength); // сильные рисуются поверх
    for (const n of nodes) n.deg = n.links.length;
    S.byId = byId;
    S.links = links;
    S.linkByKey = new Map(links.map((l) => [l.key, l]));
    S.fitted = false;
    S.userMoved = false;
    S.G.graphData({ nodes, links });
    for (const ms of [350, 1500, 3200]) setTimeout(() => { if (!S.userMoved && !S.fitted) fitGraph(ms < 1000 ? 0 : 400); }, ms);
    renderHeader();
    updateLinksCount();
    $('#z-me').hidden = !byId.has(data.me);
  }

  /* --------------------------------------------------------------- header */

  function renderHeader() {
    const g = (S.data && S.data.group) || {};
    const title = $('#g-title');
    const t = g.title || 'Levbush';
    if (validUsername(g.username) && !TGUI) title.innerHTML = `<a href="https://t.me/${esc(g.username)}" target="_blank" rel="noopener">${esc(t)}</a>`;
    else title.textContent = t;
    document.title = `${t} — Levbush Relationships`;

    const nodes = S.data.nodes || [];
    const members = g.members != null ? g.members : nodes.filter((n) => n.is_member !== false).length;
    const parts = [
      `<b>${fmtN(members)}</b> ${plural(members, 'участник', 'участника', 'участников')}`,
      `<b>${fmtN(g.messages)}</b> ${plural(g.messages || 0, 'сообщение', 'сообщения', 'сообщений')}`,
      g.map_updated_at ? `обновлено ${esc(relTime(g.map_updated_at))}` : 'карта ещё не построена',
    ];
    const meta = $('#g-meta');
    meta.innerHTML = parts.join(' · ');
    const tip = [];
    if (g.map_updated_at) tip.push('Карта: ' + fmtDateTime(g.map_updated_at));
    if (g.stats_updated_at) tip.push('Статистика: ' + fmtDateTime(g.stats_updated_at));
    if (g.first_date) tip.push('История с ' + fmtDate(g.first_date));
    meta.title = tip.join('\n');

    const pass = g.pass; if (pass && !pass.note && pass.stage) pass.note = pass.stage;
    const passEl = $('#g-pass');
    if (pass && pass.state === 'running') {
      const total = Math.max(0, pass.total || 0), done = Math.max(0, Math.min(total || Infinity, pass.done || 0));
      const pct = total ? (done / total) * 100 : 0;
      passEl.innerHTML = `<div class="pass-bar" role="progressbar" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${done}"><i style="width:${pct.toFixed(1)}%"></i></div>
        <span class="pass-long">Нейросеть анализирует группу: ${fmtN(done)} из ${fmtN(total)}${pass.note ? ' · ' + esc(pass.note) : ''}</span>
        <span class="pass-short">Анализ: ${fmtN(done)} из ${fmtN(total)}</span>`;
      passEl.title = `Нейросеть анализирует группу: ${done} из ${total}${pass.note ? ' · ' + pass.note : ''}`;
      passEl.hidden = false;
    } else {
      passEl.hidden = true;
    }
    $('#demo-badge').hidden = !DEMO;
    $('#logout').hidden = MINI || DEMO;
  }

  function updateLinksCount() {
    const vis = S.links.filter(linkVisible).length;
    $('#links-count').textContent = `связей: ${fmtN(vis)} из ${fmtN(S.links.length)}`;
  }

  /* --------------------------------------------------------------- search */

  const norm = (s) => String(s || '').toLowerCase().replace(/ё/g, 'е');
  const searchEl = $('#search');
  const resultsEl = $('#search-results');
  let results = [], activeIdx = -1;

  function runSearch() {
    const q = norm(searchEl.value.trim().replace(/^@/, ''));
    if (!q || !S.data) { resultsEl.hidden = true; results = []; return; }
    const scored = [];
    for (const n of S.byId.values()) {
      const name = norm(n.name), user = norm(n.username);
      let score = -1;
      if (name.startsWith(q) || user.startsWith(q)) score = 3;
      else if (name.split(/\s+/).some((w) => w.startsWith(q))) score = 2;
      else if (name.includes(q) || user.includes(q)) score = 1;
      if (score >= 0) scored.push([score, n]);
    }
    scored.sort((x, y) => y[0] - x[0] || (y[1].msgs || 0) - (x[1].msgs || 0));
    results = scored.slice(0, 10).map((x) => x[1]);
    activeIdx = results.length ? 0 : -1;
    renderResults();
  }

  function renderResults() {
    if (!results.length) {
      resultsEl.innerHTML = '<li class="empty">Никого не нашли</li>';
    } else {
      resultsEl.innerHTML = results.map((n, i) => `<li role="option" aria-selected="${i === activeIdx}">
        <button type="button" data-id="${esc(n.id)}" class="${i === activeIdx ? 'active' : ''}" tabindex="-1">
          ${avatarHtml(n.id, n.name, n.avatar, 32)}
          <span class="sr-text"><span class="sr-name">${esc(n.name)}</span>
          <span class="sr-sub">${[validUsername(n.username) ? '@' + esc(n.username) : '', n.id === S.data.me ? 'это вы' : '', n.is_member === false ? 'вышел(а)' : ''].filter(Boolean).join(' · ') || `${fmtN(n.msgs)} сообщ.`}</span></span>
        </button></li>`).join('');
    }
    resultsEl.hidden = false;
  }

  function pickResult(id) {
    searchEl.value = '';
    resultsEl.hidden = true;
    searchEl.blur();
    openView({ type: 'person', id });
    focusNode(id);
  }

  searchEl.addEventListener('input', runSearch);
  searchEl.addEventListener('focus', () => { if (searchEl.value.trim()) runSearch(); });
  searchEl.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!results.length) return;
      e.preventDefault();
      activeIdx = (activeIdx + (e.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length;
      renderResults();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (results[activeIdx]) pickResult(results[activeIdx].id);
    } else if (e.key === 'Escape') {
      searchEl.value = '';
      resultsEl.hidden = true;
      searchEl.blur();
    }
  });
  resultsEl.addEventListener('mousedown', (e) => e.preventDefault()); // не терять фокус до клика
  resultsEl.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-id]');
    if (!b) return;
    const n = [...S.byId.values()].find((x) => String(x.id) === b.dataset.id);
    if (n) pickResult(n.id);
  });
  document.addEventListener('pointerdown', (e) => {
    if (!e.target.closest('.search')) resultsEl.hidden = true;
  });

  /* ------------------------------------------------------------- controls */

  const rangeEl = $('#min-strength');
  rangeEl.addEventListener('input', () => {
    S.minStrength = Number(rangeEl.value);
    $('#min-strength-out').textContent = S.minStrength.toFixed(2);
    rangeEl.style.accentColor = strengthCss(S.minStrength);
    computeHl();
    updateLinksCount();
  });
  $('#labels').addEventListener('change', (e) => { S.labels = e.target.checked; });
  $('#logout').addEventListener('click', logout);
  $('#btn-filters').addEventListener('click', (e) => {
    const c = $('#controls');
    const open = !c.classList.contains('open');
    c.classList.toggle('open', open);
    e.currentTarget.setAttribute('aria-expanded', String(open));
  });
  $('#z-in').addEventListener('click', () => { S.userMoved = true; S.G && S.G.zoom(S.G.zoom() * 1.5, 250); });
  $('#z-out').addEventListener('click', () => { S.userMoved = true; S.G && S.G.zoom(S.G.zoom() / 1.5, 250); });
  $('#z-fit').addEventListener('click', () => fitGraph(500));
  $('#z-me').addEventListener('click', () => {
    if (!S.data) return;
    openView({ type: 'person', id: S.data.me });
    focusNode(S.data.me);
  });

  $('#legend-bar').style.background = `linear-gradient(90deg, ${[0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1].map((t) => strengthCss(t) + ` ${t * 100}%`).join(', ')})`;

  /* ---------------------------------------------------------------- panel */

  const P = { el: $('#panel'), body: $('#panel-body'), title: $('#panel-title'), back: $('#panel-back'), stack: [], seq: 0 };

  const sameView = (x, y) => x && y && x.type === y.type && (x.type === 'person' ? String(x.id) === String(y.id) : String(x.a) === String(y.a) && String(x.b) === String(y.b));
  const curView = () => P.stack[P.stack.length - 1];

  function openView(view) {
    const cur = curView();
    if (sameView(cur, view)) { if (!P.el.classList.contains('open')) renderView(cur); return; }
    if (cur) cur.scroll = P.body.scrollTop;
    P.stack.push(view);
    if (P.stack.length > 50) P.stack.shift();
    renderView(view, 0);
  }

  function panelBack() {
    if (P.stack.length <= 1) { closePanel(); return; }
    P.stack.pop();
    const v = curView();
    renderView(v, v.scroll || 0);
    if (v.type === 'person') focusNode(v.id, 0); else focusPair(v.a, v.b);
  }

  function closePanel(silent) {
    P.stack = [];
    P.el.classList.remove('open');
    P.el.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('panel-open');
    S.focus = null;
    computeHl();
    if (!silent) setHash('');
    updateTgBack();
  }

  function setHash(h) {
    const url = location.pathname + location.search + (h ? '#' + h : '');
    try { history.replaceState(null, '', url); } catch (e) { /* file:// и т.п. */ }
  }

  function renderView(view, scroll = 0) {
    P.el.classList.add('open');
    P.el.setAttribute('aria-hidden', 'false');
    document.body.classList.add('panel-open');
    P.back.hidden = P.stack.length <= 1;
    view.token = ++P.seq;
    S.focus = view;
    S.hoverNode = null; // явное открытие важнее «залипшего» наведения
    S.hoverLink = null;
    computeHl();
    updateTgBack();
    if (view.type === 'person') {
      setHash(`p=${view.id}`);
      renderPerson(view, scroll);
    } else {
      setHash(`r=${view.a}-${view.b}`);
      renderRelation(view, scroll);
    }
  }

  const isCurrent = (view, token) => curView() === view && view.token === token;
  const loadingHtml = (t = 'Загружаем…') => `<div class="loading"><div class="spinner"></div>${esc(t)}</div>`;
  const errorHtml = (t) => `<div class="error-box"><p>${esc(t)}</p><button type="button" class="btn" data-act="retry">Повторить</button></div>`;

  P.el.querySelector('#panel-close').addEventListener('click', () => closePanel());
  P.back.addEventListener('click', () => panelBack());
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && P.el.classList.contains('open') && document.activeElement !== searchEl) panelBack();
  });

  /* --------------------------------------------------------- person panel */

  const COUNTER_GROUPS = [
    ['Текст', [['msgs', 'Сообщения'], ['words', 'Слова'], ['chars', 'Символы'], ['links', 'Сообщения со ссылками'], ['edits', 'Отредактировано']]],
    ['Общение', [['replies', 'Ответы'], ['quotes', 'Ответы с цитатой'], ['mentions', 'Упоминания других'], ['reactions', 'Реакции поставлено'], ['forwards', 'Пересылки'], ['comments', 'Комментарии к постам']]],
    ['Медиа', [['media', 'Медиа всего'], ['photos', 'Фото'], ['videos', 'Видео'], ['gifs', 'GIF'], ['stickers', 'Стикеры'], ['documents', 'Файлы'],
      ['audios', 'Аудио'], ['voices', 'Голосовые'], ['voice_sec', 'Голосовые, длительность', 'dur'], ['video_notes', 'Кружки'],
      ['video_note_sec', 'Кружки, длительность', 'dur'], ['polls', 'Опросы']]],
    ['Получено от других', [['replies_recv', 'Ответов получено'], ['quotes_recv', 'Цитат получено'], ['mentions_recv', 'Упоминаний получено'], ['reactions_recv', 'Реакций получено']]],
  ];
  const KNOWN_KEYS = new Set(COUNTER_GROUPS.flatMap((g) => g[1].map((x) => x[0])));

  const TABS = [['d', 'День'], ['w', 'Неделя'], ['m', 'Месяц'], ['all', 'Всё время'], ['avg', 'В среднем за день']];
  const WEEKDAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];

  function periodLabel(kind, start, long) {
    const d = parseDate(start);
    if (!d) return String(start);
    if (kind === 'd') {
      const today = new Date(); today.setHours(0, 0, 0, 0);
      const diff = Math.round((today - d) / 86400000);
      const base = d.toLocaleDateString('ru-RU', { day: 'numeric', month: long ? 'long' : 'short', weekday: long ? 'short' : undefined });
      if (diff === 0) return long ? `Сегодня, ${base}` : 'сегодня';
      if (diff === 1) return long ? `Вчера, ${base}` : 'вчера';
      return base;
    }
    if (kind === 'w') {
      const e = new Date(d); e.setDate(e.getDate() + 6);
      const sameMonth = d.getMonth() === e.getMonth();
      const a = d.toLocaleDateString('ru-RU', sameMonth ? { day: 'numeric' } : { day: 'numeric', month: 'short' });
      const b = e.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
      return `${a} – ${b}`;
    }
    const m = d.toLocaleDateString('ru-RU', { month: long ? 'long' : 'short', year: long ? 'numeric' : '2-digit' });
    return m.replace(/\s?г\.$/, '');
  }

  /** Столбики в SVG. values — массив чисел. */
  function barsSvg(values, { sel = -1, act = null, titles = [], hl = -1 } = {}) {
    const n = values.length;
    if (!n) return '';
    const max = Math.max(1, ...values);
    const W = 100 * n, H = 100, gap = n > 20 ? 18 : 24, bw = 100 - gap;
    let out = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img">`;
    values.forEach((v, i) => {
      const h = v > 0 ? Math.max(3, (v / max) * (H - 4)) : 1.5;
      const cls = i === sel || i === hl ? 'b hl' : 'b';
      const title = `<title>${esc(titles[i] || '')}: ${fmtN(v)}</title>`;
      out += `<g${act ? ` data-act="${act}" data-i="${i}" style="cursor:pointer"` : ''}>${title}<rect class="hit" x="${i * 100}" y="0" width="100" height="${H}"/><rect class="${cls}" x="${i * 100 + gap / 2}" y="${H - h}" width="${bw}" height="${h}" rx="0"/></g>`;
    });
    return out + '</svg>';
  }

  function statsBodyHtml(d, view) {
    const tab = view.tab;
    let c = null, isAvg = false, head = '';
    if (tab === 'all') {
      c = (d.total && d.total.c) || {};
      head = d.person.first_join ? `<div class="chart-caption">С ${esc(fmtDate(d.person.first_join))}</div>` : '';
    } else if (tab === 'avg') {
      c = d.avg_per_day || {};
      isAvg = true;
      head = `<div class="chart-caption">Всего за время в группе, делённое на ${d.days_in_group != null ? `${nf1.format(d.days_in_group)} ${plural(Math.floor(d.days_in_group), 'день', 'дня', 'дней')}` : 'число дней'}.</div>`;
    } else {
      const list = (d.periods && d.periods[tab]) || [];
      if (!list.length) return '<p class="empty">Нет данных за этот период.</p>';
      let i = view.pi[tab] || 0;
      if (i >= list.length) i = 0;
      c = list[i].c || {};
      const chrono = list.slice().reverse(); // старые → новые
      const selChrono = list.length - 1 - i;
      head = `<div class="period-row">
          <select data-act="period-select" aria-label="Период">${list.map((p, j) => `<option value="${j}"${j === i ? ' selected' : ''}>${esc(periodLabel(tab, p.start, true))}</option>`).join('')}</select>
          <span class="muted">${fmtN(c.msgs || 0)} ${plural(c.msgs || 0, 'сообщение', 'сообщения', 'сообщений')}</span>
        </div>
        <div class="chart">${barsSvg(chrono.map((p) => (p.c && p.c.msgs) || 0), { sel: selChrono, act: 'period-bar', titles: chrono.map((p) => periodLabel(tab, p.start, true)) })}
          <div class="chart-ends"><span>${esc(periodLabel(tab, chrono[0].start))}</span><span>сообщения по ${tab === 'd' ? 'дням' : tab === 'w' ? 'неделям' : 'месяцам'}</span><span>${esc(periodLabel(tab, chrono[chrono.length - 1].start))}</span></div>
        </div>`;
    }
    return head + countersHtml(c, isAvg, view);
  }

  function countersHtml(c, isAvg, view) {
    const fmtVal = (v, type) => {
      if (type === 'dur') return isAvg ? (v ? fmtDur(v) : '0 с') : fmtDur(v || 0);
      return isAvg ? nf1.format(v || 0) : fmtN(v || 0);
    };
    const groups = COUNTER_GROUPS.map(([title, rows]) => [title, rows.slice()]);
    const extra = Object.keys(c).filter((k) => !KNOWN_KEYS.has(k) && typeof c[k] === 'number');
    if (extra.length) groups.push(['Прочее', extra.map((k) => [k, k])]);
    let hidden = 0, out = '';
    for (const [title, rows] of groups) {
      const visible = rows.filter(([k]) => view.showAll || (c[k] && Number(c[k]) !== 0));
      hidden += rows.length - rows.filter(([k]) => c[k] && Number(c[k]) !== 0).length;
      if (!visible.length) continue;
      out += `<div class="cgroup-title">${esc(title)}</div><div class="tiles">${visible.map(([k, label, type]) => {
        const v = Number(c[k]) || 0;
        return `<div class="tile${v ? '' : ' zero'}"><b>${esc(fmtVal(v, type))}</b><span>${esc(label)}</span></div>`;
      }).join('')}</div>`;
    }
    if (!out) out = '<p class="empty">Ничего не набралось.</p>';
    if (hidden) out += `<button type="button" class="btn-text show-all" data-act="show-all">${view.showAll ? 'Скрыть нулевые' : `Показать все (ещё ${hidden})`}</button>`;
    return out;
  }

  function factsHtml(d) {
    const p = d.person, t = d.total || {};
    const rows = [];
    const add = (label, value, sub) => { if (value != null && value !== '') rows.push(`<dt>${esc(label)}</dt><dd>${esc(value)}${sub ? `<span class="sub">${esc(sub)}</span>` : ''}</dd>`); };
    add('Первый вход', p.first_join ? fmtDate(p.first_join) : null);
    if (p.last_join && p.last_join !== p.first_join) add('Последний вход', fmtDate(p.last_join));
    if (p.left_at) add('Вышел(а)', fmtDate(p.left_at));
    add('В группе', p.time_in_group_sec != null ? fmtSpan(p.time_in_group_sec) : null);
    add('Первое сообщение', p.first_msg_at ? fmtDateTime(p.first_msg_at) : null);
    add('Последнее сообщение', p.last_msg_at ? fmtDateTime(p.last_msg_at) : null, p.last_msg_at ? relTime(p.last_msg_at) : null);
    if (t.peak_hour != null || t.mean_hour != null) {
      const peak = t.peak_hour != null ? `пик ${String(t.peak_hour).padStart(2, '0')}:00–${String((t.peak_hour + 1) % 24).padStart(2, '0')}:00` : null;
      add('Типичное время активности', peak || `около ${fmtHourMin(t.mean_hour)}`, peak && t.mean_hour != null ? `в среднем около ${fmtHourMin(t.mean_hour)}` : null);
    }
    if (t.avg_session_min != null) add('Средняя сессия', `${nf1.format(t.avg_session_min)} мин`, t.sessions != null ? `${fmtN(t.sessions)} ${plural(t.sessions, 'сессия', 'сессии', 'сессий')}` : null);
    if (t.active_min_per_day != null) add('Активных минут в день', nf1.format(t.active_min_per_day));
    if (t.median_reply_sec != null) add('Медианное время ответа', fmtDur(t.median_reply_sec));
    if (t.active_days != null) add('Активных дней', fmtN(t.active_days), d.days_in_group ? `из ${fmtN(d.days_in_group)} в группе` : null);
    if (t.longest_streak != null) add('Лучшая серия', `${fmtN(t.longest_streak)} ${plural(t.longest_streak, 'день', 'дня', 'дней')} подряд`);
    if (t.conversations_started != null) add('Начал(а) бесед', fmtN(t.conversations_started));
    let html = rows.length ? `<dl class="facts">${rows.join('')}</dl>` : '<p class="empty">Нет данных.</p>';
    const tr = Array.isArray(t.top_reactions) ? t.top_reactions.filter((x) => Array.isArray(x)) : [];
    // премиум-реакция: [custom:ID, n, аналог] — на сайте показываем обычный эмодзи-аналог
    const rLabel = (e, alt) => (String(e).startsWith('custom:') || e === 'paid' ? (alt || '⭐') : e);
    if (tr.length) html += `<h4>Любимые реакции</h4><div class="reactions">${tr.slice(0, 12).map(([e, n, alt]) => `<span class="reaction"${String(e).startsWith('custom:') ? ' title="Премиум-реакция"' : ''}>${esc(rLabel(e, alt))}<span>${fmtN(n)}</span></span>`).join('')}</div>`;
    return html;
  }

  function activityHtml(d) {
    const t = d.total || {};
    const hours = Array.isArray(t.hours) && t.hours.length === 24 ? t.hours : null;
    const wd = Array.isArray(t.weekdays) && t.weekdays.length === 7 ? t.weekdays : null;
    if (!hours && !wd) return '<p class="empty">Нет данных об активности.</p>';
    let out = '';
    if (hours) {
      const peak = t.peak_hour != null ? t.peak_hour : hours.indexOf(Math.max(...hours));
      out += `<h4>По часам</h4><div class="chart tall">${barsSvg(hours, { hl: peak, titles: hours.map((_, i) => `${String(i).padStart(2, '0')}:00–${String((i + 1) % 24).padStart(2, '0')}:00`) })}
        <div class="chart-axis" style="grid-template-columns:repeat(24,1fr)">${hours.map((_, i) => `<span>${i % 3 === 0 ? i : ''}</span>`).join('')}</div></div>`;
    }
    if (wd) {
      const top = wd.indexOf(Math.max(...wd));
      out += `<h4>По дням недели</h4><div class="chart">${barsSvg(wd, { hl: top, titles: WEEKDAYS })}
        <div class="chart-axis" style="grid-template-columns:repeat(7,1fr)">${WEEKDAYS.map((w) => `<span>${w}</span>`).join('')}</div></div>`;
    }
    return out + '<div class="chart-caption">Сообщения за всё время, по местному времени.</div>';
  }

  function dossierShellHtml(d) {
    if (!d.dossier) return '<p class="empty">Досье ещё не составлено.</p>';
    return '<div id="dossier-view"></div>';
  }

  // досье и описание связи: одна текущая редакция, даты изменений — прямо в тексте
  function docViewHtml(doc, text) {
    const when = doc.as_of || doc.updated_at;
    return `<div class="ver-meta">${when ? `по состоянию на ${esc(fmtDate(when))}` : ''}</div>
      <div class="md">${md(text) || '<p class="empty">Текста нет.</p>'}</div>`;
  }

  function fillDossier(view, d) {
    const box = P.body.querySelector('#dossier-view');
    if (box && d.dossier) box.innerHTML = docViewHtml(d.dossier, d.dossier.content);
  }


  function relationsHtml(d, view) {
    const rels = Array.isArray(d.relations) ? d.relations : [];
    const pid = d.person.id;
    let out = '';
    if (!rels.length) out += '<p class="empty">Связей пока нет.</p>';
    else {
      const LIM = 12;
      const shown = view.relAll ? rels : rels.slice(0, LIM);
      out += `<div class="rel-list">${shown.map((r) => `<button type="button" class="rel-item" data-act="rel" data-a="${esc(pid)}" data-b="${esc(r.id)}">
          ${avatarHtml(r.id, r.name, r.avatar, 40)}
          <div class="rel-main">
            <div class="rel-top"><span class="rel-name">${esc(r.name)}</span><span class="rel-num" style="color:${strengthCss(r.strength)}">${fmtS(r.strength)}</span></div>
            ${sbarHtml(r.strength)}
            ${r.kind || r.tone ? `<div class="rel-kind">${esc([r.kind, r.tone].filter(Boolean).join(' · '))}</div>` : ''}
            ${r.summary ? `<div class="rel-sum">${esc(r.summary)}</div>` : ''}
          </div></button>`).join('')}</div>`;
      if (rels.length > LIM) out += `<button type="button" class="btn-text show-all" data-act="rel-all">${view.relAll ? 'Свернуть' : `Показать все (${rels.length})`}</button>`;
    }

    const pairs = Array.isArray(d.pairs) ? d.pairs : [];
    out += '<h4>Взаимодействия</h4>';
    if (!pairs.length) return out + '<p class="empty">Взаимодействий пока нет.</p>';
    const K = [['replies', 'Ответы'], ['quotes', 'Цитаты'], ['mentions', 'Упом.'], ['reactions', 'Реакции']];
    const LIM = 15;
    const shown = view.pairsAll ? pairs : pairs.slice(0, LIM);
    const cell = (o, i) => {
      const x = Number(o && o[0]) || 0, y = Number(o && o[1]) || 0;
      return `<td><span class="io${x ? '' : ' z'}" title="исходящие">→ ${fmtN(x)}</span><span class="io in${y ? '' : ' z'}" title="входящие">← ${fmtN(y)}</span></td>`;
    };
    out += `<p class="tbl-note">В каждой ячейке: → — исходящие (от этого участника), ← — входящие (к нему/ней). Нажмите на имя, чтобы открыть связь.</p>
      <div class="tbl-wrap"><table class="tbl pairs">
      <thead><tr><th>Участник</th>${K.map(([, l]) => `<th>${l}</th>`).join('')}</tr></thead>
      <tbody>${shown.map((p) => `<tr><td><button type="button" class="link-btn" data-act="rel" data-a="${esc(d.person.id)}" data-b="${esc(p.id)}">${esc(p.name)}</button></td>
        ${K.map(([k], i) => cell([p.out && p.out[k], p.in && p.in[k]], i)).join('')}</tr>`).join('')}</tbody></table></div>`;
    if (pairs.length > LIM) out += `<button type="button" class="btn-text show-all" data-act="pairs-all">${view.pairsAll ? 'Свернуть' : `Показать все (${pairs.length})`}</button>`;
    return out;
  }

  function personHtml(d, view) {
    const p = d.person;
    const me = S.data && p.id === S.data.me;
    const badges = [
      p.is_member === false ? '<span class="badge badge-off">вышел(а)</span>' : '<span class="badge badge-ok">участник</span>',
      me ? '<span class="badge badge-me">это вы</span>' : '',
      p.is_premium ? '<span class="badge badge-premium">★ Premium</span>' : '',
      p.is_bot ? '<span class="badge">бот</span>' : '',
    ].join('');
    const uname = validUsername(p.username) ? `<a class="uname" href="https://t.me/${esc(p.username)}" target="_blank" rel="noopener">@${esc(p.username)}</a>` : '';
    const node = S.byId.get(p.id);
    const lead = (d.dossier && d.dossier.summary) || (node && node.summary);
    return `<div class="p-head">${avatarHtml(p.id, p.name, p.avatar || (node && node.avatar), 72)}
        <div style="min-width:0"><h2>${esc(p.name)}</h2>${uname}<div class="badges">${badges}</div></div></div>
      ${lead ? `<p class="lead">${esc(lead)}</p>` : ''}
      <nav class="sec-nav" aria-label="Разделы">
        <button type="button" data-act="scroll" data-to="sec-facts">Факты</button>
        <button type="button" data-act="scroll" data-to="sec-stats">Статистика</button>
        <button type="button" data-act="scroll" data-to="sec-activity">Активность</button>
        <button type="button" data-act="scroll" data-to="sec-dossier">Досье</button>
        <button type="button" data-act="scroll" data-to="sec-rel">Связи</button>
      </nav>
      <section class="sec" id="sec-facts"><h3>Факты</h3>${factsHtml(d)}</section>
      <section class="sec" id="sec-stats"><h3>Статистика</h3>
        <div class="tabs" role="tablist">${TABS.map(([k, l]) => `<button type="button" role="tab" data-act="tab" data-tab="${k}" aria-selected="${view.tab === k}">${l}</button>`).join('')}</div>
        <div class="stats-body" id="stats-body">${statsBodyHtml(d, view)}</div></section>
      <section class="sec" id="sec-activity"><h3>Активность</h3>${activityHtml(d)}</section>
      <section class="sec" id="sec-dossier"><h3>Досье</h3>${dossierShellHtml(d)}</section>
      <section class="sec" id="sec-rel"><h3>Связи</h3><div id="rel-box">${relationsHtml(d, view)}</div></section>`;
  }

  async function renderPerson(view, scroll) {
    const token = view.token;
    const node = S.byId.get(view.id);
    P.title.textContent = node ? node.name : 'Участник';
    if (!view.tab) { view.tab = 'd'; view.pi = { d: 0, w: 0, m: 0 }; view.showAll = false; }
    P.body.innerHTML = loadingHtml('Загружаем профиль…');
    P.body.scrollTop = 0;
    try {
      const d = await api('person', { id: view.id });
      if (!isCurrent(view, token)) return;
      if (!d || !d.person) throw new ApiError(404, 'not_found');
      view.data = d;
      P.title.textContent = d.person.name || P.title.textContent;
      P.body.innerHTML = personHtml(d, view);
      fillDossier(view, d);
      P.body.scrollTop = scroll;
    } catch (e) {
      if (handleAuthError(e)) return;
      if (isCurrent(view, token)) P.body.innerHTML = errorHtml(errText(e));
    }
  }

  function rerenderStats(view) {
    const box = P.body.querySelector('#stats-body');
    if (!box || !view.data) return;
    box.innerHTML = statsBodyHtml(view.data, view);
    P.body.querySelectorAll('.tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === view.tab)));
  }

  /* ------------------------------------------------------- relation panel */

  const PAIR_ROWS = [['replies', 'Ответы'], ['quotes', 'Цитаты'], ['mentions', 'Упоминания'], ['reactions', 'Реакции'], ['forwards', 'Пересылки']];

  function relationHtml(d, view) {
    const A = d.a || { id: view.a, name: '—' }, B = d.b || { id: view.b, name: '—' };
    const rel = d.relation;
    const gl = S.linkByKey.get(lkey(A.id, B.id));
    const s = rel ? rel.strength : gl ? gl.strength : null;
    const color = s != null ? strengthCss(s) : 'var(--muted)';
    let out = `<div class="r-head">
        <button type="button" class="who" data-act="person" data-id="${esc(A.id)}">${avatarHtml(A.id, A.name, A.avatar, 60)}<span>${esc(A.name)}</span></button>
        <div class="r-mid" aria-hidden="true"><svg viewBox="0 0 44 12"><path d="M2 6h40" stroke="${color}" stroke-width="${s != null ? (1 + 4 * s).toFixed(1) : 1.5}" stroke-linecap="round"/></svg></div>
        <button type="button" class="who" data-act="person" data-id="${esc(B.id)}">${avatarHtml(B.id, B.name, B.avatar, 60)}<span>${esc(B.name)}</span></button>
      </div>`;
    if (s != null) {
      out += `<div class="strength-big">${sbarHtml(s, 'lg')}<b style="color:${color}">${fmtS(s)}</b></div><div class="strength-cap">сила связи (0 — нет, 1 — максимум)</div>`;
    }
    if (rel) {
      const chips = [rel.kind ? `<span class="chip"><i>тип:</i> ${esc(rel.kind)}</span>` : '', rel.tone ? `<span class="chip"><i>тон:</i> ${esc(rel.tone)}</span>` : ''].join('');
      if (chips) out += `<div class="chips">${chips}</div>`;
      out += `<dl class="kv">
          <div><dt>По активности</dt><dd>${fmtS(rel.quant)}</dd></div>
          <div><dt>Оценка нейросети</dt><dd>${fmtS(rel.llm_score)}</dd></div>
          <div><dt>Обновлено</dt><dd>${esc(rel.updated_at ? fmtDateShort(rel.updated_at) : '—')}</dd></div></dl>`;
      if (rel.summary) out += `<p class="lead">${esc(rel.summary)}</p>`;
    } else if (gl && (gl.kind || gl.tone)) {
      out += `<div class="chips">${gl.kind ? `<span class="chip"><i>тип:</i> ${esc(gl.kind)}</span>` : ''}${gl.tone ? `<span class="chip"><i>тон:</i> ${esc(gl.tone)}</span>` : ''}</div>`;
    }

    const ab = d.ab || {}, ba = d.ba || {};
    const an = esc(firstName(A.name)), bn = esc(firstName(B.name));
    let sa = 0, sb = 0;
    const rows = PAIR_ROWS.map(([k, l]) => {
      const x = Number(ab[k]) || 0, y = Number(ba[k]) || 0;
      sa += x; sb += y;
      return `<tr><td>${l}</td><td class="${x ? '' : 'z'}">${fmtN(x)}</td><td class="${y ? '' : 'z'}">${fmtN(y)}</td></tr>`;
    }).join('');
    out += `<section class="sec"><h3>Взаимодействия</h3><div class="tbl-wrap"><table class="tbl">
        <thead><tr><th></th><th>${an} → ${bn}</th><th>${bn} → ${an}</th></tr></thead>
        <tbody>${rows}</tbody><tfoot><tr><td>Всего</td><td>${fmtN(sa)}</td><td>${fmtN(sb)}</td></tr></tfoot></table></div></section>`;

    out += '<section class="sec"><h3>Описание</h3>';
    if (!rel) {
      out += '<div class="notice">Нейросеть ещё не описала эту связь.</div></section>';
      return out;
    }
    return out + '<div id="rel-view"></div></section>';
  }

  function fillRelation(view, d) {
    const box = P.body.querySelector('#rel-view');
    if (box && d.relation) box.innerHTML = docViewHtml(d.relation, d.relation.description);
  }

  async function renderRelation(view, scroll) {
    const token = view.token;
    const A = S.byId.get(view.a), B = S.byId.get(view.b);
    P.title.textContent = `${A ? firstName(A.name) : '…'} и ${B ? firstName(B.name) : '…'}`;
    P.body.innerHTML = loadingHtml('Загружаем связь…');
    P.body.scrollTop = 0;
    try {
      const d = await api('relation', { a: view.a, b: view.b });
      if (!isCurrent(view, token)) return;
      view.data = d;
      if (d.a && d.b) P.title.textContent = `${firstName(d.a.name)} и ${firstName(d.b.name)}`;
      P.body.innerHTML = relationHtml(d, view);
      fillRelation(view, d);
      P.body.scrollTop = scroll;
    } catch (e) {
      if (handleAuthError(e)) return;
      if (isCurrent(view, token)) P.body.innerHTML = errorHtml(errText(e));
    }
  }

  /* ------------------------------------------------- panel event delegation */

  const toId = (v) => (/^-?\d+$/.test(v) ? Number(v) : v);

  P.body.addEventListener('click', (e) => {
    const a = e.target.closest('a[href]');
    if (a && TGUI) {
      const href = a.getAttribute('href');
      if (/^https?:\/\//i.test(href)) {
        e.preventDefault();
        try {
          if (/^https?:\/\/t\.me\//i.test(href)) TG.openTelegramLink(href); else TG.openLink(href);
        } catch (err) { window.open(href, '_blank', 'noopener'); }
        return;
      }
    }
    const el = e.target.closest('[data-act]');
    if (!el || el.tagName === 'SELECT') return;
    const view = curView();
    if (!view) return;
    switch (el.dataset.act) {
      case 'person': {
        const id = toId(el.dataset.id);
        openView({ type: 'person', id });
        focusNode(id, 0);
        break;
      }
      case 'rel': {
        const a = toId(el.dataset.a), b = toId(el.dataset.b);
        openView({ type: 'relation', a, b });
        focusPair(a, b);
        break;
      }
      case 'tab': view.tab = el.dataset.tab; rerenderStats(view); break;
      case 'period-bar': {
        const list = (view.data.periods && view.data.periods[view.tab]) || [];
        view.pi[view.tab] = list.length - 1 - Number(el.dataset.i);
        rerenderStats(view);
        break;
      }
      case 'show-all': view.showAll = !view.showAll; rerenderStats(view); break;
      case 'rel-all': case 'pairs-all': {
        view[el.dataset.act === 'rel-all' ? 'relAll' : 'pairsAll'] = !view[el.dataset.act === 'rel-all' ? 'relAll' : 'pairsAll'];
        const box = P.body.querySelector('#rel-box');
        if (box && view.data) box.innerHTML = relationsHtml(view.data, view);
        break;
      }
      case 'scroll': {
        const target = P.body.querySelector('#' + el.dataset.to);
        if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        break;
      }
      case 'retry': cache.clear(); renderView(view, 0); break;
      default: break;
    }
  });

  P.body.addEventListener('change', (e) => {
    const el = e.target.closest('select[data-act]');
    const view = curView();
    if (!el || !view || !view.data) return;
    if (el.dataset.act === 'period-select') { view.pi[view.tab] = Number(el.value); rerenderStats(view); }
  });

  /* ------------------------------------------------------------ deep links */

  function parseLink(s) {
    if (!s) return null;
    s = String(s).replace(/^#/, '');
    let m = /^p=?(-?\d+)$/.exec(s);
    if (m) return { type: 'person', id: Number(m[1]) };
    m = /^r=?(-?\d+)[-_](-?\d+)$/.exec(s);
    if (m) return { type: 'relation', a: Number(m[1]), b: Number(m[2]) };
    return null;
  }

  function openDeepLink(v, initial) {
    if (!v) return;
    openView(v);
    if (v.type === 'person') {
      if (initial) S.pendingFocus = v.id; else focusNode(v.id);
    } else if (!initial) focusPair(v.a, v.b);
  }

  window.addEventListener('hashchange', () => {
    if (!S.data) return;
    const v = parseLink(location.hash);
    if (v) openDeepLink(v, false);
    else if (!location.hash) closePanel(true);
  });

  /* ------------------------------------------------------------------ boot */

  let deepLinkDone = false;
  async function start() {
    appEl.hidden = true;
    showLoading('Загружаем карту…');
    if (!window.ForceGraph) {
      showError('Не удалось загрузить библиотеку графа (cdn.jsdelivr.net). Проверьте соединение или блокировщик.', () => location.reload());
      return;
    }
    try {
      const data = await api('graph');
      if (!data || !Array.isArray(data.nodes)) throw new ApiError(200, 'parse', 'Сервер вернул некорректный ответ');
      hideScreen();
      appEl.hidden = false;
      if (!S.G) S.G = createGraph();
      if (DEMO) window.LevbushDebug = S; // для автотестов демо-режима
      resizeGraph();
      setData(data);
      if (!deepLinkDone) {
        deepLinkDone = true;
        // ?p=ID / ?r=A-B — из кнопок бота (в Mini App хвост # занят данными входа Telegram), #p= — обычные ссылки
        let v = parseLink(location.hash)
          || (QS.get('p') ? parseLink('p' + QS.get('p')) : QS.get('r') ? parseLink('r' + QS.get('r')) : null);
        if (!v && TGUI) {
          const sp = TG.initDataUnsafe && TG.initDataUnsafe.start_param;
          v = parseLink(sp);
        }
        if (v) openDeepLink(v, true);
      }
    } catch (e) {
      if (!handleAuthError(e)) showError(errText(e), start);
    }
  }

  function loadDemo() {
    return new Promise((resolve, reject) => {
      if (window.LevbushDemo) { resolve(); return; }
      const s = document.createElement('script');
      s.src = 'demo.js';
      s.onload = () => resolve();
      s.onerror = () => reject(new Error('demo.js не загрузился'));
      document.head.appendChild(s);
    });
  }

  function boot() {
    readColors();
    if (TGUI) setupTelegram();
    else {
      const onScheme = () => readColors();
      if (mqDark.addEventListener) mqDark.addEventListener('change', onScheme);
    }
    window.addEventListener('resize', resizeGraph);
    const onBp = () => { if (!isMobile()) $('#controls').classList.remove('open'); };
    if (mqMobile.addEventListener) mqMobile.addEventListener('change', onBp);

    if (DEMO) {
      showLoading('Загружаем демо-данные…');
      loadDemo().then(start, (e) => showError(e.message, () => location.reload()));
    } else if (MINI) {
      start();
    } else {
      login = loadLogin();
      if (login) start(); else showLogin();
    }
  }

  boot();
})();
