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
:root{--bg:#0f0f10;--card:#1b1b1e;--text:#f2f2f2;--dim:#a0a0a8;--ok:#2fbf71;--no:#e5484d;--line:#2a2a2e;--accent:#4f8cff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 system-ui,-apple-system,sans-serif}
header{position:sticky;top:0;background:var(--bg);padding:12px 16px 8px;border-bottom:1px solid var(--line);z-index:2}
.hwrap{max-width:560px;margin:0 auto}
h1{font-size:18px;margin:0 0 4px}.stats{color:var(--dim);font-size:13px}
.chips{display:flex;gap:8px;overflow-x:auto;padding:10px 0 2px;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{padding:6px 12px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--dim);
 white-space:nowrap;font-size:13px;cursor:pointer;user-select:none}
.chip.on{background:var(--text);color:var(--bg);border-color:var(--text);font-weight:600}
.chip .n{opacity:.6;margin-left:5px;font-variant-numeric:tabular-nums}
main{padding:12px 16px 48px;display:grid;gap:14px;max-width:560px;margin:0 auto}
.strip{display:flex;gap:10px;overflow-x:auto;padding:2px 0 2px;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.strip::-webkit-scrollbar{display:none}
.cc{min-width:150px;flex:0 0 auto;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.cc h3{font-size:14px;margin:0 0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cc .big{font-size:22px;font-weight:700}.cc .sub{color:var(--dim);font-size:12px;margin-top:2px}
.card{background:var(--card);border-radius:14px;overflow:hidden;border:1px solid var(--line)}
video{width:100%;aspect-ratio:9/16;max-height:70vh;background:#000;display:block}
.body{padding:12px;display:grid;gap:8px}.meta{color:var(--dim);font-size:13px}
.pill{display:inline-block;font-size:11px;font-weight:600;color:var(--accent);border:1px solid var(--accent);
 border-radius:999px;padding:2px 8px;letter-spacing:.02em}
.flag{color:#f5a524;font-size:13px}
input{width:100%;padding:10px;border-radius:10px;border:1px solid var(--line);background:#111;color:var(--text);font-size:15px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:8px}
button{padding:14px;border:0;border-radius:10px;font-size:16px;font-weight:600;color:#fff;cursor:pointer}
.yes{background:var(--ok)}.nope{background:var(--no)}button:disabled{opacity:.5}
.empty{color:var(--dim);text-align:center;padding:40px 0}
h2{font-size:16px;margin:4px 0 0}.post{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;display:grid;gap:4px}
.post a{color:var(--text);font-weight:600;text-decoration:none}.nums{display:flex;gap:14px;flex-wrap:wrap;color:var(--dim);font-size:13px}
.verdict{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}.winner{color:var(--ok)}
.weak_hold,.no_reach,.low_views{color:#f5a524}.why{color:var(--dim);font-size:13px}
</style></head><body>
<header><div class="hwrap"><h1>Review clips</h1><div class="stats" id="stats">Loading…</div>
<div class="chips" id="chips"></div></div></header>
<main id="strip" hidden></main>
<main id="list"></main>
<main id="posted"></main>
<script>
const stats=document.getElementById('stats'),chipsBox=document.getElementById('chips'),
      stripBox=document.getElementById('strip'),list=document.getElementById('list'),postedBox=document.getElementById('posted');
const edits={};let last=null,filter='all';
try{filter=localStorage.getItem('reviewFilter')||'all'}catch(e){}
function el(tag,attrs,text){const e=document.createElement(tag);Object.assign(e,attrs||{});if(text!=null)e.textContent=text;return e}
function fmt(iso){return iso?new Date(iso).toLocaleString([],{weekday:'short',hour:'2-digit',minute:'2-digit'}):'—'}
function setFilter(f){filter=f;try{localStorage.setItem('reviewFilter',f)}catch(e){}render()}

function renderChips(channels){
  chipsBox.replaceChildren();
  if(!channels.length){return}
  const total=channels.reduce((n,c)=>n+c.waiting,0);
  const mk=(slug,label,n)=>{const c=el('div',{className:'chip'+(filter===slug?' on':'')});c.append(el('span',{},label));
    if(n)c.append(el('span',{className:'n'},n));c.onclick=()=>setFilter(slug);return c};
  chipsBox.append(mk('all','All',total));
  for(const c of channels)chipsBox.append(mk(c.slug,c.name,c.waiting));
}
function renderStrip(channels){
  if(!channels.length){stripBox.hidden=true;return}
  stripBox.hidden=false;stripBox.replaceChildren();
  for(const c of channels){
    const card=el('div',{className:'cc'});
    card.append(el('h3',{},c.name));
    card.append(el('div',{className:'big'},c.waiting));
    card.append(el('div',{className:'sub'},'waiting to review'));
    card.append(el('div',{className:'sub'},`${c.approved} queued · ${c.posted_today} posted today`));
    card.append(el('div',{className:'sub'},'next '+fmt(c.next_post)));
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
  const card=el('div',{className:'card'});
  card.append(el('video',{src:'/media/'+encodeURIComponent(c.id),controls:true,playsInline:true,preload:'metadata'}));
  const b=el('div',{className:'body'});
  if(c.channel)b.append(el('span',{className:'pill'},c.channel));
  const t=el('input',{value:c.id in edits?edits[c.id]:c.title,'aria-label':'Title'});
  t.addEventListener('input',()=>{edits[c.id]=t.value});
  b.append(t);
  b.append(el('div',{className:'meta'},[c.creator&&('Credit: '+c.creator),Math.round(c.seconds)+'s',c.kind].filter(Boolean).join(' · ')));
  if(c.hook)b.append(el('div',{className:'meta'},'Hook: '+c.hook));
  if(c.flag)b.append(el('div',{className:'flag'},'Flagged: '+c.flag));
  const row=el('div',{className:'row'}),yes=el('button',{className:'yes'},'Approve'),no=el('button',{className:'nope'},'Reject');
  async function decide(path,body){yes.disabled=no.disabled=true;
    const r=await fetch('/api/clips/'+encodeURIComponent(c.id)+'/'+path,{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
    if(r.ok){delete edits[c.id];load()}else{yes.disabled=no.disabled=false;alert((await r.json()).detail||'Failed')}}
  yes.onclick=()=>decide('approve',{title:t.value});no.onclick=()=>decide('reject');
  row.append(yes,no);b.append(row);card.append(b);return card;
}
function render(){
  if(!last)return;
  const d=last,channels=d.channels||[];
  if(channels.length&&filter!=='all'&&!channels.some(c=>c.slug===filter))filter='all';
  const next=d.next_post?fmt(d.next_post):'not planned yet';
  stats.textContent=`${d.clips.length} to review · ${d.approved_waiting} queued · ${d.posted_today} posted today · next ${next}`;
  renderChips(channels);renderStrip(channels);renderPosted(d.posted||[]);
  const clips=(filter==='all')?d.clips:d.clips.filter(c=>c.channel_slug===filter);
  for(const id of Object.keys(edits))if(!d.clips.some(c=>c.id===id))delete edits[id];
  list.replaceChildren();
  if(!clips.length){list.append(el('div',{className:'empty'},'Nothing to review here right now.'));return}
  for(const c of clips)list.append(clipCard(c));
}
async function load(){
  const r=await fetch('/api/queue',{credentials:'same-origin'});
  if(!r.ok){stats.textContent='Not signed in. Open the link with ?key= again.';return}
  last=await r.json();render();
}
load();setInterval(load,60000);
</script></body></html>"""
