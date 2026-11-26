/* Mounts the vendored rrweb-player (window.rrwebPlayer, MIT) on #replay. The recorded page is
 * rebuilt inside a sandboxed iframe without scripts; inputs were masked at record time. */
(function () {
  'use strict';
  var el = document.getElementById('replay');
  if (!el) return;
  function fail(msg) { el.textContent = msg; }
  function start() {
    var P = window.rrwebPlayer && (window.rrwebPlayer.Player || window.rrwebPlayer.default);
    if (!P) { fail('The replay player did not load.'); return; }
    fetch(el.getAttribute('data-src'), { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (events) {
        if (!events || events.length < 2) { fail('This recording is too short to play.'); return; }
        el.textContent = '';
        var width = Math.max(320, Math.min(el.clientWidth - 24, 1280));
        new P({
          target: el,
          props: { events: events, width: width, height: Math.round(width * 0.62), autoPlay: false,
            skipInactive: true, showController: true, mouseTail: true, speedOption: [1, 2, 4, 8] }
        });
      })
      .catch(function (e) { fail('Could not load the recording (' + e.message + ').'); });
  }
  if (document.readyState === 'complete') start(); else window.addEventListener('load', start);
})();
