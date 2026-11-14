"""The control panel's HTML, CSS and JS, served by app/review.py.

One self-contained page, no build step and no external libraries, so it loads fast on a phone over a
tunnel. Five tabs along the bottom: Review, Channels, Status, Report, Controls. Each tab fetches its own
data from the /api routes and the whole thing is one dark, touch-first layout.
"""
from __future__ import annotations

UNCONFIGURED = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Doomscroller — Setup</title><body style="font:15px/1.5 system-ui;background:#0f0f10;color:#f2f2f2;padding:24px;max-width:480px;margin:0 auto">
<h1 style="font-size:18px;margin:0 0 8px">Control Panel Locked (Setup Needed)</h1>
<p style="color:#a0a0a8;margin:0 0 14px"><code>REVIEW_TOKEN</code> is not set in <code>.env</code>, so the panel refuses connections to keep your channels safe.</p>
<div style="background:#1b1b1e;border:1px solid #2a2a2e;border-radius:12px;padding:12px;font-size:13px;color:#cfe0ff">
Run <code>python scripts/run.py --ensure</code> (or <code>start.bat</code>) — it creates <code>.env</code>, generates a strong <code>REVIEW_TOKEN</code>, and prints your one-click unlock link.
</div></body>"""

LOCKED = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Doomscroller</title><body style="font:16px system-ui;background:#0f0f10;color:#f2f2f2;padding:24px;max-width:420px;margin:0 auto">
<h1 style="font-size:18px;margin:0 0 8px">Doomscroller</h1>
<p style="color:#a0a0a8;font-size:14px;margin:0 0 16px">Enter your review key, or open this page with the link that has <code>?key=</code> in it.</p>
<form method="get" action="/" style="display:grid;gap:10px">
<input type="password" name="key" placeholder="Review key" autocomplete="current-password" required
 style="width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #2a2a2e;background:#1b1b1e;color:#f2f2f2;font-size:15px">
<button type="submit" style="padding:12px;border-radius:10px;border:0;background:#4f8cff;color:#06121f;font-weight:700;font-size:15px;cursor:pointer">Unlock</button>
</form></body>"""

_ICON_SVG = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 128 128'%3E"
    "%3Crect width='128' height='128' rx='28' fill='%230f0f10'/%3E"
    "%3Crect x='28' y='18' width='72' height='92' rx='14' fill='%231b1b1e' stroke='%234f8cff' stroke-width='6'/%3E"
    "%3Cpolygon points='54,46 54,82 82,64' fill='%234f8cff'/%3E%3C/svg%3E"
)

MANIFEST = {
    "name": "Doomscroller",
    "short_name": "Doomscroller",
    "description": "YouTube Shorts studio, review queue, and channel control panel.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0f0f10",
    "theme_color": "#0f0f10",
    "icons": [{"src": _ICON_SVG, "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
}

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f0f10">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 128 128'%3E%3Crect width='128' height='128' rx='28' fill='%230f0f10'/%3E%3Cpolygon points='46,36 46,92 92,64' fill='%234f8cff'/%3E%3C/svg%3E">
<title>Doomscroller</title>
<style>
:root{--bg:#0f0f10;--card:#1b1b1e;--text:#f2f2f2;--dim:#a0a0a8;--ok:#2fbf71;--no:#e5484d;--line:#2a2a2e;--accent:#4f8cff;--warn:#f5a524}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,sans-serif;
 padding-bottom:calc(64px + env(safe-area-inset-bottom))}
header{position:sticky;top:0;background:var(--bg);padding:12px 16px 8px;border-bottom:1px solid var(--line);z-index:3}
.hwrap{max-width:600px;margin:0 auto;display:flex;align-items:baseline;gap:10px}
h1{font-size:16px;margin:0;font-weight:700}
#conn{font-size:12px;color:var(--dim);margin-left:auto}
#conn.off{color:var(--warn)}
main{max-width:600px;margin:0 auto;padding:12px 16px 24px;display:grid;gap:12px}
.tabpane[hidden]{display:none}
h2{font-size:14px;margin:6px 0 0;color:var(--dim);font-weight:600;letter-spacing:.02em;text-transform:uppercase}
.sub{color:var(--dim);font-size:13px}
.muted{color:var(--dim)}
.empty{color:var(--dim);text-align:center;padding:36px 0}
.err{color:var(--no);font-size:13px}
/* tab bar */
nav{position:fixed;left:0;right:0;bottom:0;background:#141416;border-top:1px solid var(--line);
 display:flex;z-index:4;padding-bottom:env(safe-area-inset-bottom)}
nav button{flex:1;background:none;border:0;color:var(--dim);font:600 11px system-ui;padding:9px 2px 10px;
 display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer}
nav button .dot{width:5px;height:5px;border-radius:50%;background:var(--warn);visibility:hidden}
nav button.on{color:var(--text)}
nav button.on .ic{color:var(--accent)}
nav button .ic{font-size:17px;line-height:1}
nav button.alert .dot{visibility:visible}
/* cards & chrome shared */
.card{background:var(--card);border-radius:14px;overflow:hidden;border:1px solid var(--line)}
.card.flagged{border-color:var(--warn)}
.pill{display:inline-block;font-size:11px;font-weight:600;color:var(--accent);border:1px solid var(--accent);
 border-radius:999px;padding:2px 8px;letter-spacing:.02em}
.tag{display:inline-block;font-size:11px;font-weight:600;border-radius:999px;padding:2px 9px;border:1px solid var(--line);color:var(--dim)}
.tag.on{color:var(--ok);border-color:var(--ok)}.tag.off{color:var(--dim)}
.tag.warn{color:var(--warn);border-color:var(--warn)}.tag.bad{color:var(--no);border-color:var(--no)}
button{border:0;border-radius:10px;font-weight:600;cursor:pointer;font-size:14px}
.btn{background:var(--accent);color:#06121f;padding:12px}
.ghost{background:transparent;color:var(--text);border:1px solid var(--line);padding:10px}
.danger{background:transparent;color:var(--no);border:1px solid var(--no);padding:10px}
.go{color:var(--ok);border-color:var(--ok)}.arm{color:var(--warn);border-color:var(--warn)}
button:disabled{opacity:.5}
input,select,textarea{width:100%;padding:11px;border-radius:10px;border:1px solid var(--line);background:#111;color:var(--text);font-size:15px;font-family:inherit}
textarea{min-height:64px;resize:vertical}
label{font-size:12px;color:var(--dim);display:block;margin:0 0 4px}
.field{margin-top:8px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.banner{background:#3a1416;border:1px solid var(--no);color:#ffb4b4;border-radius:10px;padding:10px 12px;font-size:13px;font-weight:600}
/* review */
.chips{display:flex;gap:8px;overflow-x:auto;padding:2px 0 2px;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{padding:7px 13px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--dim);
 white-space:nowrap;font-size:13px;cursor:pointer;user-select:none}
.chip.on{background:var(--text);color:var(--bg);border-color:var(--text);font-weight:600}
.chip.att{border-color:var(--warn)}.chip .n{opacity:.75;margin-left:5px;font-variant-numeric:tabular-nums}
#strip{display:flex;gap:10px;overflow-x:auto;padding:2px 0;-webkit-overflow-scrolling:touch;scrollbar-width:none}
#strip::-webkit-scrollbar{display:none}
.cc{min-width:150px;flex:0 0 auto;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:9px 11px}
.cc.att{border-color:var(--warn)}
.cc h3{font-size:13px;margin:0 0 3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cc .s{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums}
.cc .attn{color:var(--warn);font-size:12px;font-weight:600;margin-top:3px}
.cbtn{margin-top:8px;width:100%;padding:8px;font-size:12px;background:transparent;color:var(--dim);border:1px solid var(--line);border-radius:9px}
video{width:100%;aspect-ratio:9/16;max-height:60vh;background:#000;display:block}
.body{padding:12px;display:grid;gap:8px}.meta{color:var(--dim);font-size:13px}
.warnbox{background:#3a2a10;border:1px solid var(--warn);color:#f5d58a;border-radius:8px;padding:8px 10px;font-size:13px;font-weight:600}
.actions{display:grid;gap:8px}
.yes{background:var(--ok);color:#06240f;padding:15px;font-size:16px}
.nope{background:transparent;color:var(--no);border:1px solid var(--no);padding:11px;font-size:14px}
.card.flagged .yes{background:transparent;color:var(--dim);border:1px solid var(--line);padding:11px;font-size:14px;order:2}
.card.flagged .nope{background:var(--no);color:#fff;border:0;padding:15px;font-size:16px;order:1}
/* channel cards */
.chan{padding:12px;display:grid;gap:8px}
.chan .top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.chan .nm{font-weight:700}.chan .sl{color:var(--dim);font-size:12px}
.step{background:#10233a;border:1px solid #27456d;color:#cfe0ff;border-radius:8px;padding:8px 10px;font-size:13px}
.feeds{display:flex;flex-wrap:wrap;gap:6px}
.feed{display:flex;align-items:center;gap:6px;background:#111;border:1px solid var(--line);border-radius:999px;padding:4px 6px 4px 10px;font-size:12px;color:var(--dim);max-width:100%}
.feed span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:200px}
.feed button{background:none;border:0;color:var(--no);font-size:15px;line-height:1;padding:0 4px;cursor:pointer}
.rowbtns{display:flex;gap:8px;flex-wrap:wrap}
.rowbtns button{flex:1;min-width:120px}
details.sx{border-top:1px solid var(--line);padding-top:8px}
details.sx>summary{cursor:pointer;color:var(--dim);font-size:13px;list-style:none}
details.sx>summary::-webkit-details-marker{display:none}
/* doctor */
.chk{display:flex;gap:9px;align-items:flex-start;padding:7px 0;border-bottom:1px solid var(--line)}
.chk:last-child{border-bottom:0}
.chk .d{width:9px;height:9px;border-radius:50%;margin-top:5px;flex:0 0 auto}
.chk.ok .d{background:var(--ok)}.chk.warn .d{background:var(--warn)}.chk.bad .d{background:var(--no)}
.chk .n{font-weight:600;font-size:13px}.chk .t{color:var(--dim);font-size:12px}
.ready{font-weight:700}.ready.y{color:var(--ok)}.ready.n{color:var(--warn)}
/* report */
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.metric{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 6px;text-align:center}
.metric b{display:block;font-size:19px}.metric span{color:var(--dim);font-size:11px}
.rowline{display:flex;justify-content:space-between;gap:10px;padding:7px 0;border-bottom:1px solid var(--line);font-size:13px}
.rowline:last-child{border-bottom:0}.rowline .r{color:var(--dim);text-align:right;flex:0 0 auto}
/* controls */
.act{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;display:grid;gap:4px}
.act .top{display:flex;align-items:center;gap:10px}
.act .lb{font-weight:600}.act .st{font-size:12px;margin-left:auto;color:var(--dim)}
.act .st.run{color:var(--accent)}.act .st.err{color:var(--no)}.act .st.done{color:var(--ok)}
.act .ab{color:var(--dim);font-size:12px}
.snack{position:fixed;left:50%;bottom:76px;transform:translateX(-50%);background:#2a2a2e;color:var(--text);
 border:1px solid var(--line);border-radius:10px;padding:10px 16px;display:flex;gap:16px;align-items:center;
 font-size:14px;box-shadow:0 6px 20px rgba(0,0,0,.45);z-index:6;max-width:92vw}
.snack button{background:none;color:var(--accent);border:0;font-weight:700;font-size:14px;padding:4px}
.post{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;display:grid;gap:4px}
.post a{color:var(--text);font-weight:600;text-decoration:none}.nums{display:flex;gap:14px;flex-wrap:wrap;color:var(--dim);font-size:13px}
.verdict{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}.winner{color:var(--ok)}
.weak_hold,.no_reach,.low_views{color:var(--warn)}.why{color:var(--dim);font-size:13px}
.card.kb-focus{outline:2px solid var(--accent);outline-offset:2px}
.modes{display:flex;gap:6px;background:#141416;padding:4px;border-radius:11px;border:1px solid var(--line)}
.modes button{flex:1;background:transparent;color:var(--dim);padding:7px 8px;border-radius:8px;font-size:12px;font-weight:600}
.modes button.on{background:var(--card);color:var(--text);border:1px solid var(--line)}
.trow{display:flex;gap:8px;align-items:center}.tcnt{font-size:11px;color:var(--dim);font-variant-numeric:tabular-nums;flex:0 0 auto}
.tcnt.over{color:var(--warn)}
.badges{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.srclink{color:var(--accent);text-decoration:none;font-weight:600}
.dropzone{border:1px dashed var(--line);border-radius:10px;padding:10px;transition:border-color .15s,background .15s}
.dropzone.dragover{border-color:var(--accent);background:#142033}
.kbd-btn{background:transparent;border:1px solid var(--line);color:var(--dim);border-radius:8px;padding:2px 8px;font-size:12px;display:none}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.65);display:grid;place-items:center;z-index:10;padding:16px}
.modal[hidden]{display:none}
.modal .box{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;max-width:380px;width:100%;display:grid;gap:8px}
kbd{background:#111;border:1px solid var(--line);border-radius:5px;padding:1px 6px;font:12px ui-monospace,monospace;color:var(--text)}
@media(min-width:900px){
  body{padding-top:52px;padding-bottom:32px}
  .hwrap,main{max-width:1120px}
  nav{top:0;bottom:auto;border-top:0;border-bottom:1px solid var(--line);justify-content:center;gap:6px;padding:0 16px}
  nav button{flex:0 1 130px;flex-direction:row;justify-content:center;gap:7px;font-size:13px;padding:14px 10px}
  header{top:52px}
  .kbd-btn{display:inline-block}
  #list{grid-template-columns:repeat(auto-fill,minmax(310px,1fr));align-items:start}
  #ch-list,#posted,#ct-body{grid-template-columns:repeat(auto-fill,minmax(330px,1fr));align-items:start}
  #ct-body h2,#posted h2{grid-column:1/-1}
  .snack{bottom:24px}
}
</style></head><body>
<header><div class="hwrap"><h1 id="title">Review</h1><span id="conn"></span><button id="kbd-help" class="kbd-btn" title="Keyboard shortcuts (?)">?</button></div></header>

<main>
  <section id="tab-review" class="tabpane">
    <div id="rv-stats" class="sub">Loading…</div>
    <div id="banner" hidden></div>
    <div id="strip" hidden></div>
    <div class="modes" id="rv-modes"></div>
    <div id="rv-tools" style="display:flex;gap:8px;align-items:center" hidden>
      <input id="rv-search" type="search" placeholder="Search title, creator, hook or verdict… (/)" aria-label="Search clips" style="flex:1">
      <button id="rv-sort" class="ghost" style="white-space:nowrap;padding:10px 12px;font-size:12px" title="Toggle sort order">Sort: Newest</button>
      <button id="rv-speed" class="ghost" style="white-space:nowrap;padding:10px 12px;font-size:12px" title="Cycle playback speed (S)">Speed: 1x</button>
    </div>
    <div class="chips" id="chips"></div>
    <div id="rv-flagged" style="display:grid;gap:8px" hidden></div>
    <div id="list" style="display:grid;gap:12px"></div>
    <div id="posted" style="display:grid;gap:8px"></div>
  </section>

  <section id="tab-clip" class="tabpane" hidden>
    <div class="card" style="padding:14px;display:grid;gap:10px">
      <h2 style="margin:0">Cut a video into Shorts</h2>
      <div id="clip-form" style="display:grid;gap:10px"></div>
    </div>
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
      <h2 style="margin:0">Recent clip jobs</h2>
      <button id="clip-clear-finished" class="ghost" style="padding:6px 10px;font-size:12px" hidden>Clear finished</button>
    </div>
    <div id="clip-jobs" style="display:grid;gap:10px"></div>
  </section>

  <section id="tab-channels" class="tabpane" hidden>
    <div id="ch-list" style="display:grid;gap:12px"></div>
    <details class="card" style="padding:12px"><summary style="cursor:pointer;font-weight:600">+ Add a channel</summary>
      <div id="add-form" style="display:grid;gap:8px;margin-top:10px"></div>
    </details>
    <details class="card" style="padding:12px"><summary style="cursor:pointer;font-weight:600">Import channels JSON</summary>
      <div id="import-form" style="display:grid;gap:8px;margin-top:10px"></div>
    </details>
  </section>

  <section id="tab-status" class="tabpane" hidden>
    <div id="dc-head"></div>
    <div id="dc-body" style="display:grid;gap:12px"></div>
  </section>

  <section id="tab-report" class="tabpane" hidden>
    <div id="rp-pick"></div>
    <div id="rp-body" style="display:grid;gap:12px"></div>
  </section>

  <section id="tab-controls" class="tabpane" hidden>
    <div id="ct-body" style="display:grid;gap:10px"></div>
  </section>
</main>

<nav id="nav"></nav>
<div id="snack" class="snack" role="status" aria-live="polite" hidden></div>
<div id="kbd-modal" class="modal" hidden>
  <div class="box">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <b>Keyboard shortcuts</b>
      <button id="kbd-close" class="ghost" style="padding:4px 10px">Close</button>
    </div>
    <div class="rowline"><span>Next / previous clip</span><span class="r"><kbd>J</kbd> / <kbd>K</kbd></span></div>
    <div class="rowline"><span>Play / pause clip</span><span class="r"><kbd>Space</kbd></span></div>
    <div class="rowline"><span>Playback speed (1x / 1.5x / 2x)</span><span class="r"><kbd>S</kbd></span></div>
    <div class="rowline"><span>Approve / pull back</span><span class="r"><kbd>A</kbd></span></div>
    <div class="rowline"><span>Reject clip</span><span class="r"><kbd>X</kbd> or <kbd>R</kbd></span></div>
    <div class="rowline"><span>Edit title (Enter saves)</span><span class="r"><kbd>E</kbd></span></div>
    <div class="rowline"><span>Search clips / posts</span><span class="r"><kbd>/</kbd></span></div>
    <div class="rowline"><span>Undo last decision</span><span class="r"><kbd>U</kbd></span></div>
    <div class="rowline"><span>Switch tabs</span><span class="r"><kbd>1</kbd>–<kbd>6</kbd></span></div>
    <div class="rowline"><span>Close help / clear search</span><span class="r"><kbd>Esc</kbd></span></div>
  </div>
</div>

<script>
const $=id=>document.getElementById(id);
function el(t,a,x){const e=document.createElement(t);Object.assign(e,a||{});if(x!=null)e.textContent=x;return e}
function fmt(iso){return iso?new Date(iso).toLocaleString([],{weekday:'short',hour:'2-digit',minute:'2-digit'}):'—'}
function ago(iso){if(!iso)return'';const s=(Date.now()-new Date(iso))/1000;if(s<60)return'just now';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago'}
const SPEEDS=[1,1.5,2];let playbackSpeed=1;
try{const sv=Number(localStorage.getItem('reviewSpeed'));if(SPEEDS.includes(sv))playbackSpeed=sv}catch(e){}
function applySpeed(v){if(v){try{v.defaultPlaybackRate=playbackSpeed;v.playbackRate=playbackSpeed}catch(e){}}}
document.addEventListener('play',e=>{if(e.target&&e.target.tagName==='VIDEO'){applySpeed(e.target);for(const v of document.querySelectorAll('video'))if(v!==e.target&&!v.paused)v.pause()}},true);
let connOff=false;
function setConn(off){connOff=off;$('conn').textContent=off?'offline':'';$('conn').className=off?'off':''}
const api={
  get:u=>fetch(u,{credentials:'same-origin'}).then(r=>{if(!r.ok)throw r;setConn(false);return r.json()}).catch(e=>{if(e instanceof Response)throw e;setConn(true);throw e}),
  post:(u,b)=>fetch(u,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})})
    .then(async r=>{if(!r.ok){let m='Failed ('+r.status+')';try{m=(await r.json()).detail||m}catch(e){}if(r.status===401)m='Session expired — reopen your link.';throw new Error(m)}setConn(false);return r.json()})
};
const snack=$('snack');let snackTimer=null,lastUndoId=null;
async function performUndo(id){
  if(!id)return;
  clearTimeout(snackTimer);snack.hidden=true;
  if(lastUndoId===id)lastUndoId=null;
  try{await api.post('/api/clips/'+encodeURIComponent(id)+'/undo');toast('Moved back to Review')}catch(e){alert(e.message)}
  loaders.review();
}
function toast(label,undoId){
  clearTimeout(snackTimer);snack.replaceChildren(el('span',{},label));
  if(undoId){lastUndoId=undoId;const u=el('button',{},'Undo');u.onclick=()=>performUndo(undoId);snack.append(u)}
  snack.hidden=false;snackTimer=setTimeout(()=>snack.hidden=true,6000);
}
async function copyText(txt,msg){
  try{await navigator.clipboard.writeText(txt);toast(msg);return}catch(e){}
  try{const ta=el('textarea',{value:txt,style:'position:fixed;opacity:0;left:-9999px'});document.body.append(ta);ta.select();if(document.execCommand('copy')){ta.remove();toast(msg);return}ta.remove()}catch(e){}
  prompt(msg+':',txt);
}

/* ---------- tabs ---------- */
const TABS=[['review','Review','▦'],['clip','Clip','✂'],['channels','Channels','≡'],['status','Status','◌'],['report','Report','▤'],['controls','Controls','⚙']];
let active='review';
function show(id){
  active=id;$('title').textContent=TABS.find(t=>t[0]===id)[1];
  for(const [t] of TABS){$('tab-'+t).hidden=t!==id;$('nav-'+t).classList.toggle('on',t===id)}
  loaders[id]&&loaders[id]();
}
for(const [id,label,ic] of TABS){
  const b=el('button',{id:'nav-'+id,onclick:()=>show(id)});
  b.append(el('span',{className:'ic'},ic),el('span',{},label),el('span',{className:'dot'}));
  $('nav').append(b);
}

/* ===================== REVIEW ===================== */
const edits={},cards=new Map();let lastQ=null,filter='all',reviewMode='review',searchQuery='',sortBy='newest',armedResume=null,armedFlagReject=false,flagArmTimer=null,armedCleanApprove=false,cleanArmTimer=null,kbIdx=-1;
try{filter=localStorage.getItem('reviewFilter')||'all'}catch(e){}
const rvTools=$('rv-tools'),rvSearch=$('rv-search'),rvSort=$('rv-sort'),rvSpeed=$('rv-speed');
function syncSpeedUi(){rvSpeed.textContent='Speed: '+playbackSpeed+'x';for(const v of document.querySelectorAll('video'))applySpeed(v)}
function cycleSpeed(){playbackSpeed=SPEEDS[(SPEEDS.indexOf(playbackSpeed)+1)%SPEEDS.length];try{localStorage.setItem('reviewSpeed',String(playbackSpeed))}catch(e){}syncSpeedUi();toast('Playback speed: '+playbackSpeed+'x')}
rvSpeed.onclick=cycleSpeed;syncSpeedUi();
rvSearch.addEventListener('input',()=>{searchQuery=rvSearch.value.trim().toLowerCase();kbIdx=-1;renderReview()});
rvSearch.addEventListener('keydown',e=>{if(e.key==='Escape'){if(rvSearch.value){rvSearch.value='';searchQuery='';renderReview()}rvSearch.blur()}});
rvSort.onclick=()=>{sortBy=sortBy==='newest'?'score':'newest';rvSort.textContent='Sort: '+(sortBy==='score'?'Top score':'Newest');cards.clear();$('list').replaceChildren();renderReview()};
function matchesSearch(item){
  if(!searchQuery)return true;
  const hay=[item.title,item.description,(item.tags||[]).join(' '),item.creator,item.hook,item.why,item.platform,item.kind,item.resolution,item.channel,item.verdict,item.flag].filter(Boolean).join(' ').toLowerCase();
  return hay.includes(searchQuery);
}
function reviewBusy(){const a=document.activeElement;return(a&&(a.tagName==='INPUT'||a.tagName==='TEXTAREA')&&$('list').contains(a))||[...$('list').querySelectorAll('video')].some(v=>!v.paused&&!v.ended)}
function setFilter(f){filter=f;armedFlagReject=false;armedCleanApprove=false;kbIdx=-1;try{localStorage.setItem('reviewFilter',f)}catch(e){}renderReview()}
function setReviewMode(m){reviewMode=m;armedFlagReject=false;armedCleanApprove=false;kbIdx=-1;cards.clear();$('list').replaceChildren();renderReview()}
function sourceHref(url,start){
  if(!url||!/^https?:\/\//i.test(url))return'';
  if(start&&start>0&&/(youtube\.com\/watch|youtu\.be\/)/i.test(url)&&!/[?&]t=/.test(url)){
    return url+(url.includes('?')?'&':'?')+'t='+Math.floor(start)+'s';
  }
  return url;
}
function renderModes(d){
  const box=$('rv-modes');box.replaceChildren();
  const qCount=(d.queued||[]).length,pCount=(d.posted||[]).length,sCount=(d.scraped||[]).length;
  const modes=[['review','To review ('+d.clips.length+')']];
  if(sCount>0||reviewMode==='scraped')modes.push(['scraped','Scraped ('+sCount+')']);
  modes.push(['queued','Up next ('+qCount+')'],['posted','Posted ('+pCount+')']);
  for(const [id,lbl] of modes){
    const b=el('button',{className:reviewMode===id?'on':''},lbl);
    b.onclick=()=>setReviewMode(id);box.append(b);
  }
}
function renderChips(channels){
  $('chips').replaceChildren();if(!channels.length||reviewMode==='posted'||reviewMode==='scraped')return;
  const countFor=c=>reviewMode==='queued'?(c.approved||0):(c.waiting||0);
  const mk=(slug,label,n,att)=>{const c=el('div',{className:'chip'+(filter===slug?' on':'')+(att?' att':'')});c.append(el('span',{},label));if(n)c.append(el('span',{className:'n'},n));c.onclick=()=>setFilter(slug);return c};
  $('chips').append(mk('all','All',channels.reduce((n,c)=>n+countFor(c),0),false));
  for(const c of channels)$('chips').append(mk(c.slug,c.name,countFor(c),!!c.attention));
}
function pauseBtn(c){
  const needsSecond=c.paused&&!c.owner_paused,armed=armedResume===c.slug&&needsSecond;
  const b=el('button',{className:'cbtn'+(armed?' arm':c.paused?' go':'')},armed?'Tap again to resume':c.paused?'Resume posting':'Pause posting');
  b.onclick=async()=>{
    if(needsSecond&&!armed){armedResume=c.slug;renderReview();setTimeout(()=>{if(armedResume===c.slug){armedResume=null;renderReview()}},6000);return}
    armedResume=null;b.disabled=true;
    try{await api.post('/api/channels/'+encodeURIComponent(c.slug)+'/'+(c.paused?'resume':'pause'));loaders.review()}
    catch(e){b.disabled=false;alert(e.message)}
  };return b;
}
function renderStrip(channels){
  const box=$('strip');if(!channels.length){box.hidden=true;return}box.hidden=false;box.replaceChildren();
  for(const c of channels){const card=el('div',{className:'cc'+(c.attention?' att':'')});
    card.append(el('h3',{},c.name),el('div',{className:'s'},c.waiting+' to review · '+c.approved+' queued'),
      el('div',{className:'s'},c.posted_today+' today · next '+fmt(c.next_post)));
    if(c.attention)card.append(el('div',{className:'attn'},c.attention));
    card.append(pauseBtn(c));box.append(card)}
}
function saveMp4Link(id){
  return el('a',{href:'/api/media/'+encodeURIComponent(id)+'?download=1',download:'short-'+id+'.mp4',className:'srclink'},'Save MP4');
}
function renderPosted(items){
  const box=$('posted');
  if(reviewMode==='queued'){box.hidden=true;return}
  box.hidden=false;box.replaceChildren(el('h2',{},reviewMode==='posted'?'Posted Shorts':'Recently posted'));
  if(!items.length){box.append(el('div',{className:'empty'},'Nothing posted yet.'));return}
  for(const p of items){const card=el('div',{className:'post'});
    card.append(el('a',{href:p.url,target:'_blank',rel:'noopener'},p.title||'Untitled'));
    const nums=el('div',{className:'nums'});nums.append(el('span',{},p.views.toLocaleString()+' views'),el('span',{},p.likes.toLocaleString()+' likes'));
    if(p.watched!==null)nums.append(el('span',{},p.watched+'% watched'));nums.append(el('span',{},new Date(p.posted).toLocaleDateString()));
    if(p.has_media&&p.id)nums.append(saveMp4Link(p.id));
    card.append(nums);
    if(p.verdict){card.append(el('div',{className:'verdict '+p.verdict},p.verdict.replace('_',' ')));card.append(el('div',{className:'why'},p.why))}
    box.append(card)}
}
function renderFlaggedBar(pool){
  const box=$('rv-flagged');
  const clean=(reviewMode==='review')?pool.filter(c=>!c.flag&&!c.scraped):[];
  const flagged=(reviewMode==='review')?pool.filter(c=>c.flag):[];
  if(!clean.length&&!flagged.length){armedFlagReject=false;armedCleanApprove=false;box.hidden=true;box.replaceChildren();return}
  box.hidden=false;box.replaceChildren();
  if(clean.length){
    const cn=clean.length;
    const crow=el('div',{className:'act',style:'display:flex;flex-direction:row;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;padding:10px 12px'});
    crow.append(el('span',{style:'font-size:13px'},cn+' unflagged clip'+(cn===1?'':'s')+' ready in review'));
    const cbtn=el('button',{className:(armedCleanApprove?'yes':'ghost'),style:'padding:7px 12px;font-size:12px;white-space:nowrap'},
      armedCleanApprove?('Tap again to approve '+cn+' unflagged'):(cn===1?'Approve 1 unflagged clip':('Approve all '+cn+' unflagged')));
    cbtn.onclick=async()=>{
      if(!armedCleanApprove){
        armedCleanApprove=true;renderFlaggedBar(pool);
        clearTimeout(cleanArmTimer);cleanArmTimer=setTimeout(()=>{if(armedCleanApprove){armedCleanApprove=false;renderReview()}},4500);
        return;
      }
      clearTimeout(cleanArmTimer);armedCleanApprove=false;cbtn.disabled=true;
      try{
        const r=await api.post('/api/clips/approve-clean',{channel_slug:filter==='all'?'':filter});
        for(const id of (r.ids||[])){delete edits[id];const cd=cards.get(id);if(cd){cd.remove();cards.delete(id)}}
        toast('Approved '+(r.approved||0)+' unflagged clip'+((r.approved||0)===1?'':'s'),(r.ids&&r.ids.length===1)?r.ids[0]:null);
        loaders.review();
      }catch(e){cbtn.disabled=false;alert(e.message)}
    };
    crow.append(cbtn);box.append(crow);
  }else armedCleanApprove=false;
  if(flagged.length){
    const row=el('div',{className:'warnbox',style:'display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap'});
    const n=flagged.length;
    row.append(el('span',{},n+' flagged clip'+(n===1?'':'s')+' waiting in review'));
    const btn=el('button',{className:'danger'+(armedFlagReject?' arm':''),style:'padding:7px 12px;font-size:12px;white-space:nowrap'},
      armedFlagReject?('Tap again to reject '+n+' flagged'):(n===1?'Reject flagged clip':('Reject all '+n+' flagged')));
    btn.onclick=async()=>{
      if(!armedFlagReject){
        armedFlagReject=true;renderFlaggedBar(pool);
        clearTimeout(flagArmTimer);flagArmTimer=setTimeout(()=>{if(armedFlagReject){armedFlagReject=false;renderReview()}},4500);
        return;
      }
      clearTimeout(flagArmTimer);armedFlagReject=false;btn.disabled=true;
      try{
        const r=await api.post('/api/clips/reject-flagged',{channel_slug:filter==='all'?'':filter});
        for(const id of (r.ids||[])){delete edits[id];const cd=cards.get(id);if(cd){cd.remove();cards.delete(id)}}
        toast('Rejected '+(r.rejected||0)+' flagged clip'+((r.rejected||0)===1?'':'s'),(r.ids&&r.ids.length===1)?r.ids[0]:null);
        loaders.review();
      }catch(e){btn.disabled=false;alert(e.message)}
    };
    row.append(btn);box.append(row);
  }else armedFlagReject=false;
}
function clipCard(c,isQueued){
  const card=el('div',{className:'card'+(c.flag&&!isQueued?' flagged':'')});
  const vid=el('video',{src:'/media/'+encodeURIComponent(c.id),controls:true,playsInline:true,preload:'metadata'});
  applySpeed(vid);vid.addEventListener('loadedmetadata',()=>applySpeed(vid));
  card.append(vid);
  const b=el('div',{className:'body'});
  if(c.flag&&!isQueued)b.append(el('div',{className:'warnbox'},'Flagged — probably should NOT post: '+c.flag));
  const badges=el('div',{className:'badges'});
  if(c.channel)badges.append(el('span',{className:'pill'},c.channel));
  if(c.scraped)badges.append(el('span',{className:'tag warn'},'Scraped · '+(c.platform||'raw')));
  if(c.score!=null)badges.append(el('span',{className:'tag'},'Score '+c.score));
  if(isQueued&&c.approved_by)badges.append(el('span',{className:'tag on'},'Queued · '+c.approved_by));
  if(isQueued&&c.pinned)badges.append(el('span',{className:'tag warn'},'Publishing next'));
  if(c.made)badges.append(el('span',{className:'sub',style:'margin-left:auto;font-size:11px'},ago(c.made)));
  if(badges.childNodes.length)b.append(badges);

  let metaDirty=false,descInp=null,tagInp=null,sumEl=null;
  const trow=el('div',{className:'trow'});
  const t=el('input',{value:c.id in edits?edits[c.id]:c.title,'aria-label':'Clip title'});
  const cnt=el('span',{className:'tcnt'});
  const syncCnt=()=>{cnt.textContent=t.value.length+'/100';cnt.className='tcnt'+(t.value.length>100?' over':'')};
  syncCnt();
  t.addEventListener('input',()=>{edits[c.id]=t.value;syncCnt()});
  t.addEventListener('keydown',async e=>{
    if(e.key==='Escape'){t.blur();return}
    if(e.key==='Enter'){
      e.preventDefault();const v=t.value.trim();if(!v)return;
      try{
        const r=await api.post('/api/clips/'+encodeURIComponent(c.id)+'/title',{title:v});
        t.value=r.title;c.title=r.title;c.edited=true;delete edits[c.id];syncCnt();
        if(r.description&&descInp&&!metaDirty){descInp.value=r.description;c.description=r.description}
        if(sumEl)sumEl.textContent='Preview description & credit · edited';
        t.blur();toast('Title saved');
      }catch(err){alert(err.message)}
    }
  });
  trow.append(t,cnt);b.append(trow);

  const metaLine=el('div',{className:'meta'});
  const parts=[c.creator&&('Credit: '+c.creator),Math.round(c.seconds)+'s',c.resolution,c.kind].filter(Boolean);
  metaLine.textContent=parts.join(' · ');
  const href=sourceHref(c.source,c.start);
  if(href){if(metaLine.textContent)metaLine.append(document.createTextNode(' · '));metaLine.append(el('a',{href,target:'_blank',rel:'noopener',className:'srclink'},'Source ↗'))}
  if(c.has_media!==false&&c.id){if(metaLine.textContent)metaLine.append(document.createTextNode(' · '));metaLine.append(saveMp4Link(c.id))}
  b.append(metaLine);
  if(c.why)b.append(el('div',{className:'meta'},'Why: '+c.why));
  if(c.hook)b.append(el('div',{className:'meta'},'Hook: '+c.hook));
  if(c.audio)b.append(el('div',{className:'meta'},'Sound: '+c.audio));

  const mdDetails=el('details',{className:'sx'});
  sumEl=el('summary',{},'Preview description & credit'+(c.edited?' · edited':''));
  mdDetails.append(sumEl);
  const mdWrap=el('div',{style:'display:grid;gap:8px;margin-top:8px'});
  const descHead=el('div',{style:'display:flex;justify-content:space-between;align-items:baseline'});
  descHead.append(el('label',{style:'margin:0'},'YouTube description & creator credit'));
  const descCnt=el('span',{className:'tcnt'});
  descHead.append(descCnt);
  descInp=el('textarea',{rows:5,value:c.description||'','aria-label':'Clip description'});
  const syncDescCnt=()=>{
    const bytes=new TextEncoder().encode(descInp.value).length;
    descCnt.textContent=bytes+'/5000B';descCnt.className='tcnt'+(bytes>5000?' over':'');
  };
  syncDescCnt();
  descInp.addEventListener('input',()=>{metaDirty=true;syncDescCnt()});
  const tagBox=el('div');
  tagBox.append(el('label',{},'Tags (comma-separated)'));
  tagInp=el('input',{value:(c.tags||[]).join(', '),'aria-label':'Clip tags'});
  tagInp.addEventListener('input',()=>{metaDirty=true});
  tagBox.append(tagInp);
  const parseTags=()=>tagInp.value.split(',').map(s=>s.trim()).filter(Boolean);
  const saveMeta=el('button',{className:'ghost',style:'padding:8px 10px;font-size:13px'},'Save description & tags');
  saveMeta.onclick=async()=>{
    saveMeta.disabled=true;
    try{
      const payload={description:descInp.value,tags:parseTags()};
      if(t.value.trim())payload.title=t.value.trim();
      const r=await api.post('/api/clips/'+encodeURIComponent(c.id)+'/metadata',payload);
      t.value=r.title;descInp.value=r.description;tagInp.value=(r.tags||[]).join(', ');
      c.title=r.title;c.description=r.description;c.tags=r.tags;c.edited=true;metaDirty=false;
      delete edits[c.id];syncCnt();syncDescCnt();
      sumEl.textContent='Preview description & credit · edited';
      toast('Metadata saved');
    }catch(err){alert(err.message)}
    saveMeta.disabled=false;
  };
  mdWrap.append(descHead,descInp,tagBox,saveMeta);
  mdDetails.append(mdWrap);
  b.append(mdDetails);

  const approveBody=()=>metaDirty?{title:t.value,description:descInp.value,tags:parseTags()}:{title:t.value};
  const errLine=el('div',{className:'err'});errLine.hidden=true;
  const actions=el('div',{className:'actions'});
  const yes=el('button',{className:isQueued?'ghost':'yes'},isQueued?'Move back to Review':'Approve');
  const no=el('button',{className:'nope'},'Reject');
  let armed=false;
  const baseRoute=c.scraped&&!isQueued?'/api/scraped/':'/api/clips/';
  async function decide(path,body,msg,allowUndo){
    yes.disabled=no.disabled=true;errLine.hidden=true;
    try{await api.post(baseRoute+encodeURIComponent(c.id)+'/'+path,body||{});
      delete edits[c.id];cards.delete(c.id);card.remove();toast(msg,allowUndo?c.id:null);loaders.review();return}
    catch(e){errLine.textContent=e.message;errLine.hidden=false}
    card.style.transform='';card.style.opacity='';yes.disabled=no.disabled=false;
  }
  if(isQueued){
    yes.onclick=()=>decide('undo',{},'Moved back to Review',false);
    no.onclick=()=>decide('shelf-reject',{},'Rejected from queue',true);
  }else{
    yes.onclick=()=>{if(c.flag&&!armed){armed=true;yes.textContent='Tap again to approve anyway';setTimeout(()=>{armed=false;yes.textContent='Approve'},3000);return}decide('approve',approveBody(),'Approved',true)};
    no.onclick=()=>decide('reject',{},'Rejected',true);
  }
  actions.append(yes);
  if(isQueued){
    const pin=el('button',{className:'ghost'},c.pinned?'Unpin':'Publish next');
    pin.onclick=async()=>{
      pin.disabled=true;
      try{
        await api.post('/api/clips/'+encodeURIComponent(c.id)+'/pin',{pinned:!c.pinned});
        toast(c.pinned?'Unpinned':'Pinned — posts in the next window');
        cards.clear();$('list').replaceChildren();loaders.review();
      }catch(e){pin.disabled=false;alert(e.message)}
    };
    actions.append(pin);
  }
  actions.append(no);b.append(actions,errLine);card.append(b);
  card._video=vid;card._approve=()=>yes.click();card._reject=()=>no.click();card._editTitle=()=>{t.focus();t.select()};

  if(!isQueued){
    let tracking=false,horiz=false,startX=0,startY=0;card.style.transition='transform .2s ease, opacity .2s ease';
    const onCard=n=>!n.closest('video,input,textarea,details,summary,button,a');
    card.addEventListener('touchstart',e=>{if(e.touches.length!==1||!onCard(e.target)){tracking=false;return}startX=e.touches[0].clientX;startY=e.touches[0].clientY;tracking=true;horiz=false},{passive:true});
    card.addEventListener('touchmove',e=>{if(!tracking)return;const dx=e.touches[0].clientX-startX,dy=e.touches[0].clientY-startY;
      if(!horiz){if(Math.abs(dx)<12&&Math.abs(dy)<12)return;if(Math.abs(dy)>=Math.abs(dx)){tracking=false;return}horiz=true}
      e.preventDefault();card.style.transition='none';card.style.transform='translateX('+dx+'px)';card.style.opacity=String(Math.max(.35,1-Math.abs(dx)/350))},{passive:false});
    card.addEventListener('touchend',e=>{if(!tracking||!horiz){tracking=false;return}tracking=false;
      const dx=e.changedTouches[0].clientX-startX;card.style.transition='transform .2s ease, opacity .2s ease';const past=Math.abs(dx)>90,right=dx>0;
      if(past&&right&&!c.flag){card.style.transform='translateX(120%)';card.style.opacity='0';decide('approve',approveBody(),'Approved',true);return}
      if(past&&!right){card.style.transform='translateX(-120%)';card.style.opacity='0';decide('reject',{},'Rejected',true);return}
      card.style.transform='';card.style.opacity=''});
  }
  return card;
}
function renderReviewList(clips,isQueued,queuedTotal,scrapedTotal){
  const list=$('list');
  if(reviewMode==='posted'){list.hidden=true;return}
  list.hidden=false;
  const want=new Map(clips.map(c=>[c.id,c]));
  for(const [id,card] of cards){if(!want.has(id)){card.remove();cards.delete(id)}}
  const emptyEl=list.querySelector('.empty');
  if(!clips.length){
    if(emptyEl)emptyEl.remove();
    const box=el('div',{className:'empty'});
    box.append(el('div',{},isQueued?'No clips waiting on the upload shelf.':'Nothing to review here right now.'));
    if(reviewMode==='review'&&scrapedTotal>0){
      const sbtn=el('button',{className:'ghost',style:'margin-top:10px;margin-right:6px'},'Curate '+scrapedTotal+' scraped candidate'+(scrapedTotal===1?'':'s')+' →');
      sbtn.onclick=()=>setReviewMode('scraped');box.append(sbtn);
    }
    if(!isQueued&&queuedTotal>0){
      const jmp=el('button',{className:'ghost',style:'margin-top:10px'},'Inspect '+queuedTotal+' queued clip'+(queuedTotal===1?'':'s')+' →');
      jmp.onclick=()=>setReviewMode('queued');box.append(jmp);
    }
    list.append(box);return;
  }
  if(emptyEl)emptyEl.remove();
  let prev=null;for(const c of clips){let card=cards.get(c.id);if(!card){card=clipCard(c,isQueued);cards.set(c.id,card);prev?prev.after(card):list.prepend(card)}prev=card}
}
function renderReview(){
  if(!lastQ)return;const d=lastQ,channels=d.channels||[],queuedAll=d.queued||[],scrapedAll=d.scraped||[];
  if(channels.length&&filter!=='all'&&!channels.some(c=>c.slug===filter))filter='all';
  $('rv-stats').textContent=d.clips.length+' to review · '+d.approved_waiting+' queued · '+d.posted_today+' posted today · next '+(d.next_post?fmt(d.next_post):'not planned');
  const paused=(channels.find(c=>c.attention&&/paused|hold/i.test(c.attention)&&!c.owner_paused)||{}).attention;
  const banner=$('banner');if(paused){banner.hidden=false;banner.replaceChildren(el('div',{className:'banner'},paused+' — some channels are not posting.'))}else banner.hidden=true;
  renderModes(d);renderChips(channels);renderStrip(channels);
  const postedAll=(d.posted||[]).filter(matchesSearch);
  renderPosted(postedAll);
  const sourcePool=reviewMode==='queued'?queuedAll:(reviewMode==='scraped'?scrapedAll:d.clips);
  const byChan=(filter==='all'||reviewMode==='scraped')?sourcePool:sourcePool.filter(c=>(c.channel_slug||'default')===filter);
  renderFlaggedBar(byChan);
  let clips=byChan.filter(matchesSearch);
  if(sortBy==='score'&&reviewMode!=='posted')clips=[...clips].sort((a,b)=>(b.score??-1)-(a.score??-1));
  rvTools.hidden=!searchQuery&&sortBy==='newest'&&playbackSpeed===1&&(reviewMode==='posted'?(d.posted||[]).length<=2:byChan.length===0);
  rvSort.hidden=reviewMode==='posted';
  rvSpeed.hidden=reviewMode==='posted';
  const keepIds=new Set([...d.clips,...scrapedAll,...queuedAll].map(c=>c.id));
  for(const id of Object.keys(edits))if(!keepIds.has(id))delete edits[id];
  renderReviewList(clips,reviewMode==='queued',queuedAll.length,scrapedAll.length);
  setAlert('review',d.clips.length>0||scrapedAll.length>0);
}

/* ===================== CHANNELS ===================== */
function feedChip(slug,url){
  const f=el('div',{className:'feed'});f.append(el('span',{title:url},url.replace(/^https?:\/\/(www\.)?/,'')));
  const x=el('button',{title:'Remove'},'×');x.onclick=async()=>{x.disabled=true;try{await api.post('/api/channels/'+encodeURIComponent(slug)+'/feeds',{remove:[url]});loaders.channels()}catch(e){x.disabled=false;alert(e.message)}};
  f.append(x);return f;
}
function channelCard(c){
  const card=el('div',{className:'card'});const w=el('div',{className:'chan'});
  const top=el('div',{className:'top'});top.append(el('span',{className:'nm'},c.name||c.slug),el('span',{className:'sl'},c.slug));
  let state='off',cls='off';
  if(c.paused){state=c.owner_paused?'paused by you':'paused';cls='warn'}
  else if(c.enabled&&!c.next_step){state='running';cls='on'}
  else if(c.enabled){state='needs setup';cls='warn'}
  top.append(el('span',{className:'tag '+cls,style:'margin-left:auto'},state));
  w.append(top);
  w.append(el('div',{className:'sub'},[c.niche,c.uploads_per_day+'/day',c.timezone].filter(Boolean).join(' · ')));
  if(c.next_step)w.append(el('div',{className:'step'},'Next: '+c.next_step));
  if(c.default){
    w.append(el('div',{className:'sub muted'},'This is your original channel, managed in .env.'));
    if((c.feeds||[]).length){
      const feeds=el('div',{className:'feeds'});
      for(const u of c.feeds){const f=el('div',{className:'feed'});f.append(el('span',{title:u},u.replace(/^https?:\/\/(www\.)?/,'')));feeds.append(f)}
      w.append(feeds);
    }
  }

  if(c.enabled||c.paused)w.append(pauseBtn(c));

  if(!c.default){
    const feeds=el('div',{className:'feeds'});
    if(!c.feeds.length)feeds.append(el('span',{className:'sub muted'},'No creators yet.'));
    for(const u of c.feeds)feeds.append(feedChip(c.slug,u));
    w.append(feeds);
    const addRow=el('div',{className:'field'});const inp=el('input',{placeholder:'Add creator link(s), e.g. youtube.com/@mkbhd'});
    const add=el('button',{className:'ghost',style:'margin-top:8px'},'Add creator');
    add.onclick=async()=>{const urls=inp.value.split(/[\s,]+/).map(s=>s.trim()).filter(Boolean);if(!urls.length)return;add.disabled=true;try{await api.post('/api/channels/'+encodeURIComponent(c.slug)+'/feeds',{add:urls});inp.value='';loaders.channels()}catch(e){alert(e.message)}add.disabled=false};
    addRow.append(inp,add);w.append(addRow);

    const sx=el('details',{className:'sx'});sx.append(el('summary',{},'Settings'));
    const f=el('div',{style:'display:grid;gap:8px;margin-top:8px'});
    const name=el('input',{value:c.name||''}),niche=el('input',{value:c.niche||'',placeholder:'e.g. tech, comedy, podcasts'}),
          brand=el('input',{value:c.brand_name||'',placeholder:'@handle on the clips'}),
          up=el('input',{type:'number',value:c.uploads_per_day,min:'0',max:'50'}),tz=el('input',{value:c.timezone||''}),
          autoScore=el('input',{type:'number',value:String(c.auto_approve_score||0),min:'0',max:'1',step:'0.05'});
    const reqApp=el('input',{type:'checkbox',checked:c.require_approval!==false,style:'width:auto;margin:0'});
    const backstop=el('input',{type:'checkbox',checked:!!c.cadence_backstop,style:'width:auto;margin:0'});
    const mk=(lbl,node)=>{const d=el('div');d.append(el('label',{},lbl),node);return d};
    const mkChk=(node,txt)=>{const l=el('label',{style:'display:flex;align-items:center;gap:8px;cursor:pointer;color:var(--dim);font-size:13px;margin:2px 0'});l.append(node,el('span',{},txt));return l};
    const g=el('div',{className:'grid2'});g.append(mk('Posts a day',up),mk('Timezone',tz));
    f.append(
      mk('Name',name),mk('Niche',niche),mk('Handle on clips',brand),g,
      mk('Auto-approve AI score (0 = off, 0–1)',autoScore),
      mkChk(reqApp,'Require human approval before posting'),
      mkChk(backstop,'Cadence backstop (approve best clip if slot would miss)')
    );
    const save=el('button',{className:'btn'},'Save settings');
    save.onclick=async()=>{save.disabled=true;try{await api.post('/api/channels/'+encodeURIComponent(c.slug)+'/settings',
      {name:name.value,niche:niche.value,brand_name:brand.value,uploads_per_day:Number(up.value),timezone:tz.value,
       require_approval:reqApp.checked,auto_approve_score:Number(autoScore.value)||0,cadence_backstop:backstop.checked});toast('Saved');loaders.channels()}catch(e){alert(e.message)}save.disabled=false};
    f.append(save);
    const rb=el('div',{className:'rowbtns'});
    const toggle=el('button',{className:'ghost'},c.enabled?'Switch channel off':'Switch channel on');
    toggle.onclick=async()=>{toggle.disabled=true;try{await api.post('/api/channels/'+encodeURIComponent(c.slug)+'/'+(c.enabled?'disable':'enable'));loaders.channels()}catch(e){toggle.disabled=false;alert(e.message)}};
    rb.append(toggle);f.append(rb);
    const cloneRow=el('div',{className:'field'});
    const cloneSlug=el('input',{placeholder:'Clone as… (short name, e.g. '+c.slug+'2)','aria-label':'Clone short name'});
    const cloneBtn=el('button',{className:'ghost',style:'margin-top:8px'},'Clone');
    cloneBtn.onclick=async()=>{
      const ns=cloneSlug.value.trim();if(!ns){alert('Give the clone a short name.');return}
      cloneBtn.disabled=true;
      try{await api.post('/api/channels/'+encodeURIComponent(c.slug)+'/clone',{new_slug:ns,name:(c.name||c.slug)+' copy'});
        cloneSlug.value='';toast('Cloned (off) — connect it to its own YouTube channel');loaders.channels()}
      catch(e){alert(e.message)}
      cloneBtn.disabled=false;
    };
    cloneRow.append(el('label',{},'Clone cadence, niche, brand & creators into a new, switched-off channel'),cloneSlug,cloneBtn);
    f.append(cloneRow);
    sx.append(f);w.append(sx);
  }
  card.append(w);return card;
}
function buildAddForm(){
  const box=$('add-form');if(box.dataset.built)return;box.dataset.built='1';
  const slug=el('input',{placeholder:'short name, e.g. tech'}),name=el('input',{placeholder:'Tech & Science'}),
        niche=el('input',{placeholder:'e.g. tech, comedy'}),
        brand=el('input',{placeholder:'@handle shown on clips'}),up=el('input',{type:'number',value:'3',min:'0',max:'50'}),
        tz=el('input',{placeholder:'Asia/Kolkata'}),feeds=el('textarea',{placeholder:'One creator link per line'});
  const mk=(lbl,node)=>{const d=el('div',{className:'field'});d.append(el('label',{},lbl),node);return d};
  const err=el('div',{className:'err'});err.hidden=true;
  const btn=el('button',{className:'btn'},'Add channel');
  btn.onclick=async()=>{err.hidden=true;if(!slug.value.trim()){err.textContent='Give it a short name.';err.hidden=false;return}
    btn.disabled=true;try{await api.post('/api/channels',{slug:slug.value.trim(),name:name.value,niche:niche.value,brand:brand.value,
      uploads:Number(up.value)||3,timezone:tz.value,feeds:feeds.value.split('\n').map(s=>s.trim()).filter(Boolean)});
      slug.value=name.value=niche.value=brand.value=tz.value=feeds.value='';toast('Channel added — see its next step');loaders.channels()}
    catch(e){err.textContent=e.message;err.hidden=false}btn.disabled=false};
  const g=el('div',{className:'grid2'});g.append(mk('Posts a day',up),mk('Timezone',tz));
  box.append(mk('Short name',slug),mk('Display name',name),mk('Niche',niche),mk('Handle on clips',brand),g,mk('Creators to clip',feeds),btn,err);
}
function buildImportForm(){
  const box=$('import-form');if(!box||box.dataset.built)return;box.dataset.built='1';
  const ta=el('textarea',{rows:5,placeholder:'Paste a channels export or /api/backup JSON','aria-label':'Channels JSON'});
  const file=el('input',{type:'file',accept:'application/json,.json',style:'font-size:13px;padding:8px'});
  const out=el('div',{className:'ab'},'Channels import never copies sign-ins, and never switches a channel on.');
  const err=el('div',{className:'err'});err.hidden=true;
  const preview=el('button',{className:'ghost'},'Preview');
  const apply=el('button',{className:'btn'},'Apply import');apply.disabled=true;
  let previewed=null;
  const send=dry=>{
    let payload;try{payload=JSON.parse(ta.value)}catch(e){throw new Error('That is not valid JSON.')}
    return api.post('/api/channels/import',{payload,dry_run:dry});
  };
  const summary=r=>'creates '+((r.created||[]).join(', ')||'nothing')+' · updates '+((r.updated||[]).join(', ')||'nothing')+' · '+(r.feeds||0)+' creator link(s)';
  ta.addEventListener('input',()=>{previewed=null;apply.disabled=true});
  file.onchange=async()=>{const f=file.files&&file.files[0];if(f){ta.value=await f.text();previewed=null;apply.disabled=true}};
  preview.onclick=async()=>{
    err.hidden=true;preview.disabled=true;
    try{const r=await send(true);out.textContent='Preview: '+summary(r);previewed=ta.value;apply.disabled=false}
    catch(e){err.textContent=e.message;err.hidden=false;apply.disabled=true}
    preview.disabled=false;
  };
  apply.onclick=async()=>{
    if(previewed!==ta.value)return;
    err.hidden=true;apply.disabled=true;
    try{const r=await send(false);ta.value='';previewed=null;file.value='';out.textContent='Imported: '+summary(r);toast('Channels imported (all off until connected)');loaders.channels()}
    catch(e){err.textContent=e.message;err.hidden=false}
  };
  const row=el('div',{className:'rowbtns'});row.append(preview,apply);
  box.append(file,ta,out,row,err);
}
async function loadChannels(){
  buildAddForm();buildImportForm();
  try{const d=await api.get('/api/channels');const box=$('ch-list');box.replaceChildren();
    if(!d.channels.length){box.append(el('div',{className:'empty'},'No channels yet. Add one below.'));}
    let need=false;for(const c of d.channels){box.append(channelCard(c));if(c.next_step||c.paused)need=true}
    setAlert('channels',need);
  }catch(e){}
}

/* ===================== STATUS (doctor) ===================== */
function checkRow(c){
  const cls=c.ok?'ok':(c.required?'bad':'warn');const row=el('div',{className:'chk '+cls});
  row.append(el('div',{className:'d'}));const txt=el('div');txt.append(el('div',{className:'n'},c.name),el('div',{className:'t'},c.detail));row.append(txt);return row;
}
function renderDoctor(d,live){
  const head=$('dc-head');head.replaceChildren();
  const r=el('div',{className:'card',style:'padding:12px;display:grid;gap:8px'});
  r.append(el('div',{},[el('span',{className:'ready '+(d.ready?'y':'n')},d.ready?'Ready to run':'Not fully ready')]));
  const btn=el('button',{className:'ghost'},live?'Re-check with Google':'Run full check (asks Google)');
  btn.onclick=()=>loadDoctor(true);r.append(btn);head.append(r);
  const body=$('dc-body');body.replaceChildren();
  const fails=d.checks.filter(c=>!c.ok&&c.required),warns=d.checks.filter(c=>!c.ok&&!c.required),oks=d.checks.filter(c=>c.ok);
  const group=(title,arr)=>{if(!arr.length)return;const card=el('div',{className:'card',style:'padding:12px'});card.append(el('h2',{},title));for(const c of arr)card.append(checkRow(c));body.append(card)};
  group('Needs fixing',fails);group('Worth a look',warns);
  if(oks.length){const card=el('div',{className:'card',style:'padding:12px'});const dt=el('details');dt.append(el('summary',{style:'cursor:pointer;color:var(--dim)'},oks.length+' checks passing'));for(const c of oks)dt.append(checkRow(c));card.append(dt);body.append(card)}
  for(const ch of (d.channels||[])){const card=el('div',{className:'card',style:'padding:12px'});
    const h=el('div',{style:'display:flex;gap:8px;align-items:center'});h.append(el('span',{className:'nm',style:'font-weight:700'},ch.name||ch.slug),el('span',{className:'tag '+(ch.ready?'on':'warn'),style:'margin-left:auto'},ch.ready?'ready':(ch.enabled===false?'off':'needs setup')));
    card.append(h);for(const c of (ch.checks||[]).filter(c=>!c.ok))card.append(checkRow(c));body.append(card)}
  setAlert('status',!d.ready);
}
let doctorLoaded=false;
async function loadDoctor(live){
  const body=$('dc-body');if(live)body.replaceChildren(el('div',{className:'empty'},'Asking Google…'));
  else if(!doctorLoaded)body.replaceChildren(el('div',{className:'empty'},'Loading…'));
  try{const d=await api.get('/api/doctor'+(live?'?live=true':''));doctorLoaded=true;renderDoctor(d,live)}catch(e){body.replaceChildren(el('div',{className:'err'},'Could not load.'))}
}

/* ===================== REPORT ===================== */
let reportData=null,reportPick='';
function metric(v,label){const m=el('div',{className:'metric'});m.append(el('b',{},String(v)),el('span',{},label));return m}
function formatReportSummary(d,pickSlug){
  if(!d)return'';
  const chans=d.channels||null;
  const view=(pickSlug&&chans&&chans[pickSlug])?chans[pickSlug]:d;
  const scopeLabel=(pickSlug&&chans&&chans[pickSlug])?(chans[pickSlug].name||pickSlug):(chans?'All channels':'Channel');
  const headBits=['Day '+(view.day||0)];
  if(view.phase)headBits.push(view.phase);
  const lines=['Doomscroller Report — '+scopeLabel+' ('+headBits.join(' · ')+')'];
  lines.push('Published: '+(view.published||0)+' · Judged: '+(view.matured||0)+' · In stock: '+(view.stock||0));
  if(!pickSlug&&chans){
    for(const [slug,s] of Object.entries(chans)){
      lines.push('  • '+(s.name||slug)+': '+(s.published||0)+' published · '+(s.matured||0)+' judged · '+(s.stock||0)+' in stock');
    }
  }
  const posts=((view.plan||{}).posts)||[];
  if(posts.length)lines.push('Today: '+posts.map(p=>p.slot+' '+p.state).join(', '));
  const warns=view.warnings||[];
  if(warns.length){
    lines.push('Warnings:');
    for(const w of warns)lines.push('- '+w);
  }
  let winners=(view.best||[]).map(r=>({title:r.title,views:r.views||0,diagnosis:r.diagnosis||'',chan:''}));
  if(!winners.length&&!pickSlug&&chans){
    for(const [slug,s] of Object.entries(chans)){
      for(const r of (s.best||[]))winners.push({title:r.title,views:r.views||0,diagnosis:r.diagnosis||'',chan:s.name||slug});
    }
    winners.sort((a,b)=>b.views-a.views);
    winners=winners.slice(0,5);
  }
  if(winners.length){
    lines.push('Top winners:');
    for(const r of winners){
      const prefix=r.chan?'['+r.chan+'] ':'';
      const diag=r.diagnosis?' ('+r.diagnosis+')':'';
      lines.push('- '+prefix+(r.title||'Untitled')+' — '+Number(r.views||0).toLocaleString('en-US')+' views'+diag);
    }
  }
  const factors=view.factors||{};
  const fKeys=Object.keys(factors);
  if(fKeys.length){
    lines.push('What tests show:');
    for(const f of fKeys){
      const top=(factors[f]||[])[0];
      if(top)lines.push('- '+f+': '+top.arm+' ('+top.multiplier+'x views · '+Math.round((top.p_best||0)*100)+'% best · n='+top.n+')');
    }
  }
  const probs=d.problems||[];
  if(probs.length){
    lines.push('Recent problems (30d): '+probs.length);
    for(const p of probs.slice(0,3))lines.push('- '+(p.channel?'['+p.channel+'] ':'')+p.summary);
  }
  return lines.join('\n');
}
function renderReport(){
  const d=reportData;if(!d)return;const pick=$('rp-pick');pick.replaceChildren();
  let view=d;const chans=d.channels;
  const topRow=el('div',{style:'display:flex;gap:8px;align-items:center'});
  if(chans){const names=Object.entries(chans).map(([slug,s])=>[s.name||slug,slug]);
    const sel=el('select',{style:'flex:1'});sel.append(el('option',{value:''},'All channels'));for(const [nm,slug] of names)sel.append(el('option',{value:slug},nm));
    sel.value=reportPick;sel.onchange=()=>{reportPick=sel.value;renderReport()};topRow.append(sel);
    if(reportPick&&chans[reportPick])view=chans[reportPick];}
  const cpBtn=el('button',{className:'ghost',style:chans?'white-space:nowrap;padding:10px 12px;font-size:13px':'width:100%'},'Copy summary');
  cpBtn.onclick=()=>copyText(formatReportSummary(d,reportPick),'Copied report summary');
  topRow.append(cpBtn);pick.append(topRow);
  const body=$('rp-body');body.replaceChildren();
  const m=el('div',{className:'metrics'});m.append(metric(view.day||0,'Day'),metric(view.published||0,'Published'),metric(view.matured||0,'Judged'),metric(view.stock||0,'In stock'));body.append(m);
  for(const w of (view.warnings||[])){body.append(el('div',{className:'banner'},w))}
  if((view.plan||{}).posts&&view.plan.posts.length){const card=el('div',{className:'card',style:'padding:12px'});card.append(el('h2',{},'Today'));
    for(const p of view.plan.posts){const line=el('div',{className:'rowline'});line.append(el('span',{},p.slot),el('span',{className:'r'},p.state));card.append(line)}body.append(card)}
  if(view.factors&&Object.keys(view.factors).length){const card=el('div',{className:'card',style:'padding:12px'});card.append(el('h2',{},'What the tests show'));
    for(const [factor,rows] of Object.entries(view.factors)){card.append(el('div',{style:'font-weight:600;margin-top:6px'},factor));
      for(const r of rows){const line=el('div',{className:'rowline'});line.append(el('span',{},r.arm),el('span',{className:'r'},r.multiplier+'x views · '+Math.round(r.p_best*100)+'% best · n='+r.n));card.append(line)}}body.append(card)}
  let bestList=view.best||[];
  if(!bestList.length&&!reportPick&&chans){
    bestList=Object.entries(chans).flatMap(([slug,s])=>(s.best||[]).map(r=>({...r,title:'['+(s.name||slug)+'] '+(r.title||'—')}))).sort((a,b)=>(b.views||0)-(a.views||0)).slice(0,5);
  }
  for(const [label,arr] of [['Best videos',bestList],['Worst videos',view.worst||[]]]){if(!arr.length)continue;
    const card=el('div',{className:'card',style:'padding:12px'});card.append(el('h2',{},label));
    for(const r of arr){const line=el('div',{className:'rowline'});line.append(el('span',{style:'overflow:hidden;text-overflow:ellipsis;white-space:nowrap'},r.title||'—'),el('span',{className:'r'},(r.views||0).toLocaleString()+(r.diagnosis?(' · '+r.diagnosis):'')));card.append(line)}body.append(card)}
  if((d.problems||[]).length){const card=el('div',{className:'card',style:'padding:12px'});card.append(el('h2',{},'Problems (30 days)'));
    for(const p of d.problems.slice(0,20)){const line=el('div',{className:'rowline'});line.append(el('span',{},(p.channel?('['+p.channel+'] '):'')+p.summary),el('span',{className:'r'},ago(p.at)));card.append(line)}body.append(card);setAlert('report',true)}
  else setAlert('report',false);
  if((d.events||[]).length){const card=el('div',{className:'card',style:'padding:12px'});card.append(el('h2',{},'Recent activity'));
    for(const e of d.events.slice(0,25)){const line=el('div',{className:'rowline'});line.append(el('span',{},(e.channel?('['+e.channel+'] '):'')+e.summary),el('span',{className:'r'},ago(e.at)));card.append(line)}body.append(card)}
}
async function loadReport(){try{reportData=await api.get('/api/report');renderReport()}catch(e){$('rp-body').replaceChildren(el('div',{className:'err'},'Could not load.'))}}

/* ===================== CONTROLS ===================== */
async function runAction(action,btn){
  btn.disabled=true;try{await api.post('/api/controls/'+action);toast('Started…');setTimeout(loadControls,1500);setTimeout(loadControls,6000)}catch(e){alert(e.message)}btn.disabled=false;
}
function renderControls(d){
  const body=$('ct-body');body.replaceChildren();
  if(d.tunnel_url){
    const tc=el('div',{className:'act',style:'grid-column:1/-1'});
    tc.append(el('div',{className:'lb'},'Phone tunnel link'),el('div',{className:'ab',style:'word-break:break-all'},d.tunnel_url));
    const cp=el('button',{className:'ghost',style:'margin-top:4px'},'Copy phone link');
    cp.onclick=()=>copyText(d.tunnel_url,'Copied phone link');
    tc.append(cp);body.append(tc);
  }
  const shCard=el('div',{className:'act',style:'grid-column:1/-1'});
  const shTop=el('div',{className:'top'});
  shTop.append(el('span',{className:'lb'},'System health'));
  const shTag=el('span',{className:'tag',style:'margin-left:auto'},'checking…');
  shTop.append(shTag);
  const shChips=el('div',{className:'badges',style:'margin-top:4px'});
  const shWarn=el('div',{className:'ab'});
  const shFix=el('button',{className:'ghost',style:'margin-top:4px'},'Fix missing folders');shFix.hidden=true;
  const paintHealth=r=>{
    shChips.replaceChildren();
    const chip=(label,ok)=>shChips.append(el('span',{className:'tag '+(ok?'on':'warn')},(ok?'✓ ':'✗ ')+label));
    chip('Python '+((r.python||{}).version||'?'),(r.python||{}).ok);
    chip('ffmpeg',(r.media_tools||{}).ok);
    chip('Tunnel: '+((r.tunnel||{}).provider||'none'),(r.tunnel||{}).ok);
    chip('Folders',(r.directories||{}).ok);
    chip('Disk '+((r.disk||{}).free_gb!=null?(r.disk.free_gb+' GB'):''),(r.disk||{}).ok);
    chip('Database',(r.database||{}).ok);
    chip('Daemon ports',(r.ports||{}).daemon_running);
    chip('.env & token',(r.env||{}).env_file_exists&&(r.env||{}).review_token_set);
    shTag.textContent=r.ok?'healthy':'needs attention';shTag.className='tag '+(r.ok?'on':'warn');
    shWarn.textContent=(r.warnings||[]).join(' · ');shWarn.hidden=!(r.warnings||[]).length;
    shFix.hidden=!(!(r.directories||{}).ok||!(r.env||{}).env_file_exists||!(r.env||{}).review_token_set||!(r.database||{}).exists);
  };
  shFix.onclick=async()=>{
    shFix.disabled=true;
    try{const r=await api.post('/api/system/doctor/fix');paintHealth(r);toast((r.fixes_applied||[]).length?('Fixed: '+r.fixes_applied.join(', ')):'Nothing needed fixing')}
    catch(e){alert(e.message)}
    shFix.disabled=false;
  };
  shCard.append(shTop,shChips,shWarn,shFix);body.append(shCard);
  api.get('/api/system/doctor').then(paintHealth).catch(()=>{shTag.textContent='unavailable';shTag.className='tag warn'});
  if(d.scheduler){
    const sc=el('div',{className:'act',style:'grid-column:1/-1'});
    const top=el('div',{className:'top'});
    top.append(el('span',{className:'lb'},'Scheduler & overnight history'));
    top.append(el('span',{className:'tag '+(d.scheduler.running?'on':'warn'),style:'margin-left:auto'},d.scheduler.running?'running':'stopped'));
    sc.append(top);
    const overdue=d.scheduler.overdue||[];
    if(overdue.length)sc.append(el('div',{className:'ab',style:'color:var(--warn)'},'Overdue catch-up: '+overdue.join(', ')));
    const recent=d.scheduler.recent||[];
    if(recent.length){
      const dt=el('details',{style:'font-size:12px;color:var(--dim);margin-top:2px'});
      dt.append(el('summary',{style:'cursor:pointer'},'Recent job runs ('+recent.length+')'));
      for(const r of recent){
        const line=el('div',{className:'rowline'});
        line.append(el('span',{},(r.ok===false?'✗ ':'✓ ')+r.job_id),el('span',{className:'r'},(r.duration_sec||0)+'s · '+ago(r.timestamp)));
        dt.append(line);
      }
      sc.append(dt);
    }else sc.append(el('div',{className:'ab'},'No scheduled job runs recorded yet.'));
    body.append(sc);
  }
  body.append(el('h2',{},'Run something now'));
  for(const a of d.actions){const st=d.status[a.action]||{};const card=el('div',{className:'act'});
    const top=el('div',{className:'top'});top.append(el('span',{className:'lb'},a.label));
    const stx=el('span',{className:'st '+(st.state||'')});
    stx.textContent=st.state==='running'?'running…':(st.state==='done'?('done '+ago(st.at)):(st.state==='error'?'error':''));
    const about=(a.action==='cleanup'&&d.storage)?(a.about+' · '+d.storage.total_mb+' MB ('+d.storage.files+' files)'):a.about;
    top.append(stx);card.append(top);card.append(el('div',{className:'ab'},about));
    if(st.result)card.append(el('div',{className:'ab'},'Last: '+st.result));
    if(st.error)card.append(el('div',{className:'err'},st.error));
    const b=el('button',{className:'ghost',style:'margin-top:4px'},a.label);b.onclick=()=>runAction(a.action,b);if(st.state==='running')b.disabled=true;card.append(b);
    body.append(card)}
  const pvCard=el('div',{className:'act',style:'grid-column:1/-1'});
  const pvOut=el('div',{className:'ab'},'Check how many queued videos current filters would drop and how much disk cache is reclaimable before running.');
  const pvBtn=el('button',{className:'ghost',style:'margin-top:4px'},'Preview cleanup & filter sweep (dry run)');
  pvBtn.onclick=async()=>{
    pvBtn.disabled=true;
    try{
      const p=await api.get('/api/controls/preview');
      const f=p.filters||{},c=p.cleanup||{};
      pvOut.textContent='Dry run: '+(f.rejected||0)+' of '+(f.checked||0)+' pending video(s) would be filtered out · '+(c.mb_reclaimed||0)+' MB reclaimable ('+((c.sources_deleted||0)+(c.transforms_deleted||0)+(c.orphans_deleted||0)+(c.cache_deleted||0))+' expired/orphan files).';
    }catch(e){alert(e.message)}
    pvBtn.disabled=false;
  };
  pvCard.append(el('div',{className:'lb'},'Maintenance dry-run preview'),pvOut,pvBtn);
  body.append(pvCard);
  const logCard=el('details',{className:'act',style:'grid-column:1/-1'});
  const logSum=el('summary',{style:'cursor:pointer;font-weight:600'},'Daemon logs');
  const logBar=el('div',{style:'display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:8px'});
  let logLevel='all';
  const logAllBtn=el('button',{className:'ghost',style:'padding:6px 10px;font-size:12px'},'All');
  const logWarnBtn=el('button',{className:'ghost',style:'padding:6px 10px;font-size:12px'},'Warnings & Errors');
  const logRefBtn=el('button',{className:'ghost',style:'padding:6px 10px;font-size:12px;margin-left:auto'},'Refresh');
  const logPre=el('pre',{style:'max-height:260px;overflow:auto;background:#111;border:1px solid var(--line);border-radius:8px;padding:10px;font:11px/1.45 ui-monospace,monospace;white-space:pre-wrap;word-break:break-word;margin:6px 0 0;color:var(--dim)'},'Open to load recent daemon logs…');
  const syncLogBtns=()=>{
    logAllBtn.style.borderColor=logLevel==='all'?'var(--accent)':'var(--line)';
    logWarnBtn.style.borderColor=logLevel==='warn'?'var(--accent)':'var(--line)';
  };
  syncLogBtns();
  const fetchLogs=async()=>{
    logRefBtn.disabled=true;
    try{
      const r=await api.get('/api/logs?limit=80&level='+encodeURIComponent(logLevel));
      const lines=r.lines||[];
      logPre.textContent=lines.length?lines.join('\n'):(logLevel==='warn'?'No warnings or errors in recent logs.':'No daemon logs recorded yet.');
      logPre.scrollTop=logPre.scrollHeight;
    }catch(e){logPre.textContent='Could not load logs: '+(e.message||'error')}
    logRefBtn.disabled=false;
  };
  logAllBtn.onclick=()=>{logLevel='all';syncLogBtns();fetchLogs()};
  logWarnBtn.onclick=()=>{logLevel='warn';syncLogBtns();fetchLogs()};
  logRefBtn.onclick=fetchLogs;
  logCard.addEventListener('toggle',()=>{if(logCard.open)fetchLogs()});
  logBar.append(logAllBtn,logWarnBtn,logRefBtn);
  logCard.append(logSum,logBar,logPre);
  body.append(logCard);
  const ta=el('button',{className:'ghost'},'Send test alert');
  ta.onclick=async()=>{
    ta.disabled=true;
    try{
      const r=await api.post('/api/notify/test');
      if(!r.configured){
        toast('Alerts not configured — set NTFY_TOPIC or Telegram bot in .env');
      }else{
        const parts=Object.entries(r.channels||{}).map(([k,v])=>k+': '+(v&&v.ok?'sent':('failed'+(v&&v.error?' ('+v.error+')':''))));
        toast((r.ok?'Test alert sent · ':'Test alert failed · ')+parts.join(' · '));
      }
    }catch(e){alert(e.message)}
    ta.disabled=false;
  };
  const bk=el('button',{className:'ghost'},'Export channels & queue JSON');
  bk.onclick=()=>{const a=el('a',{href:'/api/backup',download:'doomscroller-backup.json'});document.body.append(a);a.click();a.remove();toast('Downloading backup JSON…')};
  const ra=el('button',{className:'ghost'},'Resume all paused channels');
  ra.onclick=async()=>{ra.disabled=true;try{await api.post('/api/resume-all');toast('Resumed');loadControls()}catch(e){alert(e.message)}ra.disabled=false};
  const lo=el('button',{className:'ghost'},'Lock this browser');
  lo.onclick=async()=>{lo.disabled=true;try{await api.post('/api/logout');location.href='/'}catch(e){lo.disabled=false;alert(e.message)}};
  body.append(ta,bk,ra,lo);
  if((d.blocks||[]).length){body.append(el('h2',{},'Takedown blocks'));
    for(const b of d.blocks){const card=el('div',{className:'act'});const who=(b.authors&&b.authors[0])||b.source||b.parent||'blocked';
      card.append(el('div',{className:'lb'},who));
      if(b.reason)card.append(el('div',{className:'ab'},b.reason));
      const u=el('button',{className:'danger',style:'margin-top:4px'},'Unblock');const key=who;
      u.onclick=async()=>{u.disabled=true;try{await api.post('/api/unblock',{name:key});toast('Unblocked');loadControls()}catch(e){u.disabled=false;alert(e.message)}};card.append(u);body.append(card)}
    setAlert('controls',true);
  }else setAlert('controls',false);
}
async function loadControls(){
  try{renderControls(await api.get('/api/controls'))}
  catch(e){$('ct-body').replaceChildren(el('div',{className:'err'},'Could not load.'))}
}

/* ===================== CLIP ===================== */
let clipChanSel=null,clipPollTimer=null;
async function syncClipChannels(){
  if(!clipChanSel)return;
  try{
    const d=await api.get('/api/channels');const chans=d.channels||[];
    const wrap=clipChanSel.parentElement;
    if(chans.length<=1&&(!chans[0]||chans[0].default)){wrap.hidden=true;clipChanSel.value='';return}
    const prev=clipChanSel.value;clipChanSel.replaceChildren(el('option',{value:''},'Default channel'));
    for(const c of chans)clipChanSel.append(el('option',{value:c.slug},(c.name||c.slug)+(c.enabled?'':' (off)')));
    if([...clipChanSel.options].some(o=>o.value===prev))clipChanSel.value=prev;
    wrap.hidden=false;
  }catch(e){}
}
function buildClipForm(){
  const box=$('clip-form');if(box.dataset.built)return;box.dataset.built='1';
  let droppedFile=null;
  const defaultHint='Drag & drop a video file here, or choose from this device';
  const src=el('input',{placeholder:'Video link (YouTube, etc.) or server path'});
  const fileInp=el('input',{type:'file',accept:'video/mp4,video/quicktime,video/x-matroska,video/webm,.mp4,.mov,.mkv,.webm,.m4v',style:'font-size:13px;padding:8px'});
  const dropHint=el('div',{style:'font-size:12px;color:var(--dim);margin-bottom:6px'},defaultHint);
  const dropZone=el('div',{className:'dropzone'});
  dropZone.append(dropHint,fileInp);
  fileInp.onchange=()=>{
    droppedFile=null;
    const f=fileInp.files&&fileInp.files[0];
    dropHint.textContent=f?('Selected: '+f.name):defaultHint;
  };
  const clipPane=$('tab-clip');
  let dragDepth=0;
  const hasFiles=e=>!!(e.dataTransfer&&[...(e.dataTransfer.types||[])].includes('Files'));
  clipPane.addEventListener('dragenter',e=>{if(hasFiles(e)){e.preventDefault();dragDepth++;dropZone.classList.add('dragover')}});
  clipPane.addEventListener('dragover',e=>{if(hasFiles(e)){e.preventDefault();dropZone.classList.add('dragover')}});
  clipPane.addEventListener('dragleave',()=>{dragDepth=Math.max(0,dragDepth-1);if(!dragDepth)dropZone.classList.remove('dragover')});
  clipPane.addEventListener('drop',e=>{
    dragDepth=0;dropZone.classList.remove('dragover');
    const files=e.dataTransfer&&e.dataTransfer.files;
    if(!files||!files.length)return;
    e.preventDefault();
    droppedFile=files[0];
    try{const dt=new DataTransfer();dt.items.add(droppedFile);fileInp.files=dt.files}catch(err){}
    dropHint.textContent='Selected: '+droppedFile.name;
  });
  const clrBtn=$('clip-clear-finished');
  if(clrBtn)clrBtn.onclick=async()=>{
    clrBtn.disabled=true;
    try{
      const r=await api.post('/api/clip/clear-finished');
      toast('Cleared '+(r.cleared||0)+' finished job'+((r.cleared||0)===1?'':'s'));
      refreshClipJobs();
    }catch(e){alert(e.message)}
    clrBtn.disabled=false;
  };
  const cnt=el('input',{type:'number',value:'3',min:'1',max:'10'});
  clipChanSel=el('select');
  const skipChk=el('input',{type:'checkbox',style:'width:auto;margin:0'});
  const chkWrap=el('label',{style:'display:flex;align-items:center;gap:8px;cursor:pointer;color:var(--dim);font-size:13px;margin:2px 0'});
  chkWrap.append(skipChk,el('span',{},'Skip review & queue straight to publish'));
  const err=el('div',{className:'err'});err.hidden=true;
  const btn=el('button',{className:'btn'},'Cut into Shorts');
  const uploadVideoFile=file=>new Promise((resolve,reject)=>{
    const qs=new URLSearchParams({filename:file.name,count:String(Number(cnt.value)||3),channel_slug:clipChanSel.value||''});
    if(skipChk.checked)qs.set('auto_queue','true');
    const xhr=new XMLHttpRequest();
    xhr.open('POST','/api/clip/upload?'+qs.toString());
    xhr.withCredentials=true;
    xhr.upload.onprogress=e=>{if(e.lengthComputable)btn.textContent='Uploading '+Math.round((e.loaded/e.total)*100)+'%…'};
    xhr.onload=()=>{
      let body={};try{body=JSON.parse(xhr.responseText||'{}')}catch(e){}
      if(xhr.status>=200&&xhr.status<300)resolve(body);
      else reject(new Error(body.detail||('Upload failed ('+xhr.status+')')));
    };
    xhr.onerror=()=>reject(new Error('Network error during upload'));
    xhr.send(file);
  });
  const submit=async()=>{
    err.hidden=true;const v=src.value.trim(),picked=(fileInp.files&&fileInp.files[0])||droppedFile;
    if(!v&&!picked){err.textContent='Paste a video link or choose a video file to upload.';err.hidden=false;return}
    btn.disabled=true;
    try{
      if(picked&&!v){
        await uploadVideoFile(picked);
        fileInp.value='';droppedFile=null;dropHint.textContent=defaultHint;toast('Uploaded & queued for clipping');
      }else{
        const payload={source:v,count:Number(cnt.value)||3,channel_slug:clipChanSel.value||''};
        if(skipChk.checked)payload.auto_queue=true;
        await api.post('/api/clip',payload);src.value='';toast('Queued for clipping');
      }
      refreshClipJobs();
    }catch(e){err.textContent=e.message;err.hidden=false}
    btn.textContent='Cut into Shorts';btn.disabled=false;
  };
  btn.onclick=submit;
  src.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();submit()}});
  const mk=(lbl,node)=>{const d=el('div');d.append(el('label',{},lbl),node);return d};
  const chWrap=mk('Target channel',clipChanSel);chWrap.hidden=true;
  box.append(mk('Video link or server path',src),mk('Or upload video from this device (.mp4, .mov, .mkv, .webm)',dropZone),chWrap,mk('Max Shorts to cut',cnt),chkWrap,btn,err);
}
async function refreshClipJobs(){
  clearTimeout(clipPollTimer);
  const box=$('clip-jobs'),clrBtn=$('clip-clear-finished');
  const FINISHED=new Set(['done','succeeded','failed','cancelled','canceled']);
  try{
    const d=await api.get('/api/clip/jobs');box.replaceChildren();
    const jobs=d.jobs||[];
    if(clrBtn)clrBtn.hidden=!jobs.some(j=>FINISHED.has(String(j.status||'').toLowerCase()));
    setAlert('clip',jobs.length>0&&jobs[0].status==='failed');
    if(!jobs.length){box.append(el('div',{className:'empty'},'No manual clip jobs yet.'));return}
    let activeJob=false;
    for(const j of jobs){
      if(j.status==='queued'||j.status==='running')activeJob=true;
      const card=el('div',{className:'act'});
      const top=el('div',{className:'top'});
      top.append(el('span',{className:'lb',style:'overflow:hidden;text-overflow:ellipsis;white-space:nowrap'},j.source));
      const cls=j.status==='done'?'done':(j.status==='failed'?'err':(j.status==='running'?'run':''));
      top.append(el('span',{className:'st '+cls},j.status+(j.stage?(' · '+j.stage):'')));
      card.append(top);
      const clips=(j.result&&j.result.clips)||[],warns=(j.result&&j.result.warnings)||[];
      const made=clips.length;
      const metaBits=[ago(j.created_at),j.channel_slug&&('['+j.channel_slug+']'),made&&(made+' clip'+(made===1?'':'s')+' made'),j.auto_queue?'auto-queued':'for review'].filter(Boolean);
      card.append(el('div',{className:'ab'},metaBits.join(' · ')));
      if(j.error)card.append(el('div',{className:'err'},j.error));
      if(made){
        const dt=el('details',{style:'font-size:12px;color:var(--dim)'});
        dt.append(el('summary',{style:'cursor:pointer'},'Show cut clips ('+made+')'));
        for(const c of clips)dt.append(el('div',{style:'padding:3px 0'},(c.title||'Clip')+' ('+Math.round(c.duration||0)+'s)'));
        card.append(dt);
      }
      if(warns.length){
        const wt=el('details',{style:'font-size:12px;color:var(--warn)'});
        wt.append(el('summary',{style:'cursor:pointer'},warns.length+' note'+(warns.length===1?'':'s')));
        for(const w of warns)wt.append(el('div',{style:'padding:2px 0'},w));
        card.append(wt);
      }
      const btns=el('div',{className:'rowbtns',style:'margin-top:4px'});
      if(j.status==='done'&&made&&!j.auto_queue){
        const rv=el('button',{className:'ghost'},'Review clips →');
        rv.onclick=()=>show('review');btns.append(rv);
      }
      if(j.status==='failed'){
        const rt=el('button',{className:'ghost'},'Retry');
        rt.onclick=async()=>{rt.disabled=true;try{await api.post('/api/clip/jobs/'+encodeURIComponent(j.id)+'/retry');toast('Re-queued');refreshClipJobs()}catch(e){rt.disabled=false;alert(e.message)}};
        btns.append(rt);
      }
      if(j.status!=='running'){
        const dm=el('button',{className:j.status==='failed'?'danger':'ghost'},j.status==='queued'?'Cancel':'Dismiss');
        dm.onclick=async()=>{dm.disabled=true;try{await api.post('/api/clip/jobs/'+encodeURIComponent(j.id)+'/dismiss');refreshClipJobs()}catch(e){dm.disabled=false;alert(e.message)}};
        btns.append(dm);
      }
      if(btns.childNodes.length)card.append(btns);
      box.append(card);
    }
    if(activeJob&&active==='clip')clipPollTimer=setTimeout(()=>{if(active==='clip')refreshClipJobs()},3500);
  }catch(e){box.replaceChildren(el('div',{className:'err'},'Could not load clip jobs.'))}
}
async function loadClip(){
  buildClipForm();
  syncClipChannels();
  await refreshClipJobs();
}

/* ---------- alerts on the tab bar ---------- */
function setAlert(tab,on){const b=$('nav-'+tab);if(b)b.classList.toggle('alert',!!on)}

/* ---------- keyboard shortcuts ---------- */
const kbdModal=$('kbd-modal');
$('kbd-help').onclick=()=>{kbdModal.hidden=!kbdModal.hidden};
$('kbd-close').onclick=()=>{kbdModal.hidden=true};
kbdModal.onclick=e=>{if(e.target===kbdModal)kbdModal.hidden=true};
function visibleCards(){return[...$('list').querySelectorAll('.card')]}
function focusCard(idx){
  const arr=visibleCards();if(!arr.length){kbIdx=-1;return null}
  kbIdx=Math.max(0,Math.min(arr.length-1,idx));
  arr.forEach((c,i)=>c.classList.toggle('kb-focus',i===kbIdx));
  arr[kbIdx].scrollIntoView({behavior:'smooth',block:'nearest'});
  return arr[kbIdx];
}
document.addEventListener('keydown',e=>{
  if(e.metaKey||e.ctrlKey||e.altKey)return;
  if(e.key==='Escape'&&!kbdModal.hidden){e.preventDefault();kbdModal.hidden=true;return}
  if(e.key==='Escape'&&active==='review'&&document.activeElement===rvSearch){
    e.preventDefault();rvSearch.value='';searchQuery='';kbIdx=-1;rvSearch.blur();renderReview();return;
  }
  const tag=(document.activeElement&&document.activeElement.tagName)||'';
  if(tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT')return;
  if(e.key==='?'){e.preventDefault();kbdModal.hidden=!kbdModal.hidden;return}
  if(e.key>='1'&&e.key<='6'){const t=TABS[Number(e.key)-1];if(t){e.preventDefault();show(t[0])}return}
  if(active!=='review')return;
  if(e.key==='Escape'&&(rvSearch.value||searchQuery)){e.preventDefault();rvSearch.value='';searchQuery='';kbIdx=-1;renderReview();return}
  if(e.key==='/'){e.preventDefault();rvTools.hidden=false;rvSearch.focus();rvSearch.select();return}
  const k=e.key.toLowerCase();
  if(k==='u'&&lastUndoId){e.preventDefault();performUndo(lastUndoId);return}
  if(reviewMode==='posted')return;
  if(k==='s'){e.preventDefault();cycleSpeed();return}
  const arr=visibleCards();if(!arr.length)return;
  if(k==='j'){e.preventDefault();focusCard(kbIdx<0?0:kbIdx+1);return}
  if(k==='k'){e.preventDefault();focusCard(kbIdx<=0?0:kbIdx-1);return}
  const cur=arr[kbIdx>=0&&kbIdx<arr.length?kbIdx:0];
  if(!cur)return;
  if(kbIdx<0)focusCard(0);
  if(e.key===' '&&cur._video){e.preventDefault();cur._video.paused?cur._video.play():cur._video.pause();return}
  if(k==='a'&&cur._approve){e.preventDefault();cur._approve();return}
  if((k==='x'||k==='r')&&cur._reject){e.preventDefault();cur._reject();return}
  if(k==='e'&&cur._editTitle){e.preventDefault();cur._editTitle()}
});

/* ---------- wiring ---------- */
const loaders={
  review:async()=>{try{lastQ=await api.get('/api/queue');renderReview()}catch(e){if(e instanceof Response&&e.status===401)$('rv-stats').textContent='Session expired — reopen your link.'}},
  clip:loadClip,channels:loadChannels,status:()=>loadDoctor(false),report:loadReport,controls:loadControls,
};
show('review');
// Refresh the tab you are looking at. Review and the quick tabs poll; never while you are typing or a clip plays.
setInterval(()=>{const a=document.activeElement;const typing=a&&(a.tagName==='INPUT'||a.tagName==='TEXTAREA'||a.tagName==='SELECT');
  if(typing)return;if(active==='review'&&reviewBusy())return;if(active==='status')return;loaders[active]&&loaders[active]()},60000);
// Keep the review badge current even on another tab, cheaply.
setInterval(async()=>{if(active==='review')return;try{const q=await api.get('/api/queue');setAlert('review',q.clips.length>0||(q.scraped||[]).length>0)}catch(e){}},90000);
</script></body></html>"""
