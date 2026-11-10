"""The phone review page: approve or reject clips before they can be posted.

It runs as its own small server (scripts/run.py starts it on port 8600) so that only these
routes are reachable when a tunnel exposes it, never the main API. Every route needs the
REVIEW_TOKEN, given once as ?key= and then kept in a cookie.
"""
from __future__ import annotations

import hmac

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.config import settings
from app.db import init_db
from app.growth import approval

review_app = FastAPI(title="Doomscroller review", docs_url=None, redoc_url=None, openapi_url=None)
_COOKIE = "review_key"


@review_app.on_event("startup")
def _startup() -> None:
    init_db()


def _allowed(request: Request) -> bool:
    token = settings.review_token
    given = request.cookies.get(_COOKIE) or ""
    return bool(token) and hmac.compare_digest(given.encode(), token.encode())


def _guard(request: Request) -> None:
    if not settings.review_token:
        raise HTTPException(503, "REVIEW_TOKEN is not set, so the review page is shut")
    if not _allowed(request):
        raise HTTPException(401, "open the link with ?key= first")


class Decision(BaseModel):
    title: str = ""


@review_app.get("/", response_class=HTMLResponse)
def page(request: Request, key: str = ""):
    if key and settings.review_token and hmac.compare_digest(key.encode(), settings.review_token.encode()):
        response = RedirectResponse("/", status_code=303)
        secure = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
        response.set_cookie(_COOKIE, key, max_age=90 * 24 * 3600, httponly=True, samesite="lax", secure=secure)
        return response
    if not _allowed(request):
        return HTMLResponse(_LOCKED, status_code=401 if settings.review_token else 503)
    return HTMLResponse(_PAGE)


@review_app.get("/api/queue")
def api_queue(request: Request) -> JSONResponse:
    _guard(request)
    return JSONResponse(approval.queue())


@review_app.post("/api/clips/{video_id}/approve")
def api_approve(video_id: str, decision: Decision, request: Request) -> dict:
    _guard(request)
    try:
        return approval.approve(video_id, decision.title)
    except approval.ReviewError as exc:
        raise HTTPException(409, str(exc))


@review_app.post("/api/clips/{video_id}/reject")
def api_reject(video_id: str, request: Request) -> dict:
    _guard(request)
    try:
        return approval.reject(video_id)
    except approval.ReviewError as exc:
        raise HTTPException(409, str(exc))


@review_app.post("/api/clips/{video_id}/undo")
def api_undo(video_id: str, request: Request) -> dict:
    _guard(request)
    try:
        return approval.undo(video_id)
    except approval.ReviewError as exc:
        raise HTTPException(409, str(exc))


@review_app.get("/media/{video_id}")
def media(video_id: str, request: Request):
    _guard(request)
    path = approval.media_path(video_id)
    if path is None:
        raise HTTPException(404, "no such clip")
    return FileResponse(path, media_type="video/mp4")


_LOCKED = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Review</title><body style="font:16px system-ui;background:#111;color:#eee;padding:24px">
<p>Open this page with the link that has <code>?key=</code> in it.</p></body>"""

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Review clips</title>
<style>
:root{--bg:#0f0f10;--card:#1b1b1e;--text:#f2f2f2;--dim:#a0a0a8;--ok:#2fbf71;--no:#e5484d;--line:#2a2a2e;--accent:#4f8cff;--warn:#f5a524}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 system-ui,-apple-system,sans-serif}
header{position:sticky;top:0;background:var(--bg);padding:10px 16px 6px;border-bottom:1px solid var(--line);z-index:2}
.hwrap{max-width:560px;margin:0 auto}
h1{font-size:15px;margin:0;display:inline}.stats{color:var(--dim);font-size:12px;margin-top:2px}
.chips{display:flex;gap:8px;overflow-x:auto;padding:8px 0 2px;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{padding:7px 13px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--dim);
 white-space:nowrap;font-size:13px;cursor:pointer;user-select:none}
.chip.on{background:var(--text);color:var(--bg);border-color:var(--text);font-weight:600}
.chip.att{border-color:var(--warn)}
.chip .n{opacity:.75;margin-left:5px;font-variant-numeric:tabular-nums}
main{padding:10px 16px 40px;display:grid;gap:12px;max-width:560px;margin:0 auto}
#strip{display:flex;gap:10px;overflow-x:auto;padding:2px 0;-webkit-overflow-scrolling:touch;scrollbar-width:none}
#strip::-webkit-scrollbar{display:none}
.cc{min-width:150px;flex:0 0 auto;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:9px 11px}
.cc.att{border-color:var(--warn)}
.cc h3{font-size:13px;margin:0 0 3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cc .sub{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums}
.cc .attn{color:var(--warn);font-size:12px;font-weight:600;margin-top:3px}
.banner{background:#3a1416;border:1px solid var(--no);color:#ffb4b4;border-radius:10px;padding:10px 12px;font-size:13px;font-weight:600}
.card{background:var(--card);border-radius:14px;overflow:hidden;border:1px solid var(--line)}
.card.flagged{border-color:var(--warn)}
video{width:100%;aspect-ratio:9/16;max-height:62vh;background:#000;display:block}
.body{padding:12px;display:grid;gap:8px}.meta{color:var(--dim);font-size:13px}
.pill{display:inline-block;font-size:11px;font-weight:600;color:var(--accent);border:1px solid var(--accent);
 border-radius:999px;padding:2px 8px;letter-spacing:.02em}
.warnbox{background:#3a2a10;border:1px solid var(--warn);color:#f5d58a;border-radius:8px;padding:8px 10px;font-size:13px;font-weight:600}
.err{color:var(--no);font-size:13px}
input{width:100%;padding:10px;border-radius:10px;border:1px solid var(--line);background:#111;color:var(--text);font-size:15px}
.actions{display:grid;gap:8px}
button{border:0;border-radius:10px;font-weight:600;cursor:pointer}
.yes{background:var(--ok);color:#06240f;padding:15px;font-size:16px}
.nope{background:transparent;color:var(--no);border:1px solid var(--no);padding:11px;font-size:14px}
.card.flagged .yes{background:transparent;color:var(--dim);border:1px solid var(--line);padding:11px;font-size:14px;order:2}
.card.flagged .nope{background:var(--no);color:#fff;border:0;padding:15px;font-size:16px;order:1}
button:disabled{opacity:.5}
.empty{color:var(--dim);text-align:center;padding:40px 0}
.snack{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:#2a2a2e;color:var(--text);
 border:1px solid var(--line);border-radius:10px;padding:10px 16px;display:flex;gap:16px;align-items:center;
 font-size:14px;box-shadow:0 6px 20px rgba(0,0,0,.45);z-index:5;max-width:90vw}
.snack button{background:none;color:var(--accent);border:0;font-weight:700;font-size:14px;padding:4px;cursor:pointer}
h2{font-size:15px;margin:4px 0 0}.post{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;display:grid;gap:4px}
.post a{color:var(--text);font-weight:600;text-decoration:none}.nums{display:flex;gap:14px;flex-wrap:wrap;color:var(--dim);font-size:13px}
.verdict{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}.winner{color:var(--ok)}
.weak_hold,.no_reach,.low_views{color:var(--warn)}.why{color:var(--dim);font-size:13px}
</style></head><body>
<header><div class="hwrap"><h1>Review clips</h1><div class="stats" id="stats">Loading…</div>
<div class="chips" id="chips"></div></div></header>
<main id="banner" hidden></main>
<main id="strip" hidden></main>
<main id="list"></main>
<main id="posted"></main>
<div id="snack" class="snack" hidden></div>
<script>
const stats=document.getElementById('stats'),chipsBox=document.getElementById('chips'),bannerBox=document.getElementById('banner'),
      stripBox=document.getElementById('strip'),list=document.getElementById('list'),postedBox=document.getElementById('posted'),
      snack=document.getElementById('snack');
const edits={},cards=new Map();let last=null,filter='all',stale=false,snackTimer=null;
try{filter=localStorage.getItem('reviewFilter')||'all'}catch(e){}
function el(tag,attrs,text){const e=document.createElement(tag);Object.assign(e,attrs||{});if(text!=null)e.textContent=text;return e}
function fmt(iso){return iso?new Date(iso).toLocaleString([],{weekday:'short',hour:'2-digit',minute:'2-digit'}):'—'}
function setFilter(f){filter=f;try{localStorage.setItem('reviewFilter',f)}catch(e){}render()}
function busy(){const a=document.activeElement;return(a&&a.tagName==='INPUT'&&list.contains(a))||[...list.querySelectorAll('video')].some(v=>!v.paused&&!v.ended)}
function toast(label,id){
  clearTimeout(snackTimer);snack.replaceChildren(el('span',{},label));
  const u=el('button',{},'Undo');
  u.onclick=async()=>{clearTimeout(snackTimer);snack.hidden=true;
    try{const r=await fetch('/api/clips/'+encodeURIComponent(id)+'/undo',{method:'POST',credentials:'same-origin'});
      if(!r.ok){let m='Undo failed';try{m=(await r.json()).detail||m}catch(e){}alert(m)}}catch(e){alert('Undo failed — network error')}
    load();};
  snack.append(u);snack.hidden=false;snackTimer=setTimeout(()=>{snack.hidden=true},6000);
}

function renderChips(channels){
  chipsBox.replaceChildren();
  if(!channels.length){return}
  const total=channels.reduce((n,c)=>n+c.waiting,0);
  const mk=(slug,label,n,att)=>{const c=el('div',{className:'chip'+(filter===slug?' on':'')+(att?' att':'')});c.append(el('span',{},label));
    if(n)c.append(el('span',{className:'n'},n));c.onclick=()=>setFilter(slug);return c};
  chipsBox.append(mk('all','All',total,false));
  for(const c of channels)chipsBox.append(mk(c.slug,c.name,c.waiting,!!c.attention));
}
function renderStrip(channels){
  if(!channels.length){stripBox.hidden=true;return}
  stripBox.hidden=false;stripBox.replaceChildren();
  for(const c of channels){
    const card=el('div',{className:'cc'+(c.attention?' att':'')});
    card.append(el('h3',{},c.name));
    card.append(el('div',{className:'sub'},`${c.waiting} to review · ${c.approved} queued`));
    card.append(el('div',{className:'sub'},`${c.posted_today} today · next ${fmt(c.next_post)}`));
    if(c.attention)card.append(el('div',{className:'attn'},c.attention));
    stripBox.append(card)}
}
function renderPosted(items){
  postedBox.replaceChildren(el('h2',{},'Recently posted'));
  if(!items.length){postedBox.append(el('div',{className:'empty'},'Nothing posted yet.'));return}
  for(const p of items){
    const card=el('div',{className:'post'});
    card.append(el('a',{href:p.url,target:'_blank',rel:'noopener'},p.title||'Untitled'));
    const nums=el('div',{className:'nums'});
    nums.append(el('span',{},p.views.toLocaleString()+' views'),el('span',{},p.likes.toLocaleString()+' likes'));
    if(p.watched!==null)nums.append(el('span',{},p.watched+'% watched'));
    nums.append(el('span',{},new Date(p.posted).toLocaleDateString()));
    card.append(nums);
    if(p.verdict){card.append(el('div',{className:'verdict '+p.verdict},p.verdict.replace('_',' ')));card.append(el('div',{className:'why'},p.why))}
    postedBox.append(card)}
}
function clipCard(c){
  const card=el('div',{className:'card'+(c.flag?' flagged':'')});
  card.append(el('video',{src:'/media/'+encodeURIComponent(c.id),controls:true,playsInline:true,preload:'metadata'}));
  const b=el('div',{className:'body'});
  if(c.flag)b.append(el('div',{className:'warnbox'},'Flagged — probably should NOT post: '+c.flag));
  if(c.channel)b.append(el('span',{className:'pill'},c.channel));
  const t=el('input',{value:c.id in edits?edits[c.id]:c.title,'aria-label':'Clip title'});
  t.addEventListener('input',()=>{edits[c.id]=t.value});
  b.append(t);
  b.append(el('div',{className:'meta'},[c.creator&&('Credit: '+c.creator),Math.round(c.seconds)+'s',c.kind].filter(Boolean).join(' · ')));
  if(c.hook)b.append(el('div',{className:'meta'},'Hook: '+c.hook));
  const errLine=el('div',{className:'err'});errLine.hidden=true;
  const actions=el('div',{className:'actions'}),yes=el('button',{className:'yes','aria-label':'Approve clip'},'Approve'),
        no=el('button',{className:'nope','aria-label':'Reject clip'},'Reject');
  let armed=false;
  async function decide(path,body){
    yes.disabled=no.disabled=true;errLine.hidden=true;
    try{
      const r=await fetch('/api/clips/'+encodeURIComponent(c.id)+'/'+path,{method:'POST',credentials:'same-origin',
        headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
      if(r.ok){delete edits[c.id];cards.delete(c.id);card.remove();
        toast(path==='approve'?'Approved':'Rejected',c.id);load();return}
      let msg='Failed ('+r.status+')';
      try{msg=(await r.json()).detail||msg}catch(e){}
      if(r.status===401)msg='Session expired — reopen your ?key= link.';
      errLine.textContent=msg;errLine.hidden=false;
    }catch(e){errLine.textContent='Network error — not sent. Check your connection and try again.';errLine.hidden=false;}
    card.style.transform='';card.style.opacity='';yes.disabled=no.disabled=false;
  }
  // Approving a flagged clip (the gate says it probably should not post) takes a confirming second tap.
  yes.onclick=()=>{if(c.flag&&!armed){armed=true;yes.textContent='Tap again to approve anyway';
      setTimeout(()=>{armed=false;yes.textContent='Approve'},3000);return}decide('approve',{title:t.value})};
  no.onclick=()=>decide('reject');
  actions.append(yes,no);b.append(actions,errLine);card.append(b);
  // Swipe: left = reject, right = approve. Ignores the video, title input and buttons, engages only on a
  // clearly-horizontal drag (so vertical scrolling is untouched), and a flagged clip's swipe-right snaps
  // back — approving a flagged clip still needs the confirming button tap.
  let tracking=false,horiz=false,startX=0,startY=0;
  card.style.transition='transform .2s ease, opacity .2s ease';
  const onCard=t=>!t.closest('video,input,button');
  card.addEventListener('touchstart',e=>{if(e.touches.length!==1||!onCard(e.target)){tracking=false;return}
    startX=e.touches[0].clientX;startY=e.touches[0].clientY;tracking=true;horiz=false},{passive:true});
  card.addEventListener('touchmove',e=>{if(!tracking)return;
    const dx=e.touches[0].clientX-startX,dy=e.touches[0].clientY-startY;
    if(!horiz){if(Math.abs(dx)<12&&Math.abs(dy)<12)return;if(Math.abs(dy)>=Math.abs(dx)){tracking=false;return}horiz=true}
    e.preventDefault();card.style.transition='none';card.style.transform='translateX('+dx+'px)';
    card.style.opacity=String(Math.max(.35,1-Math.abs(dx)/350));},{passive:false});
  card.addEventListener('touchend',e=>{if(!tracking||!horiz){tracking=false;return}tracking=false;
    const dx=e.changedTouches[0].clientX-startX;card.style.transition='transform .2s ease, opacity .2s ease';
    const past=Math.abs(dx)>90,right=dx>0;
    if(past&&right&&!c.flag){card.style.transform='translateX(120%)';card.style.opacity='0';decide('approve',{title:t.value});return}
    if(past&&!right){card.style.transform='translateX(-120%)';card.style.opacity='0';decide('reject');return}
    card.style.transform='';card.style.opacity='';});
  return card;
}
function renderList(clips){
  const want=new Map(clips.map(c=>[c.id,c]));
  for(const [id,card] of cards){if(!want.has(id)){card.remove();cards.delete(id)}}
  const emptyEl=list.querySelector('.empty');
  if(!clips.length){if(!emptyEl)list.append(el('div',{className:'empty'},'Nothing to review here right now.'));return}
  if(emptyEl)emptyEl.remove();
  let prev=null;
  for(const c of clips){
    let card=cards.get(c.id);
    if(!card){card=clipCard(c);cards.set(c.id,card);prev?prev.after(card):list.prepend(card)}
    prev=card;
  }
}
function render(){
  if(!last)return;
  const d=last,channels=d.channels||[];
  if(channels.length&&filter!=='all'&&!channels.some(c=>c.slug===filter))filter='all';
  const next=d.next_post?fmt(d.next_post):'not planned yet';
  stats.textContent=(stale?'OFFLINE — showing older data · ':'')+
    `${d.clips.length} to review · ${d.approved_waiting} queued · ${d.posted_today} posted today · next ${next}`;
  stats.style.color=stale?'var(--warn)':'';
  const paused=(channels.find(c=>c.attention&&/paused|hold/i.test(c.attention))||{}).attention;
  if(paused){bannerBox.hidden=false;bannerBox.replaceChildren(el('div',{className:'banner'},paused+' — some channels are not posting.'))}
  else bannerBox.hidden=true;
  renderChips(channels);renderStrip(channels);renderPosted(d.posted||[]);
  const clips=(filter==='all')?d.clips:d.clips.filter(c=>c.channel_slug===filter);
  for(const id of Object.keys(edits))if(!d.clips.some(c=>c.id===id))delete edits[id];
  renderList(clips);
}
async function load(){
  try{
    const r=await fetch('/api/queue',{credentials:'same-origin'});
    if(!r.ok){stats.textContent='Not signed in. Open the link with ?key= again.';stats.style.color='var(--warn)';return}
    last=await r.json();stale=false;render();
  }catch(e){stale=true;if(last)render();else{stats.textContent='Offline — could not load. Retrying…';stats.style.color='var(--warn)'}}
}
// The background refresh never rebuilds a card you are watching or typing in; the list is diffed by id.
load();setInterval(()=>{if(!busy())load()},60000);
</script></body></html>"""
