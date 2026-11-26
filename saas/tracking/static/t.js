/*! t.js - first-party analytics, consent, fingerprinting and session replay. No dependencies.
 *
 * Loaded on every customer page: <script src="/ts/t.js" defer></script>
 *
 * Before consent (EU/EEA/UK, or anyone when the server says so) it does exactly one thing: fill
 * input[name=fp] / input[name=ts] on identity forms, for abuse prevention. Analytics, the vid cookie
 * and replay start only once the server reports consent as granted or implied (notice regions).
 * It never reads form values; replay masks every input and anything marked [data-track-mask].
 *
 * Public API: window.dsTrack(name, props), window.dsFingerprint() -> Promise<string>,
 *             window.dsConsent.open() (and any element with [data-ds-consent-open]).
 */
(function () {
  'use strict';
  var W = window, D = document, N = navigator;
  if (W.__dsTracker) return;
  W.__dsTracker = 1;

  var EP = '/api/t', CFG = '/api/t/config', CONSENT = '/api/consent', REPLAY = '/api/t/replay';
  var BOOT = Date.now();
  var MAX_BATCH = 200, FLUSH_MS = 5000, HB_MS = 5000, IDLE_MS = 30000;
  var NO_REPLAY = /^\/(admin|login|signup|forgot|reset|verify|verify-email|waitlist\/confirm|app\/settings|app\/developers|app\/billing)(\/|$)/;

  var cfg = null, allowed = false, begun = false;
  var pv = newId('p_'), queue = [], sentSig = false, sentLayout = false;
  var fpValue = null, fpPromise = null;

  // ---------------------------------------------------------------------------------------- utils
  function now() { return Date.now(); }
  function hex(n) {
    var a = new Uint8Array(n), s = '';
    if (W.crypto && W.crypto.getRandomValues) W.crypto.getRandomValues(a);
    else for (var i = 0; i < n; i++) a[i] = Math.floor(Math.random() * 256);
    for (var j = 0; j < n; j++) s += ('0' + a[j].toString(16)).slice(-2);
    return s;
  }
  function newId(p) { return p + hex(12); }
  function cookie(name) {
    var m = D.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }
  function clip(s, n) { s = s == null ? '' : String(s); return s.length > n ? s.slice(0, n) : s; }
  function docH() { var b = D.body, e = D.documentElement; return Math.max(b ? b.scrollHeight : 0, e.scrollHeight, 1); }
  function docW() { var b = D.body, e = D.documentElement; return Math.max(b ? b.scrollWidth : 0, e.scrollWidth, 1); }
  function round4(x) { return Math.round(x * 10000) / 10000; }
  function closest(el, sel) {
    while (el && el.nodeType === 1) { if (el.matches && el.matches(sel)) return el; el = el.parentElement; }
    return null;
  }
  function section(el) { var s = closest(el, '[data-track-section]'); return s ? clip(s.getAttribute('data-track-section'), 64) : undefined; }
  function on(t, ev, fn, opts) { t.addEventListener(ev, fn, opts || { passive: true, capture: true }); }

  // 128-bit non-cryptographic hash (cyrb128). Same answer in every context, secure or not.
  function hash128(str) {
    var h1 = 1779033703, h2 = 3144134277, h3 = 1013904242, h4 = 2773480762;
    for (var i = 0, k; i < str.length; i++) {
      k = str.charCodeAt(i);
      h1 = h2 ^ Math.imul(h1 ^ k, 597399067);
      h2 = h3 ^ Math.imul(h2 ^ k, 2869860233);
      h3 = h4 ^ Math.imul(h3 ^ k, 951274213);
      h4 = h1 ^ Math.imul(h4 ^ k, 2716044179);
    }
    h1 = Math.imul(h3 ^ (h1 >>> 18), 597399067);
    h2 = Math.imul(h4 ^ (h2 >>> 22), 2869860233);
    h3 = Math.imul(h1 ^ (h3 >>> 17), 951274213);
    h4 = Math.imul(h2 ^ (h4 >>> 19), 2716044179);
    h1 ^= (h2 ^ h3 ^ h4); h2 ^= h1; h3 ^= h1; h4 ^= h1;
    return [h1, h2, h3, h4].map(function (x) { return ('00000000' + (x >>> 0).toString(16)).slice(-8); }).join('');
  }

  // --------------------------------------------------------------------------------- fingerprint
  function canvasFp() {
    try {
      var c = D.createElement('canvas'); c.width = 240; c.height = 60;
      var x = c.getContext('2d'); if (!x) return 'nocanvas';
      x.textBaseline = 'top'; x.font = "14px 'Arial'"; x.fillStyle = '#f60'; x.fillRect(125, 1, 62, 20);
      x.fillStyle = '#069'; x.fillText('Cwm fjordbank glyphs vext quiz 😃', 2, 15);
      x.fillStyle = 'rgba(102,204,0,0.7)'; x.fillText('Cwm fjordbank glyphs vext quiz', 4, 17);
      x.globalCompositeOperation = 'multiply'; x.fillStyle = 'rgb(255,0,255)';
      x.beginPath(); x.arc(50, 50, 50, 0, Math.PI * 2, true); x.closePath(); x.fill();
      return hash128(c.toDataURL());
    } catch (e) { return 'err'; }
  }
  function webglInfo() {
    try {
      var c = D.createElement('canvas');
      var gl = c.getContext('webgl') || c.getContext('experimental-webgl');
      if (!gl) return { v: 'nogl' };
      var dbg = gl.getExtension('WEBGL_debug_renderer_info');
      var v = dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR);
      var r = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
      var p = [gl.MAX_TEXTURE_SIZE, gl.MAX_VIEWPORT_DIMS, gl.MAX_RENDERBUFFER_SIZE, gl.MAX_VERTEX_ATTRIBS,
        gl.MAX_TEXTURE_IMAGE_UNITS, gl.ALIASED_LINE_WIDTH_RANGE].map(function (q) {
        var val = gl.getParameter(q); return val && val.length ? Array.prototype.join.call(val, 'x') : String(val);
      }).join(',');
      return { v: clip(v, 80), r: clip(r, 120), p: p, e: (gl.getSupportedExtensions() || []).length };
    } catch (e) { return { v: 'err' }; }
  }
  function audioFp() {
    return new Promise(function (resolve) {
      var done = false;
      function fin(v) { if (!done) { done = true; resolve(v); } }
      try {
        var AC = W.OfflineAudioContext || W.webkitOfflineAudioContext;
        if (!AC) return fin('noaudio');
        var ctx = new AC(1, 5000, 44100), osc = ctx.createOscillator(), comp = ctx.createDynamicsCompressor();
        osc.type = 'triangle'; osc.frequency.value = 10000;
        comp.threshold.value = -50; comp.knee.value = 40; comp.ratio.value = 12; comp.attack.value = 0; comp.release.value = 0.25;
        osc.connect(comp); comp.connect(ctx.destination); osc.start(0);
        setTimeout(function () { fin('timeout'); }, 1000);
        ctx.oncomplete = function (ev) {
          var d = ev.renderedBuffer.getChannelData(0), s = 0;
          for (var i = 4500; i < 5000; i++) s += Math.abs(d[i]);
          fin(s.toFixed(6));
        };
        var pr = ctx.startRendering();
        if (pr && pr.catch) pr.catch(function () { fin('err'); });
      } catch (e) { fin('err'); }
    });
  }
  var FONTS = ['Arial', 'Arial Black', 'Calibri', 'Cambria', 'Candara', 'Comic Sans MS', 'Consolas', 'Courier New',
    'Georgia', 'Helvetica', 'Helvetica Neue', 'Impact', 'Lucida Console', 'Lucida Grande', 'Menlo', 'Monaco',
    'Palatino', 'Segoe UI', 'Tahoma', 'Times New Roman', 'Trebuchet MS', 'Verdana', 'Roboto', 'Ubuntu',
    'Noto Sans', 'Noto Sans Devanagari', 'Mangal', 'Nirmala UI', 'Apple Color Emoji', 'Segoe UI Emoji',
    'Cantarell', 'DejaVu Sans', 'Liberation Sans', 'Fira Sans', 'Source Sans Pro', 'Open Sans', 'PingFang SC',
    'Microsoft YaHei', 'MS Gothic', 'Avenir', 'Futura', 'Gill Sans', 'Optima', 'Franklin Gothic Medium'];
  function fontsFp() {
    try {
      var c = D.createElement('canvas').getContext('2d'); if (!c) return 'nofonts';
      var text = 'mmMwWLliI0fiflO&1', bases = ['monospace', 'sans-serif', 'serif'], base = {}, bits = '';
      bases.forEach(function (b) { c.font = '72px ' + b; base[b] = c.measureText(text).width; });
      FONTS.forEach(function (f) {
        var hit = bases.some(function (b) { c.font = '72px "' + f + '", ' + b; return c.measureText(text).width !== base[b]; });
        bits += hit ? '1' : '0';
      });
      return bits;
    } catch (e) { return 'err'; }
  }
  function tzName() { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) { return ''; } }
  function signals() {
    var s = {
      sw: screen.width, sh: screen.height, cd: screen.colorDepth, dpr: W.devicePixelRatio || 1,
      vw: W.innerWidth, vh: W.innerHeight, tz: tzName(), lang: N.language || '',
      langs: (N.languages || []).slice(0, 5), hc: N.hardwareConcurrency || 0, dm: N.deviceMemory || 0,
      touch: N.maxTouchPoints || 0, plat: (N.userAgentData && N.userAgentData.platform) || N.platform || '',
      vendor: N.vendor || '', pdf: !!N.pdfViewerEnabled, cookies: !!N.cookieEnabled
    };
    var g = webglInfo(); s.wgl = clip((g.v || '') + ' / ' + (g.r || ''), 120);
    var auto = [];
    if (N.webdriver) auto.push('webdriver');
    if (W.callPhantom || W._phantom) auto.push('phantom');
    if (W.__nightmare) auto.push('nightmare');
    if (W.domAutomation || W.domAutomationController) auto.push('selenium');
    if (W.__playwright__binding__ || W.__pwInitScripts) auto.push('playwright');
    try { if (Object.keys(D).some(function (k) { return /^\$?cdc_|^\$wdc_/.test(k); })) auto.push('cdc'); } catch (e) { }
    if (/HeadlessChrome/.test(N.userAgent)) auto.push('headless_ua');
    if (!N.languages || !N.languages.length) auto.push('no_languages');
    if (W.outerWidth === 0 && W.outerHeight === 0) auto.push('zero_outer');
    s.auto = auto;
    return s;
  }
  function fingerprint() {
    if (fpPromise) return fpPromise;
    fpPromise = audioFp().then(function (audio) {
      var s = signals(), g = webglInfo();
      var parts = [canvasFp(), JSON.stringify(g), audio, fontsFp(), [s.sw, s.sh, s.cd, s.dpr].join('x'), s.tz,
        s.langs.join(','), s.hc, s.dm, s.touch, s.plat, s.vendor].join('|');
      fpValue = hash128(parts);
      return fpValue;
    }, function () {
      fpValue = hash128('fallback|' + N.userAgent + '|' + screen.width + 'x' + screen.height + '|' + tzName());
      return fpValue;
    });
    return fpPromise;
  }
  W.dsFingerprint = fingerprint;

  // Identity forms: fp (abuse prevention, runs before consent) and ts (render time) if the page left it empty.
  function fillForms() {
    var fps = D.querySelectorAll('input[name=fp]'), tss = D.querySelectorAll('input[name=ts]');
    for (var i = 0; i < tss.length; i++) if (!tss[i].value) tss[i].value = String(BOOT);
    if (!fps.length) return;
    fingerprint().then(function (fp) {
      var list = D.querySelectorAll('input[name=fp]');
      for (var j = 0; j < list.length; j++) if (!list[j].value) list[j].value = fp;
    });
  }

  // ------------------------------------------------------------------------------------ transport
  function post(url, body, beacon, type) {
    try {
      if (beacon && N.sendBeacon && body.length < 60000) {
        if (N.sendBeacon(url, new Blob([body], { type: type || 'text/plain' }))) return;
      }
      fetch(url, {
        method: 'POST', body: body, credentials: 'same-origin', keepalive: body.length < 60000,
        headers: { 'Content-Type': type || 'text/plain' }
      }).catch(function () { });
    } catch (e) { }
  }
  function push(ev) {
    if (!allowed) return;
    ev.ts = now();
    queue.push(ev);
    if (queue.length >= MAX_BATCH) flush(false);
  }
  function flush(final) {
    if (!allowed) { queue = []; return; }
    collectDwell(); collectHeartbeat(); collectHeat();
    if (final) collectAbandons();
    while (queue.length || !sentSig) {
      var batch = { v: 1, pv: pv, now: now(), url: location.pathname + location.search, ref: D.referrer || undefined,
        title: clip(D.title, 200), e: queue.splice(0, MAX_BATCH) };
      if (!sentSig && fpValue) { batch.sig = signals(); batch.fp = fpValue; sentSig = true; }
      if (!sentLayout && layout) { batch.layout = layout; sentLayout = true; }
      if (cfg && cfg.vid && !N.cookieEnabled) batch.vid = cfg.vid;
      if (!batch.e.length && !batch.sig) break;
      post(EP, JSON.stringify(batch), final);
      if (!sentSig && !fpValue) break;
    }
  }

  // ------------------------------------------------------------------------------------ collectors
  var maxScroll = 0, lastBucket = -1;
  function onScroll() {
    var d = Math.min(100, Math.round(((W.scrollY || D.documentElement.scrollTop) + W.innerHeight) / docH() * 100));
    if (d > maxScroll) {
      maxScroll = d;
      var b = Math.floor(d / 10);
      if (b > lastBucket) { lastBucket = b; push({ t: 'scroll', v: d }); }
    }
  }

  // Section dwell: a section counts while >= 50% of it, or half the viewport, is on screen and the tab is visible.
  var visibleSince = {}, dwellAcc = {}, intersecting = {}, layout = null;
  function openSpan(n) { if (!visibleSince[n] && D.visibilityState === 'visible') visibleSince[n] = now(); }
  function closeSpan(n) { if (visibleSince[n]) { dwellAcc[n] = (dwellAcc[n] || 0) + (now() - visibleSince[n]); delete visibleSince[n]; } }
  function collectDwell() {
    Object.keys(visibleSince).forEach(function (n) { closeSpan(n); if (intersecting[n]) openSpan(n); });
    Object.keys(dwellAcc).forEach(function (n) {
      if (dwellAcc[n] >= 300) queue.push({ t: 'dwell', n: n, s: n, v: Math.min(120000, dwellAcc[n]), ts: now() });
      delete dwellAcc[n];
    });
  }
  function setupSections() {
    var secs = D.querySelectorAll('[data-track-section]');
    if (!secs.length) return;
    var h = docH();
    layout = [];
    for (var i = 0; i < secs.length && i < 60; i++) {
      var r = secs[i].getBoundingClientRect(), top = r.top + (W.scrollY || 0);
      layout.push({ n: clip(secs[i].getAttribute('data-track-section'), 64), t: round4(top / h), h: round4(r.height / h) });
    }
    if (!W.IntersectionObserver) return;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        var n = en.target.getAttribute('data-track-section');
        var vis = en.intersectionRatio >= 0.5 || en.intersectionRect.height >= W.innerHeight * 0.5;
        intersecting[n] = vis;
        if (vis) openSpan(n); else closeSpan(n);
      });
    }, { threshold: [0, 0.25, 0.5, 0.75, 1] });
    for (var j = 0; j < secs.length; j++) io.observe(secs[j]);
  }

  // Clicks, rage clicks (3 within 800ms and 30px) and dead clicks (nothing happened within 1s).
  var recentClicks = [], mutated = 0, mo = null;
  var INTERACTIVE = 'a,button,input,select,textarea,label,summary,details,[role=button],[role=link],[role=tab],[role=checkbox],[role=switch],[onclick],[data-track],[tabindex]';
  function describe(el) {
    var t = closest(el, '[data-track]');
    if (t) return clip(t.getAttribute('data-track'), 100);
    var a = closest(el, 'a[href]');
    if (a) {
      try { var u = new URL(a.href, location.href); return 'a:' + (u.host === location.host ? u.pathname : u.host); } catch (e) { return 'a'; }
    }
    var b = closest(el, 'button,[role=button],input[type=submit],input[type=button]');
    if (b) return 'button:' + clip((b.getAttribute('aria-label') || b.textContent || b.getAttribute('value') || '').replace(/\s+/g, ' ').trim(), 40);
    return (el.tagName || 'node').toLowerCase();
  }
  function onClick(e) {
    var el = e.target; if (!el || el.nodeType !== 1) return;
    var x = round4(e.pageX / docW()), y = round4(e.pageY / docH()), name = describe(el), sec = section(el);
    push({ t: 'click', n: name, s: sec, x: x, y: y });
    var t = now();
    recentClicks = recentClicks.filter(function (c) { return t - c.t < 800 && Math.abs(c.px - e.pageX) < 30 && Math.abs(c.py - e.pageY) < 30; });
    recentClicks.push({ t: t, px: e.pageX, py: e.pageY });
    if (recentClicks.length === 3) push({ t: 'rage', n: name, s: sec, x: x, y: y });
    if (!closest(el, INTERACTIVE) && !(W.getSelection && String(W.getSelection()))) {
      var before = mutated, href = location.href;
      setTimeout(function () {
        if (mutated === before && location.href === href) push({ t: 'dead', n: name, s: sec, x: x, y: y });
      }, 1000);
    }
  }

  // Mouse heat: one sampled point per 120ms of movement, page-relative, capped per page.
  var heat = [], heatTotal = 0, lastMove = 0, lastActivity = now();
  function onMove(e) {
    lastActivity = now();
    if (heatTotal >= 600 || lastActivity - lastMove < 120) return;
    lastMove = lastActivity; heatTotal++;
    heat.push([round4(e.pageX / docW()), round4(e.pageY / docH())]);
  }
  function collectHeat() { if (heat.length) { queue.push({ t: 'heat', d: { p: heat.splice(0, 150) }, ts: now() }); } }

  // Active time: counted in HB_MS steps while visible and touched in the last 30s.
  var activeMs = 0, lastTick = now();
  function tick() {
    var t = now();
    if (D.visibilityState === 'visible' && t - lastActivity < IDLE_MS) activeMs += Math.min(t - lastTick, HB_MS * 2);
    lastTick = t;
  }
  function collectHeartbeat() { tick(); if (activeMs >= 500) { queue.push({ t: 'hb', v: Math.min(30000, activeMs), ts: now() }); activeMs = 0; } }

  // Forms: which field got focus, submitted or abandoned. Field *names* only, never values.
  var forms = {};
  function formName(f) { return clip(f.getAttribute('data-track') || f.id || f.getAttribute('name') || (f.getAttribute('action') || 'form'), 64); }
  function fieldName(el) { return clip(el.getAttribute('name') || el.id || el.type || el.tagName.toLowerCase(), 40); }
  function onFocus(e) {
    var el = e.target; if (!el || !el.form || !/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) return;
    if (el.type === 'hidden' || el.name === 'website') return;
    var n = formName(el.form), f = forms[n] || (forms[n] = { fields: [], started: false, submitted: false, last: '' });
    var fn = fieldName(el);
    if (f.fields.indexOf(fn) < 0 && f.fields.length < 20) f.fields.push(fn);
    f.last = fn;
    if (!f.started) { f.started = true; push({ t: 'focus', n: n, s: section(el), d: { f: fn } }); }
  }
  function onSubmit(e) {
    var fm = e.target; if (!fm || fm.tagName !== 'FORM') return;
    var n = formName(fm), f = forms[n] || (forms[n] = { fields: [], started: true, submitted: false, last: '' });
    f.submitted = true; push({ t: 'submit', n: n, s: section(fm) }); flush(false);
  }
  function collectAbandons() {
    Object.keys(forms).forEach(function (n) {
      var f = forms[n];
      if (f.started && !f.submitted && !f.reported) { f.reported = true; queue.push({ t: 'abandon', n: n, d: { fields: f.fields, last: f.last }, ts: now() }); }
    });
  }

  // CTA impressions (>= 50% visible once) and hovers (>= 600ms over a [data-track]).
  var seenImp = {}, seenHover = {}, hoverTimer = null;
  function setupCtas() {
    var els = D.querySelectorAll('[data-track]');
    if (W.IntersectionObserver && els.length) {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          var n = en.target.getAttribute('data-track');
          if (en.intersectionRatio >= 0.5 && !seenImp[n]) { seenImp[n] = 1; push({ t: 'imp', n: clip(n, 100), s: section(en.target) }); }
        });
      }, { threshold: [0.5] });
      for (var i = 0; i < els.length && i < 200; i++) io.observe(els[i]);
    }
    on(D, 'mouseover', function (e) {
      var t = closest(e.target, '[data-track]'); if (!t) return;
      var n = t.getAttribute('data-track'); if (seenHover[n]) return;
      clearTimeout(hoverTimer);
      hoverTimer = setTimeout(function () { seenHover[n] = 1; push({ t: 'hover', n: clip(n, 100), s: section(t) }); }, 600);
    });
    on(D, 'mouseout', function (e) { if (closest(e.target, '[data-track]')) clearTimeout(hoverTimer); });
  }

  // Web Vitals (LCP, CLS, INP, FCP, TTFB), sent once when the page is hidden.
  var vitals = {}, vitalsSent = false;
  function observe(type, fn, extra) {
    try {
      var po = new PerformanceObserver(function (l) { l.getEntries().forEach(fn); });
      var o = { type: type, buffered: true }; if (extra) for (var k in extra) o[k] = extra[k];
      po.observe(o);
    } catch (e) { }
  }
  function setupVitals() {
    if (!W.PerformanceObserver) return;
    observe('largest-contentful-paint', function (e) { vitals.LCP = e.startTime; });
    observe('layout-shift', function (e) { if (!e.hadRecentInput) vitals.CLS = (vitals.CLS || 0) + e.value; });
    observe('event', function (e) { if (e.interactionId) vitals.INP = Math.max(vitals.INP || 0, e.duration); }, { durationThreshold: 40 });
    observe('paint', function (e) { if (e.name === 'first-contentful-paint') vitals.FCP = e.startTime; });
    try {
      var nav = performance.getEntriesByType('navigation')[0];
      if (nav) vitals.TTFB = nav.responseStart;
    } catch (e) { }
  }
  function sendVitals() {
    if (vitalsSent) return; vitalsSent = true;
    Object.keys(vitals).forEach(function (k) { queue.push({ t: 'vital', n: k, v: Math.round(vitals[k] * (k === 'CLS' ? 1000 : 1)) / (k === 'CLS' ? 1000 : 1), ts: now() }); });
  }

  // JS errors, deduped, ten per page at most.
  var errSeen = {}, errCount = 0;
  function reportError(msg, src, line, col) {
    msg = clip(msg || 'error', 200);
    if (errSeen[msg] || errCount >= 10) return;
    errSeen[msg] = 1; errCount++;
    var where = '';
    try { if (src) { var u = new URL(src, location.href); where = u.host === location.host ? u.pathname : u.host; } } catch (e) { }
    push({ t: 'error', n: msg, d: { src: clip(where, 120), line: line || 0, col: col || 0 } });
  }

  // ------------------------------------------------------------------------------- session replay
  var replayOn = false;
  function noReplayHere() {
    if (NO_REPLAY.test(location.pathname)) return true;
    if (D.documentElement.hasAttribute('data-track-noreplay')) return true;
    var m = D.querySelector('meta[name="ds-replay"]');
    return !!(m && m.getAttribute('content') === 'off');
  }
  function gzipBlob(str) {
    if (W.CompressionStream && W.Response) {
      try { return new Response(new Blob([str]).stream().pipeThrough(new CompressionStream('gzip'))).blob(); } catch (e) { }
    }
    return Promise.resolve(new Blob([str]));
  }
  function startReplay() {
    if (replayOn || !cfg || !cfg.replay || noReplayHere()) return;
    replayOn = true;
    var s = D.createElement('script');
    s.src = cfg.replay_js; s.async = true;
    s.onload = function () {
      var rec = W.rrwebRecord && W.rrwebRecord.record; if (!rec) return;
      var buf = [], seq = 0;
      function url() { return REPLAY + '?pv=' + pv + '&seq=' + (seq++) + (cfg.vid && !N.cookieEnabled ? '&vid=' + cfg.vid : ''); }
      function send(final) {
        if (!buf.length || !allowed) return;
        var body = JSON.stringify(buf); buf = [];
        // sendBeacon only takes CORS-safelisted types, so the last chunk goes as text/plain, uncompressed.
        if (final) { post(url(), body, true); return; }
        var u = url();
        gzipBlob(body).then(function (blob) {
          fetch(u, { method: 'POST', body: blob, credentials: 'same-origin', headers: { 'Content-Type': 'application/octet-stream' } }).catch(function () { });
        });
      }
      try {
        rec({
          emit: function (ev) { buf.push(ev); if (buf.length >= 500) send(false); },
          maskAllInputs: true, maskTextSelector: '[data-track-mask]', blockSelector: '[data-track-block]',
          recordCanvas: false, collectFonts: false, inlineImages: false, slimDOMOptions: 'all',
          sampling: { mousemove: 50, scroll: 150, media: 800, input: 'last' }
        });
      } catch (e) { return; }
      setInterval(function () { send(false); }, 10000);
      on(W, 'pagehide', function () { send(true); });
      on(D, 'visibilitychange', function () { if (D.visibilityState === 'hidden') send(false); });
    };
    (D.head || D.documentElement).appendChild(s);
  }

  // -------------------------------------------------------------------------------------- consent
  var banner = null;
  function ensureCss() {
    if (D.getElementById('ds-consent-css')) return;
    var l = D.createElement('link'); l.id = 'ds-consent-css'; l.rel = 'stylesheet'; l.href = '/ts/t.css';
    (D.head || D.documentElement).appendChild(l);
  }
  function el(tag, attrs, text) {
    var e = D.createElement(tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text) e.textContent = text;
    return e;
  }
  function showBanner(ask) {
    if (banner || !D.body) return;
    ensureCss();
    var brand = (cfg && cfg.brand) || 'this site';
    banner = el('div', { 'class': 'ds-consent', role: 'region', 'aria-label': 'Privacy choices', 'data-track-block': '' });
    var p = el('p', { 'class': 'ds-consent-text' }, ask
      ? 'We use first-party cookies and analytics, including session recordings with everything you type hidden, to improve ' + brand + ' and keep it safe from abuse. Nothing is shared with ad networks.'
      : brand + ' uses first-party analytics, including session recordings with everything you type hidden, to improve the site and keep it safe. Nothing is shared with ad networks.');
    var links = el('span', { 'class': 'ds-consent-links' });
    links.appendChild(el('a', { href: (cfg && cfg.privacy_url) || '/privacy' }, 'Privacy'));
    links.appendChild(D.createTextNode(' · '));
    links.appendChild(el('a', { href: (cfg && cfg.cookies_url) || '/cookies' }, 'Cookies'));
    p.appendChild(D.createTextNode(' ')); p.appendChild(links);
    var actions = el('div', { 'class': 'ds-consent-actions' });
    var no = el('button', { type: 'button', 'class': 'ds-btn', 'data-ds-choice': 'denied' }, ask ? 'Decline' : 'Opt out');
    var yes = el('button', { type: 'button', 'class': 'ds-btn ds-btn-primary', 'data-ds-choice': 'granted' }, ask ? 'Accept' : 'OK');
    actions.appendChild(no); actions.appendChild(yes);
    banner.appendChild(p); banner.appendChild(actions);
    banner.addEventListener('click', function (e) {
      var c = e.target && e.target.getAttribute && e.target.getAttribute('data-ds-choice');
      if (c) choose(c);
    });
    D.body.appendChild(banner);
  }
  function hideBanner() { if (banner && banner.parentNode) banner.parentNode.removeChild(banner); banner = null; }
  function choose(choice) {
    hideBanner();
    var headers = { 'Content-Type': 'application/json' }, tok = cookie('csrf');
    if (tok) headers['X-CSRF-Token'] = tok;
    fetch(CONSENT, { method: 'POST', credentials: 'same-origin', headers: headers, body: JSON.stringify({ choice: choice }) })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (!cfg) cfg = {};
        if (choice === 'granted' && res && res.ok) {
          cfg.vid = res.vid || cfg.vid; cfg.replay = !!res.replay; cfg.consent = 'granted';
          allowed = true; begin();
        } else if (choice === 'denied') {
          allowed = false; queue = []; cfg.consent = 'denied';
        }
      }).catch(function () { });
  }
  W.dsConsent = { open: function () { showBanner(true); }, state: function () { return cfg ? cfg.consent : 'unknown'; } };

  // ------------------------------------------------------------------------------------- lifecycle
  W.dsTrack = function (name, props) {
    if (!name) return;
    var d = {};
    if (props && typeof props === 'object') {
      Object.keys(props).slice(0, 10).forEach(function (k) {
        var v = props[k];
        if (typeof v === 'number' || typeof v === 'boolean') d[clip(k, 40)] = v;
        else if (typeof v === 'string') d[clip(k, 40)] = clip(v, 120);
      });
    }
    push({ t: 'custom', n: clip(name, 100), d: d });
  };

  function newPage() {
    flush(true);
    pv = newId('p_'); maxScroll = 0; lastBucket = -1; seenImp = {}; seenHover = {}; forms = {};
    sentLayout = false; layout = null; heatTotal = 0; vitalsSent = true;
    push({ t: 'pageview' });
    setTimeout(setupSections, 300);
  }

  function begin() {
    if (begun || !allowed) return;
    begun = true;
    fingerprint().then(function () { flush(false); });
    push({ t: 'pageview' });
    setupSections(); setupCtas(); setupVitals();
    on(W, 'scroll', onScroll); onScroll();
    on(D, 'click', onClick);
    on(D, 'mousemove', onMove);
    on(D, 'keydown', function () { lastActivity = now(); });
    on(D, 'touchstart', function () { lastActivity = now(); });
    on(D, 'focusin', onFocus);
    D.addEventListener('submit', onSubmit, true);
    on(W, 'error', function (e) { if (e && e.message) reportError(e.message, e.filename, e.lineno, e.colno); });
    on(W, 'unhandledrejection', function (e) { var r = e && e.reason; reportError('unhandled: ' + (r && r.message ? r.message : String(r)), '', 0, 0); });
    if (W.MutationObserver && D.body) { mo = new MutationObserver(function () { mutated++; }); mo.observe(D.body, { childList: true, subtree: true, attributes: true, characterData: true }); }
    setInterval(tick, HB_MS);
    setInterval(function () { flush(false); }, FLUSH_MS);
    on(D, 'visibilitychange', function () {
      if (D.visibilityState === 'hidden') { tick(); sendVitals(); collectDwell(); flush(true); }
      else { lastTick = now(); Object.keys(intersecting).forEach(function (n) { if (intersecting[n]) openSpan(n); }); }
    });
    on(W, 'pagehide', function () { tick(); sendVitals(); flush(true); });
    ['pushState', 'replaceState'].forEach(function (m) {
      var orig = history[m];
      history[m] = function () { var before = location.pathname; var r = orig.apply(this, arguments); if (m === 'pushState' && location.pathname !== before) newPage(); return r; };
    });
    on(W, 'popstate', newPage);
    startReplay();
  }

  function boot() {
    fillForms();
    var opener = function (e) { if (closest(e.target, '[data-ds-consent-open]')) { e.preventDefault(); W.dsConsent.open(); } };
    D.addEventListener('click', opener, true);
    if (!W.fetch) return;
    fetch(CFG, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (c) {
        if (!c) return;
        cfg = c;
        if (!c.tracking) return;
        if (c.consent === 'granted' || c.consent === 'implied') { allowed = true; begin(); }
        if (c.consent === 'pending') showBanner(true);
        else if (c.consent === 'implied') showBanner(false);
      }).catch(function () { });
  }

  if (D.readyState === 'loading') D.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
