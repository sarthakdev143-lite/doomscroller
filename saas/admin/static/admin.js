/* Admin panel behaviour. No dependencies, no inline handlers (the CSP forbids them).
 *
 *  form[data-api]    submit -> JSON POST to data-api (or data-method), with X-CSRF-Token
 *  [data-api] button  same, body from data-body (JSON)
 *  data-confirm       asks first; data-redirect goes somewhere after success, else the page reloads
 *  table[data-live]   polls that URL every 5s and redraws the rows (text only, never HTML)
 *  tr[data-href]      the whole row is a link
 */
(function () {
  'use strict';
  var D = document;

  function cookie(name) {
    var m = D.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }

  var toastEl = null, toastTimer = null;
  function toast(msg, isError) {
    toastEl = toastEl || D.querySelector('.adm-toast');
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.classList.toggle('is-error', !!isError);
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.hidden = true; }, isError ? 7000 : 3000);
  }

  function call(url, method, body) {
    var headers = { 'Accept': 'application/json', 'X-CSRF-Token': cookie('csrf') };
    var opts = { method: method || 'POST', credentials: 'same-origin', headers: headers };
    if (body !== undefined && opts.method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts).then(function (r) {
      return r.text().then(function (t) {
        var data = null;
        try { data = t ? JSON.parse(t) : {}; } catch (e) { data = { message: t.slice(0, 200) }; }
        if (!r.ok) throw new Error((data && (data.message || data.error)) || ('HTTP ' + r.status));
        return data;
      });
    });
  }

  function formBody(form) {
    var out = {};
    new FormData(form).forEach(function (v, k) {
      if (typeof v !== 'string') return;
      out[k] = v;
    });
    return out;
  }

  function run(el, body) {
    var msg = el.getAttribute('data-confirm');
    if (msg && !window.confirm(msg)) return;
    var btn = el.tagName === 'FORM' ? el.querySelector('[type=submit]') : el;
    if (btn) btn.disabled = true;
    call(el.getAttribute('data-api'), el.getAttribute('data-method') || 'POST', body)
      .then(function (res) {
        toast(res && res.message ? res.message : 'Done.');
        var to = el.getAttribute('data-redirect');
        setTimeout(function () { if (to) window.location.href = to; else window.location.reload(); }, 350);
      })
      .catch(function (err) { toast(err.message || 'That did not work.', true); if (btn) btn.disabled = false; });
  }

  D.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || !form.hasAttribute || !form.hasAttribute('data-api')) return;
    e.preventDefault();
    run(form, formBody(form));
  });

  D.addEventListener('click', function (e) {
    var el = e.target && e.target.closest ? e.target.closest('[data-api]') : null;
    if (el && el.tagName !== 'FORM') {
      e.preventDefault();
      var body = {};
      var raw = el.getAttribute('data-body');
      if (raw) { try { body = JSON.parse(raw); } catch (err) { body = {}; } }
      run(el, body);
      return;
    }
    var row = e.target && e.target.closest ? e.target.closest('tr[data-href]') : null;
    if (row && !e.target.closest('a,button,input,select,textarea,label')) {
      if (e.metaKey || e.ctrlKey) window.open(row.getAttribute('data-href'), '_blank');
      else window.location.href = row.getAttribute('data-href');
    }
  });

  // ---- live page ---------------------------------------------------------------------------------
  function td(text, cls) {
    var c = D.createElement('td');
    if (cls) c.className = cls;
    c.textContent = text == null ? '' : String(text);
    return c;
  }
  function flag(cc) {
    if (!cc || cc.length !== 2) return '';
    return String.fromCodePoint(0x1F1E6 + cc.charCodeAt(0) - 65, 0x1F1E6 + cc.charCodeAt(1) - 65);
  }
  function ago(iso) {
    var s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    return s < 60 ? Math.round(s) + 's ago' : Math.round(s / 60) + 'm ago';
  }
  function chip(text, cls) {
    var s = D.createElement('span'); s.className = 'chip ' + cls; s.textContent = text; return s;
  }
  function renderLive(table, data) {
    var tbody = table.tBodies[0];
    var rows = data.rows || [];
    var frag = D.createDocumentFragment();
    if (!rows.length) {
      var tr0 = D.createElement('tr'); tr0.className = 'adm-empty-row';
      var c0 = td('Nobody on the site right now.'); c0.colSpan = 8; tr0.appendChild(c0); frag.appendChild(tr0);
    }
    rows.forEach(function (r) {
      var tr = D.createElement('tr');
      tr.setAttribute('data-href', '/admin/visitors/' + encodeURIComponent(r.visitor));
      tr.appendChild(td(flag(r.country) + ' ' + (r.city || r.country || '—')));
      tr.appendChild(td(r.path || '—', 'adm-mono'));
      tr.appendChild(td((r.device || '') + ' · ' + (r.browser || '')));
      tr.appendChild(td(r.source || 'direct'));
      tr.appendChild(td(r.pageviews, 'num'));
      tr.appendChild(td(r.active_s + 's', 'num'));
      var lead = td('');
      var sc = D.createElement('span'); sc.className = 'adm-score'; sc.style.setProperty('--s', r.score || 0);
      var inner = D.createElement('span'); inner.textContent = r.score || 0; sc.appendChild(inner);
      lead.appendChild(sc); lead.appendChild(D.createTextNode(' '));
      var map = { 'ready to buy': 'chip-hot', 'comparing prices': 'chip-warm', 'developer / API': 'chip-dev', 'bot': 'chip-bad' };
      lead.appendChild(chip(r.label || 'new', map[r.label] || 'chip-plain'));
      if (r.waitlist) lead.appendChild(chip('waitlist', 'chip-ok'));
      if (r.user) lead.appendChild(chip('user', 'chip-ok'));
      tr.appendChild(lead);
      tr.appendChild(td(ago(r.last_seen_at), 'adm-muted'));
      frag.appendChild(tr);
    });
    tbody.textContent = '';
    tbody.appendChild(frag);
    var count = D.querySelector('[data-live-count]');
    if (count) count.textContent = data.count;
  }
  var live = D.querySelector('table[data-live]');
  if (live) {
    var url = live.getAttribute('data-live');
    var tick = function () {
      if (D.visibilityState !== 'visible') return;
      call(url, 'GET').then(function (data) { renderLive(live, data); }).catch(function () { });
    };
    setInterval(tick, 5000);
  }
})();
