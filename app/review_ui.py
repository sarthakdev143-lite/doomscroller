"""The control panel's HTML, CSS and JS, served by app/review.py.

One self-contained page, no build step and no external libraries, so it loads fast on a phone over a
tunnel. Five tabs along the bottom: Review, Channels, Status, Report, Controls. Each tab fetches its own
data from the /api routes and the whole thing is one dark, touch-first layout.
"""
from __future__ import annotations

LOCKED = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Doomscroller</title><body style="font:16px system-ui;background:#111;color:#eee;padding:24px">
<p>Open this page with the link that has <code>?key=</code> in it.</p></body>"""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
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
</style></head><body>
<header><div class="hwrap"><h1 id="title">Review</h1><span id="conn"></span></div></header>

<main>
  <section id="tab-review" class="tabpane">
    <div id="rv-stats" class="sub">Loading…</div>
    <div id="banner" hidden></div>
    <div id="strip" hidden></div>
    <div class="chips" id="chips"></div>
    <div id="list" style="display:grid;gap:12px"></div>
    <div id="posted" style="display:grid;gap:8px"></div>
  </section>

  <section id="tab-channels" class="tabpane" hidden>
    <div id="ch-list" style="display:grid;gap:12px"></div>
    <details class="card" style="padding:12px"><summary style="cursor:pointer;font-weight:600">+ Add a channel</summary>
      <div id="add-form" style="display:grid;gap:8px;margin-top:10px"></div>
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
<div id="snack" class="snack" hidden></div>

<script>
const $=id=>document.getElementById(id);
function el(t,a,x){const e=document.createElement(t);Object.assign(e,a||{});if(x!=null)e.textContent=x;return e}
function fmt(iso){return iso?new Date(iso).toLocaleString([],{weekday:'short',hour:'2-digit',minute:'2-digit'}):'—'}
function ago(iso){if(!iso)return'';const s=(Date.now()-new Date(iso))/1000;if(s<60)return'just now';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago'}
let connOff=false;
function setConn(off){connOff=off;$('conn').textContent=off?'offline':'';$('conn').className=off?'off':''}
const api={
  get:u=>fetch(u,{credentials:'same-origin'}).then(r=>{if(!r.ok)throw r;setConn(false);return r.json()}).catch(e=>{if(e instanceof Response)throw e;setConn(true);throw e}),
  post:(u,b)=>fetch(u,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})})
    .then(async r=>{if(!r.ok){let m='Failed ('+r.status+')';try{m=(await r.json()).detail||m}catch(e){}if(r.status===401)m='Session expired — reopen your link.';throw new Error(m)}setConn(false);return r.json()})
};
const snack=$('snack');let snackTimer=null;
function toast(label,undoId){
  clearTimeout(snackTimer);snack.replaceChildren(el('span',{},label));
  if(undoId){const u=el('button',{},'Undo');u.onclick=async()=>{clearTimeout(snackTimer);snack.hidden=true;
    try{await api.post('/api/clips/'+encodeURIComponent(undoId)+'/undo')}catch(e){alert(e.message)}loaders.review()};snack.append(u)}
  snack.hidden=false;snackTimer=setTimeout(()=>snack.hidden=true,6000);
}

/* ---------- tabs ---------- */
const TABS=[['review','Review','▦'],['channels','Channels','≡'],['status','Status','◌'],['report','Report','▤'],['controls','Controls','⚙']];
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
const edits={},cards=new Map();let lastQ=null,filter='all',armedResume=null;
try{filter=localStorage.getItem('reviewFilter')||'all'}catch(e){}
function reviewBusy(){const a=document.activeElement;return(a&&a.tagName==='INPUT'&&$('list').contains(a))||[...$('list').querySelectorAll('video')].some(v=>!v.paused&&!v.ended)}
function setFilter(f){filter=f;try{localStorage.setItem('reviewFilter',f)}catch(e){}renderReview()}
function renderChips(channels){
  $('chips').replaceChildren();if(!channels.length)return;
  const mk=(slug,label,n,att)=>{const c=el('div',{className:'chip'+(filter===slug?' on':'')+(att?' att':'')});c.append(el('span',{},label));if(n)c.append(el('span',{className:'n'},n));c.onclick=()=>setFilter(slug);return c};
  $('chips').append(mk('all','All',channels.reduce((n,c)=>n+c.waiting,0),false));
  for(const c of channels)$('chips').append(mk(c.slug,c.name,c.waiting,!!c.attention));
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
function renderPosted(items){
  const box=$('posted');box.replaceChildren(el('h2',{},'Recently posted'));
  if(!items.length){box.append(el('div',{className:'empty'},'Nothing posted yet.'));return}
  for(const p of items){const card=el('div',{className:'post'});
    card.append(el('a',{href:p.url,target:'_blank',rel:'noopener'},p.title||'Untitled'));
    const nums=el('div',{className:'nums'});nums.append(el('span',{},p.views.toLocaleString()+' views'),el('span',{},p.likes.toLocaleString()+' likes'));
    if(p.watched!==null)nums.append(el('span',{},p.watched+'% watched'));nums.append(el('span',{},new Date(p.posted).toLocaleDateString()));
    card.append(nums);
    if(p.verdict){card.append(el('div',{className:'verdict '+p.verdict},p.verdict.replace('_',' ')));card.append(el('div',{className:'why'},p.why))}
    box.append(card)}
}
function clipCard(c){
  const card=el('div',{className:'card'+(c.flag?' flagged':'')});
  card.append(el('video',{src:'/media/'+encodeURIComponent(c.id),controls:true,playsInline:true,preload:'metadata'}));
  const b=el('div',{className:'body'});
  if(c.flag)b.append(el('div',{className:'warnbox'},'Flagged — probably should NOT post: '+c.flag));
  if(c.channel)b.append(el('span',{className:'pill'},c.channel));
  const t=el('input',{value:c.id in edits?edits[c.id]:c.title,'aria-label':'Clip title'});
  t.addEventListener('input',()=>{edits[c.id]=t.value});b.append(t);
  b.append(el('div',{className:'meta'},[c.creator&&('Credit: '+c.creator),Math.round(c.seconds)+'s',c.kind].filter(Boolean).join(' · ')));
  if(c.hook)b.append(el('div',{className:'meta'},'Hook: '+c.hook));
  const errLine=el('div',{className:'err'});errLine.hidden=true;
  const actions=el('div',{className:'actions'}),yes=el('button',{className:'yes'},'Approve'),no=el('button',{className:'nope'},'Reject');
  let armed=false;
  async function decide(path,body){
    yes.disabled=no.disabled=true;errLine.hidden=true;
    try{await api.post('/api/clips/'+encodeURIComponent(c.id)+'/'+path,body||{});
      delete edits[c.id];cards.delete(c.id);card.remove();toast(path==='approve'?'Approved':'Rejected',c.id);loaders.review();return}
    catch(e){errLine.textContent=e.message;errLine.hidden=false}
    card.style.transform='';card.style.opacity='';yes.disabled=no.disabled=false;
  }
  yes.onclick=()=>{if(c.flag&&!armed){armed=true;yes.textContent='Tap again to approve anyway';setTimeout(()=>{armed=false;yes.textContent='Approve'},3000);return}decide('approve',{title:t.value})};
  no.onclick=()=>decide('reject');
  actions.append(yes,no);b.append(actions,errLine);card.append(b);
  let tracking=false,horiz=false,startX=0,startY=0;card.style.transition='transform .2s ease, opacity .2s ease';
  const onCard=t=>!t.closest('video,input,button');
  card.addEventListener('touchstart',e=>{if(e.touches.length!==1||!onCard(e.target)){tracking=false;return}startX=e.touches[0].clientX;startY=e.touches[0].clientY;tracking=true;horiz=false},{passive:true});
  card.addEventListener('touchmove',e=>{if(!tracking)return;const dx=e.touches[0].clientX-startX,dy=e.touches[0].clientY-startY;
    if(!horiz){if(Math.abs(dx)<12&&Math.abs(dy)<12)return;if(Math.abs(dy)>=Math.abs(dx)){tracking=false;return}horiz=true}
    e.preventDefault();card.style.transition='none';card.style.transform='translateX('+dx+'px)';card.style.opacity=String(Math.max(.35,1-Math.abs(dx)/350))},{passive:false});
  card.addEventListener('touchend',e=>{if(!tracking||!horiz){tracking=false;return}tracking=false;
    const dx=e.changedTouches[0].clientX-startX;card.style.transition='transform .2s ease, opacity .2s ease';const past=Math.abs(dx)>90,right=dx>0;
    if(past&&right&&!c.flag){card.style.transform='translateX(120%)';card.style.opacity='0';decide('approve',{title:t.value});return}
    if(past&&!right){card.style.transform='translateX(-120%)';card.style.opacity='0';decide('reject');return}
    card.style.transform='';card.style.opacity=''});
  return card;
}
function renderReviewList(clips){
  const list=$('list'),want=new Map(clips.map(c=>[c.id,c]));
  for(const [id,card] of cards){if(!want.has(id)){card.remove();cards.delete(id)}}
  const emptyEl=list.querySelector('.empty');
  if(!clips.length){if(!emptyEl)list.append(el('div',{className:'empty'},'Nothing to review here right now.'));return}
  if(emptyEl)emptyEl.remove();
  let prev=null;for(const c of clips){let card=cards.get(c.id);if(!card){card=clipCard(c);cards.set(c.id,card);prev?prev.after(card):list.prepend(card)}prev=card}
}
function renderReview(){
  if(!lastQ)return;const d=lastQ,channels=d.channels||[];
  if(channels.length&&filter!=='all'&&!channels.some(c=>c.slug===filter))filter='all';
  $('rv-stats').textContent=d.clips.length+' to review · '+d.approved_waiting+' queued · '+d.posted_today+' posted today · next '+(d.next_post?fmt(d.next_post):'not planned');
  const paused=(channels.find(c=>c.attention&&/paused|hold/i.test(c.attention)&&!c.owner_paused)||{}).attention;
  const banner=$('banner');if(paused){banner.hidden=false;banner.replaceChildren(el('div',{className:'banner'},paused+' — some channels are not posting.'))}else banner.hidden=true;
  renderChips(channels);renderStrip(channels);renderPosted(d.posted||[]);
  const clips=(filter==='all')?d.clips:d.clips.filter(c=>c.channel_slug===filter);
  for(const id of Object.keys(edits))if(!d.clips.some(c=>c.id===id))delete edits[id];
  renderReviewList(clips);
  setAlert('review',d.clips.length>0);
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
  if(c.default)w.append(el('div',{className:'sub muted'},'This is your original channel, managed in .env.'));

  if(c.enabled||c.paused)w.append(pauseBtn(c));

  if(!c.default){
    const feeds=el('div',{className:'feeds'});
    if(!c.feeds.length)feeds.append(el('span',{className:'sub muted'},'No creators yet.'));
    for(const u of c.feeds)feeds.append(feedChip(c.slug,u));
    w.append(feeds);
    const addRow=el('div',{className:'field'});const inp=el('input',{placeholder:'Add a creator link, e.g. youtube.com/@mkbhd'});
    const add=el('button',{className:'ghost',style:'margin-top:8px'},'Add creator');
    add.onclick=async()=>{const v=inp.value.trim();if(!v)return;add.disabled=true;try{await api.post('/api/channels/'+encodeURIComponent(c.slug)+'/feeds',{add:[v]});inp.value='';loaders.channels()}catch(e){alert(e.message)}add.disabled=false};
    addRow.append(inp,add);w.append(addRow);

    const sx=el('details',{className:'sx'});sx.append(el('summary',{},'Settings'));
    const f=el('div',{style:'display:grid;gap:8px;margin-top:8px'});
    const name=el('input',{value:c.name||''}),brand=el('input',{value:c.brand_name||'',placeholder:'@handle on the clips'}),
          up=el('input',{type:'number',value:c.uploads_per_day,min:'0',max:'50'}),tz=el('input',{value:c.timezone||''});
    const mk=(lbl,node)=>{const d=el('div');d.append(el('label',{},lbl),node);return d};
    const g=el('div',{className:'grid2'});g.append(mk('Posts a day',up),mk('Timezone',tz));
    f.append(mk('Name',name),mk('Handle on clips',brand),g);
    const save=el('button',{className:'btn'},'Save settings');
    save.onclick=async()=>{save.disabled=true;try{await api.post('/api/channels/'+encodeURIComponent(c.slug)+'/settings',
      {name:name.value,brand_name:brand.value,uploads_per_day:Number(up.value),timezone:tz.value});toast('Saved');loaders.channels()}catch(e){alert(e.message)}save.disabled=false};
    f.append(save);
    const rb=el('div',{className:'rowbtns'});
    const toggle=el('button',{className:'ghost'},c.enabled?'Switch channel off':'Switch channel on');
    toggle.onclick=async()=>{toggle.disabled=true;try{await api.post('/api/channels/'+encodeURIComponent(c.slug)+'/'+(c.enabled?'disable':'enable'));loaders.channels()}catch(e){toggle.disabled=false;alert(e.message)}};
    rb.append(toggle);f.append(rb);
    sx.append(f);w.append(sx);
  }
  card.append(w);return card;
}
function buildAddForm(){
  const box=$('add-form');if(box.dataset.built)return;box.dataset.built='1';
  const slug=el('input',{placeholder:'short name, e.g. tech'}),name=el('input',{placeholder:'Tech & Science'}),
        brand=el('input',{placeholder:'@handle shown on clips'}),up=el('input',{type:'number',value:'3',min:'0',max:'50'}),
        tz=el('input',{placeholder:'Asia/Kolkata'}),feeds=el('textarea',{placeholder:'One creator link per line'});
  const mk=(lbl,node)=>{const d=el('div',{className:'field'});d.append(el('label',{},lbl),node);return d};
  const err=el('div',{className:'err'});err.hidden=true;
  const btn=el('button',{className:'btn'},'Add channel');
  btn.onclick=async()=>{err.hidden=true;if(!slug.value.trim()){err.textContent='Give it a short name.';err.hidden=false;return}
    btn.disabled=true;try{await api.post('/api/channels',{slug:slug.value.trim(),name:name.value,brand:brand.value,
      uploads:Number(up.value)||3,timezone:tz.value,feeds:feeds.value.split('\n').map(s=>s.trim()).filter(Boolean)});
      slug.value=name.value=brand.value=tz.value=feeds.value='';toast('Channel added — see its next step');loaders.channels()}
    catch(e){err.textContent=e.message;err.hidden=false}btn.disabled=false};
  const g=el('div',{className:'grid2'});g.append(mk('Posts a day',up),mk('Timezone',tz));
  box.append(mk('Short name',slug),mk('Display name',name),mk('Handle on clips',brand),g,mk('Creators to clip',feeds),btn,err);
}
async function loadChannels(){
  buildAddForm();
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
    card.append(el('div',{className:'top',style:'display:flex;gap:8px;align-items:center'},),);
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
function renderReport(){
  const d=reportData;if(!d)return;const pick=$('rp-pick');pick.replaceChildren();
  let view=d;const chans=d.channels;
  if(chans){const names=Object.entries(chans).map(([slug,s])=>[s.name||slug,slug]);
    const sel=el('select');sel.append(el('option',{value:''},'All channels'));for(const [nm,slug] of names)sel.append(el('option',{value:slug},nm));
    sel.value=reportPick;sel.onchange=()=>{reportPick=sel.value;renderReport()};pick.append(sel);
    if(reportPick&&chans[reportPick])view=chans[reportPick];}
  const body=$('rp-body');body.replaceChildren();
  const m=el('div',{className:'metrics'});m.append(metric(view.day||0,'Day'),metric(view.published||0,'Published'),metric(view.matured||0,'Judged'),metric(view.stock||0,'In stock'));body.append(m);
  for(const w of (view.warnings||[])){body.append(el('div',{className:'banner'},w))}
  if((view.plan||{}).posts&&view.plan.posts.length){const card=el('div',{className:'card',style:'padding:12px'});card.append(el('h2',{},'Today'));
    for(const p of view.plan.posts){const line=el('div',{className:'rowline'});line.append(el('span',{},p.slot),el('span',{className:'r'},p.state));card.append(line)}body.append(card)}
  if(view.factors&&Object.keys(view.factors).length){const card=el('div',{className:'card',style:'padding:12px'});card.append(el('h2',{},'What the tests show'));
    for(const [factor,rows] of Object.entries(view.factors)){card.append(el('div',{style:'font-weight:600;margin-top:6px'},factor));
      for(const r of rows){const line=el('div',{className:'rowline'});line.append(el('span',{},r.arm),el('span',{className:'r'},r.multiplier+'x views · '+Math.round(r.p_best*100)+'% best · n='+r.n));card.append(line)}}body.append(card)}
  for(const [label,key] of [['Best videos','best'],['Worst videos','worst']]){const arr=view[key]||[];if(!arr.length)continue;
    const card=el('div',{className:'card',style:'padding:12px'});card.append(el('h2',{},label));
    for(const r of arr){const line=el('div',{className:'rowline'});line.append(el('span',{style:'overflow:hidden;text-overflow:ellipsis;white-space:nowrap'},r.title||'—'),el('span',{className:'r'},r.views.toLocaleString()+(r.diagnosis?(' · '+r.diagnosis):'')));card.append(line)}body.append(card)}
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
  body.append(el('h2',{},'Run something now'));
  for(const a of d.actions){const st=d.status[a.action]||{};const card=el('div',{className:'act'});
    const top=el('div',{className:'top'});top.append(el('span',{className:'lb'},a.label));
    const stx=el('span',{className:'st '+(st.state||'')});
    stx.textContent=st.state==='running'?'running…':(st.state==='done'?('done '+ago(st.at)):(st.state==='error'?'error':''));
    top.append(stx);card.append(top);card.append(el('div',{className:'ab'},a.about));
    if(st.result)card.append(el('div',{className:'ab'},'Last: '+st.result));
    if(st.error)card.append(el('div',{className:'err'},st.error));
    const b=el('button',{className:'ghost',style:'margin-top:4px'},a.label);b.onclick=()=>runAction(a.action,b);if(st.state==='running')b.disabled=true;card.append(b);
    body.append(card)}
  const ra=el('button',{className:'ghost'},'Resume all paused channels');
  ra.onclick=async()=>{ra.disabled=true;try{await api.post('/api/resume-all');toast('Resumed');loadControls()}catch(e){alert(e.message)}ra.disabled=false};
  body.append(ra);
  if((d.blocks||[]).length){body.append(el('h2',{},'Takedown blocks'));
    for(const b of d.blocks){const card=el('div',{className:'act'});const who=(b.authors&&b.authors[0])||b.source||b.parent||'blocked';
      card.append(el('div',{className:'lb'},who));
      if(b.reason)card.append(el('div',{className:'ab'},b.reason));
      const u=el('button',{className:'danger',style:'margin-top:4px'},'Unblock');const key=who;
      u.onclick=async()=>{u.disabled=true;try{await api.post('/api/unblock',{name:key});toast('Unblocked');loadControls()}catch(e){u.disabled=false;alert(e.message)}};card.append(u);body.append(card)}
    setAlert('controls',true);
  }else setAlert('controls',false);
}
async function loadControls(){try{renderControls(await api.get('/api/controls'))}catch(e){$('ct-body').replaceChildren(el('div',{className:'err'},'Could not load.'))}}

/* ---------- alerts on the tab bar ---------- */
function setAlert(tab,on){const b=$('nav-'+tab);if(b)b.classList.toggle('alert',!!on)}

/* ---------- wiring ---------- */
const loaders={
  review:async()=>{try{lastQ=await api.get('/api/queue');renderReview()}catch(e){if(e instanceof Response&&e.status===401)$('rv-stats').textContent='Session expired — reopen your link.'}},
  channels:loadChannels,status:()=>loadDoctor(false),report:loadReport,controls:loadControls,
};
show('review');
// Refresh the tab you are looking at. Review and the quick tabs poll; never while you are typing or a clip plays.
setInterval(()=>{const a=document.activeElement;const typing=a&&(a.tagName==='INPUT'||a.tagName==='TEXTAREA'||a.tagName==='SELECT');
  if(typing)return;if(active==='review'&&reviewBusy())return;if(active==='status')return;loaders[active]&&loaders[active]()},60000);
// Keep the review badge current even on another tab, cheaply.
setInterval(async()=>{if(active==='review')return;try{const q=await api.get('/api/queue');setAlert('review',q.clips.length>0)}catch(e){}},90000);
</script></body></html>"""
