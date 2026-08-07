"""Web UI — run: python web.py"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from flask import Flask, Response, jsonify, render_template_string, request

from hunter import (
    CLAIM_ENABLED, DB, OWNER_EMAIL, WEB_PORT, WEB_REFRESH_MIN,
    claim_specific, fetch_all, game_dict, is_free_listing, normalize_image_url,
    resolve_game_image, run, send_email_to, subscriber_digest,
)

app = Flask(__name__)
STATE = {
    "games": [], "updated": None, "fetching": False,
    "claiming": False, "claim_msg": "", "claim_i": 0, "claim_n": 0,
}

APP_VERSION = "1.0.0"

IMG_HOSTS = (
    "steamstatic.com", "akamai.steamstatic.com", "cloudflare.steamstatic.com",
    "epicgames.com", "unrealengine.com", "gamerpower.com", "cheapshark.com",
    "gog.com", "gog-statics.com",
)


@app.after_request
def no_cache(resp):
    if request.path == "/api/img":
        return resp
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

LAYOUT = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }}</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Serif:wght@500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#f7f7f7;--surface:#fafafa;--card:#fff;--border:#c8c8c8;--ink:#0d0d0d;--muted:#6e6e6e;--dim:#9a9a9a}
body{font-family:"IBM Plex Sans",system-ui,sans-serif;background:var(--bg);color:var(--ink);min-height:100vh;line-height:1.5;padding-bottom:4.5rem;
background-image:radial-gradient(circle at 1px 1px,rgba(0,0,0,.04) 1px,transparent 0);background-size:24px 24px}
a{color:inherit;text-decoration:none}
.top{background:var(--surface);border-bottom:1px solid var(--ink);position:sticky;top:0;z-index:50}
.top-in{max-width:1100px;margin:0 auto;padding:0 1.25rem;height:64px;display:flex;align-items:center;gap:1rem}
.brand{font-family:"IBM Plex Serif",Georgia,serif;font-weight:600;font-size:1.35rem;letter-spacing:-.02em}
.nav{display:flex;border:1px solid var(--ink)}
.nav a{padding:.45rem .9rem;font-family:"IBM Plex Mono",monospace;font-size:.68rem;font-weight:500;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.nav a:hover,.nav a.on{color:#f7f7f7;background:var(--ink)}
.nav a+a{border-left:1px solid var(--ink)}
.top-r{margin-left:auto;display:flex;align-items:center;gap:.5rem;font-size:.78rem;color:var(--muted);font-family:"IBM Plex Mono",monospace}
.btn{padding:.45rem 1rem;border:1px solid var(--ink);font-weight:600;font-size:.75rem;cursor:pointer;background:var(--ink);color:#f7f7f7;text-transform:uppercase;letter-spacing:.06em}
.btn:hover{background:#2e2e2e}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn.ghost{background:transparent;color:var(--ink)}
.hero{border-bottom:1px solid var(--border);padding:2rem 1.25rem;background:var(--surface)}
.hero-in{max-width:1100px;margin:0 auto}
.hero h1{font-family:"IBM Plex Serif",Georgia,serif;font-size:clamp(1.6rem,3.5vw,2.2rem);font-weight:600;margin-bottom:.4rem}
.hero p{color:var(--muted);font-size:.95rem;max-width:36rem}
.main{max-width:1100px;margin:0 auto;padding:1.5rem 1.25rem}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;margin-bottom:1.25rem;background:var(--ink);border:1px solid var(--ink)}
.stat{padding:1rem;background:var(--card)}
.stat b{display:block;font-family:"IBM Plex Serif",Georgia,serif;font-size:1.5rem;font-weight:600}
.stat span{font-family:"IBM Plex Mono",monospace;font-size:.62rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.panel{background:var(--card);border:1px solid var(--border);padding:1.25rem}
.filters{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin-bottom:1rem}
.chip{padding:.35rem .85rem;font-family:"IBM Plex Mono",monospace;font-size:.68rem;font-weight:500;cursor:pointer;border:1px solid var(--border);background:var(--surface);color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.chip:hover{color:var(--ink);border-color:var(--ink)}
.chip.on{background:var(--ink);color:#f7f7f7;border-color:var(--ink)}
.sep{width:1px;height:18px;background:var(--border)}
.sub{margin-left:auto;display:flex;gap:.4rem}
.sub input{padding:.45rem .75rem;border:1px solid var(--border);background:#fff;color:var(--ink);font-size:.82rem;min-width:150px}
.msg{font-size:.78rem;color:var(--muted);margin-top:.4rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:.75rem}
.card{border:1px solid var(--border);overflow:hidden;background:var(--card);position:relative}
.card.sel{border-color:var(--ink);border-width:2px}
.card:hover{border-color:var(--ink)}
.card .pick{position:absolute;top:.5rem;left:.5rem;z-index:2;width:18px;height:18px;accent-color:var(--ink);cursor:pointer}
.card img{width:100%;height:110px;object-fit:cover;background:#eee;display:block}
.card .ph{height:110px;background:#eee;display:flex;align-items:center;justify-content:center;color:var(--dim);font-family:"IBM Plex Mono",monospace;font-size:.7rem}
.card-body{padding:.85rem}
.card h3{font-size:.86rem;font-weight:600;margin-bottom:.3rem;line-height:1.35}
.tags{display:flex;flex-wrap:wrap;gap:.3rem;margin-bottom:.35rem}
.tag{font-family:"IBM Plex Mono",monospace;font-size:.58rem;font-weight:500;padding:.12rem .4rem;text-transform:uppercase;border:1px solid var(--border);color:var(--muted)}
.meta{font-size:.7rem;color:var(--muted);margin-bottom:.5rem}
.actions{display:flex;gap:.35rem}
.actions .go{flex:1;text-align:center;padding:.4rem;font-size:.72rem;font-weight:600;border:1px solid var(--border);color:var(--ink);background:transparent;cursor:pointer}
.actions .go:hover{background:#eee}
.actions .claim{background:var(--ink);color:#f7f7f7;border-color:var(--ink)}
.empty{text-align:center;padding:3rem 1rem;color:var(--muted);grid-column:1/-1}
.date-section{margin-bottom:1.75rem}
.date-section h2{font-family:"IBM Plex Mono",monospace;font-size:.72rem;font-weight:500;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:.75rem;padding-bottom:.4rem;border-bottom:1px solid var(--border)}
.date-section .grid{margin-top:0}
footer{text-align:center;padding:1.5rem;color:var(--dim);font-family:"IBM Plex Mono",monospace;font-size:.68rem;border-top:1px solid var(--border)}
.cart{position:fixed;bottom:0;left:0;right:0;background:var(--surface);border-top:1px solid var(--ink);padding:.75rem 1.5rem;z-index:60;display:flex;align-items:center;gap:1rem;transform:translateY(100%);transition:transform .2s}
.cart.show{transform:translateY(0)}
.cart span{font-size:.85rem;color:var(--muted)}
.claimbar{position:fixed;bottom:1rem;right:1rem;z-index:70;background:var(--ink);border:1px solid var(--ink);padding:.65rem 1rem;display:flex;align-items:center;gap:.75rem;max-width:320px;color:#f7f7f7}
.claimbar.hide{display:none}
.pad{width:36px;height:28px;position:relative;flex-shrink:0}
.pad::before{content:'';position:absolute;inset:0;border:2px solid #f7f7f7}
.pad::after{content:'';position:absolute;width:6px;height:6px;background:#f7f7f7;border-radius:50%;top:8px;left:6px;box-shadow:10px 0 0 #f7f7f7;animation:blink 1s step-end infinite}
.claimbar p{font-size:.78rem;line-height:1.35}
.claimbar b{display:block;font-size:.82rem;margin-bottom:.1rem}
@keyframes blink{50%{opacity:.3}}
@media(max-width:640px){.sub{margin-left:0;width:100%}.cart{flex-wrap:wrap}.stats{grid-template-columns:1fr}}
</style></head><body>
<div id="claimbar" class="claimbar hide">
  <div class="pad" aria-hidden="true"></div>
  <div><b id="claimtitle">Claiming…</b><p id="claimsub">Please wait</p></div>
</div>
<div id="cart" class="cart"><span id="carttxt">0 selected</span>
  <button class="btn" onclick="claimSelected()">Claim selected</button>
  <button class="btn ghost" onclick="clearCart()">Clear</button>
</div>
<header class="top"><div class="top-in">
  <a href="/" class="brand">DropAlert</a>
  <nav class="nav">
    <a href="/" class="{{ 'on' if page=='free' else '' }}">Free Games</a>
    <a href="/claimed" class="{{ 'on' if page=='claimed' else '' }}">Claimed</a>
  </nav>
  <div class="top-r">
    <span id="meta">…</span>
    {% if page=='free' %}
    <button class="btn ghost" id="refreshbtn" onclick="refreshGames()">Refresh</button>
    <button class="btn" id="claimbtn" onclick="claimAll()">Claim all</button>
    {% endif %}
  </div>
</div></header>
{{ content|safe }}
<footer>DropAlert v{{ version }} · auto-refresh {{ refresh }} min</footer>
<script>{{ script|safe }}</script>
</body></html>"""

FREE_PAGE = r"""
<section class="hero"><div class="hero-in">
  <h1>Free games &amp; free-to-play</h1>
  <p>Select games like a cart — claim one, many, or all. Headless, store-only.</p>
</div></section>
<div class="main">
<div class="stats">
  <div class="stat"><span>Giveaways</span><b id="s-free">0</b></div>
  <div class="stat"><span>Free to play</span><b id="s-f2p">0</b></div>
  <div class="stat"><span>Upcoming</span><b id="s-up">0</b></div>
</div>
<div class="panel">
<div class="filters">
  <button class="chip on" data-p="all">All</button>
  <button class="chip" data-p="Steam">Steam</button>
  <button class="chip" data-p="Epic Games">Epic</button>
  <button class="chip" data-p="GOG">GOG</button>
  <div class="sep"></div>
  <button class="chip on" data-k="all">All</button>
  <button class="chip" data-k="free">Giveaways</button>
  <button class="chip" data-k="f2p">F2P</button>
  <button class="chip" data-k="upcoming">Upcoming</button>
  <form class="sub" onsubmit="return subscribe(event)">
    <input type="email" id="email" placeholder="Email alerts" required>
    <button type="submit" class="btn ghost">Subscribe</button>
  </form>
</div>
<div class="msg" id="submsg"></div>
<div class="grid" id="grid"></div>
</div></div>"""

CLAIMED_PAGE = r"""
<section class="hero"><div class="hero-in">
  <h1>Claimed games</h1>
  <p>Games added to your library.</p>
</div></section>
<div class="main"><div class="panel"><div id="grid"></div></div></div>"""

FREE_JS = r"""
let games=[], platform='all', kind='all', cart=new Set();
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');}
function imgUrl(u){
  if(!u) return '';
  if(u.startsWith('//')) u='https:'+u;
  if(u.startsWith('/')) return u;
  return '/api/img?u='+encodeURIComponent(u);
}
function imgTag(u,ph){
  if(!u) return `<div class="ph">${ph}</div>`;
  if(u.startsWith('//')) u='https:'+u;
  const direct=encodeURIComponent(u);
  return `<img src="${imgUrl(u)}" data-u="${direct}" alt="" loading="lazy" referrerpolicy="no-referrer"
    onerror="if(!this.dataset.fallback){this.dataset.fallback=1;this.src=decodeURIComponent(this.dataset.u)}else{this.onerror=null;this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',textContent:'${ph}'}))}">`;
}
function card(g){
  const sel=cart.has(g.fp);
  const chk=g.claimable?`<input type="checkbox" class="pick" data-fp="${esc(g.fp)}" ${sel?'checked':''} onchange="toggleCart(this)">`:'';
  const claim=g.claimable?`<button class="go claim" onclick="claimFps(['${esc(g.fp)}'])">Claim</button>`:'';
  return `<article class="card${sel?' sel':''}" data-fp="${esc(g.fp)}">${chk}
    ${imgTag(g.image,'▪')}
    <div class="card-body">
    <div class="tags"><span class="tag">${g.kind}</span><span class="tag">${g.platform}</span></div>
    <h3>${esc(g.title)}</h3>
    <div class="meta">${g.starts?'From '+g.starts+' · ':''}${g.ends?'Ends '+g.ends:''}</div>
    <div class="actions"><a class="go" href="${g.url}" target="_blank" rel="noopener">Open</a>${claim}</div>
  </div></article>`;
}
function render(){
  const f=games.filter(g=>(platform==='all'||g.platform===platform)&&(kind==='all'||g.kind===kind));
  const el=document.getElementById('grid');
  if(!f.length){el.innerHTML='<div class="empty"><h3>No games</h3><p>Nothing matches or all claimed.</p></div>';updateCart();return;}
  el.innerHTML=f.map(card).join('');
  updateCart();
}
function toggleCart(el){
  const fp=el.dataset.fp;
  if(el.checked) cart.add(fp); else cart.delete(fp);
  el.closest('.card').classList.toggle('sel',el.checked);
  updateCart();
}
function updateCart(){
  const n=cart.size;
  document.getElementById('cart').classList.toggle('show',n>0);
  document.getElementById('carttxt').textContent=n+' selected';
}
function clearCart(){cart.clear();render();}
function showClaimBar(on,title,sub){
  const b=document.getElementById('claimbar');
  b.classList.toggle('hide',!on);
  if(title) document.getElementById('claimtitle').textContent=title;
  if(sub) document.getElementById('claimsub').textContent=sub;
}
function pollClaim(){
  const t=setInterval(async()=>{
    const d=await(await fetch('/api/games')).json();
    if(d.claiming){
      showClaimBar(true,d.claim_msg||'Claiming…',d.claim_i&&d.claim_n?d.claim_i+' / '+d.claim_n:'Working…');
    }else{
      clearInterval(t);
      if((d.claim_msg||'').startsWith('Store login')){
        showClaimBar(true,'Login required',d.claim_msg);
      }else{
        showClaimBar(false);
      }
      cart.clear();
      load();
    }
  },1000);
}
async function load(){
  const r=await fetch('/api/games'); const d=await r.json();
  games=d.available;
  document.getElementById('s-free').textContent=d.counts.free;
  document.getElementById('s-f2p').textContent=d.counts.f2p;
  document.getElementById('s-up').textContent=d.counts.upcoming;
  document.getElementById('meta').textContent=d.fetching?'Refreshing…':d.updated;
  const cb=document.getElementById('claimbtn'),rb=document.getElementById('refreshbtn');
  if(cb) cb.disabled=d.claiming||d.fetching;
  if(rb) rb.disabled=d.fetching||d.claiming;
  if(d.claiming) showClaimBar(true,d.claim_msg||'Claiming…',d.claim_i&&d.claim_n?d.claim_i+' / '+d.claim_n:'');
  render();
}
async function subscribe(e){e.preventDefault();
  const r=await fetch('/api/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.getElementById('email').value})});
  const d=await r.json(); document.getElementById('submsg').textContent=d.ok?'Subscribed!':(d.error||'Failed'); return false;}
async function refreshGames(){
  await fetch('/api/refresh',{method:'POST'});
  const t=setInterval(async()=>{
    const d=await(await fetch('/api/games')).json();
    if(!d.fetching){clearInterval(t);load();}
  },1500);
}
async function claimFps(fps){
  showClaimBar(true,'Starting claim…','Headless — only needed stores');
  await fetch('/api/claim',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fps})});
  pollClaim();
}
function claimSelected(){if(cart.size) claimFps([...cart]);}
function claimAll(){
  showClaimBar(true,'Claiming all…','Headless — all stores');
  fetch('/api/claim',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all:true})});
  pollClaim();
}
document.querySelectorAll('[data-p]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-p]').forEach(x=>x.classList.remove('on'));b.classList.add('on');platform=b.dataset.p;render();});
document.querySelectorAll('[data-k]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-k]').forEach(x=>x.classList.remove('on'));b.classList.add('on');kind=b.dataset.k;render();});
setInterval(load,15000); load();
"""

CLAIMED_JS = r"""
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');}
function imgUrl(u){
  if(!u) return '';
  if(u.startsWith('//')) u='https:'+u;
  if(u.startsWith('/')) return u;
  return '/api/img?u='+encodeURIComponent(u);
}
function imgTag(u,ph){
  if(!u) return `<div class="ph">${ph}</div>`;
  if(u.startsWith('//')) u='https:'+u;
  const direct=encodeURIComponent(u);
  return `<img src="${imgUrl(u)}" data-u="${direct}" alt="" loading="lazy" referrerpolicy="no-referrer"
    onerror="if(!this.dataset.fallback){this.dataset.fallback=1;this.src=decodeURIComponent(this.dataset.u)}else{this.onerror=null;this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',textContent:'${ph}'}))}">`;
}
function card(g){
  const link=g.url?`<a class="go" href="${g.url}" target="_blank">Open store</a>`:'';
  const time=g.claimed_at?g.claimed_at.slice(11,16):'';
  return `<article class="card">${imgTag(g.image,'✓')}<div class="card-body">
    <div class="tags"><span class="tag">${esc(g.platform)}</span></div>
    <h3>${esc(g.title)}</h3><div class="meta">${time||''}</div>${link}
  </div></article>`;
}
async function load(){
  const r=await fetch('/api/claimed'); const d=await r.json();
  document.getElementById('meta').textContent=d.updated;
  const el=document.getElementById('grid');
  if(!d.groups.length){el.innerHTML='<div class="empty"><h3>No claimed games yet</h3><p>Claim games from the Free Games page.</p></div>';return;}
  el.innerHTML=d.groups.map(sec=>`
    <section class="date-section">
      <h2>${esc(sec.label)} <span style="font-weight:400;color:var(--dim)">(${sec.games.length})</span></h2>
      <div class="grid">${sec.games.map(card).join('')}</div>
    </section>`).join('');
}
setInterval(load,15000); load();
"""


def _split_games():
    db = DB()
    claimed_fps = db.claimed_fps()
    claimed_keys = db.claimed_keys()
    games = [g for g in (STATE["games"] or []) if is_free_listing(g)]

    def _is_claimed(g) -> bool:
        if g.fp() in claimed_fps:
            return True
        return (g.title, g.platform) in claimed_keys

    unclaimed = [g for g in games if not _is_claimed(g)]
    available = [game_dict(g) for g in unclaimed]
    return available, claimed_fps, unclaimed


def _games_payload() -> dict:
    available, _, games = _split_games()
    return {
        "available": available,
        "updated": STATE["updated"] or "never",
        "fetching": STATE["fetching"],
        "claiming": STATE["claiming"],
        "claim_msg": STATE.get("claim_msg", ""),
        "claim_i": STATE.get("claim_i", 0),
        "claim_n": STATE.get("claim_n", 0),
        "counts": {
            "free": sum(1 for g in games if g.claimable and g.kind == "free"),
            "f2p": sum(1 for g in games if g.kind == "f2p"),
            "upcoming": sum(1 for g in games if g.kind == "upcoming"),
        },
    }


def _refresh():
    STATE["fetching"] = True
    try:
        STATE["games"] = fetch_all()
        STATE["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        db = DB()
        db.sync_claimed_images(STATE["games"])
        new = db.filter_new(STATE["games"])
        if new:
            from hunter import notify_subscribers
            notify_subscribers(STATE["games"], new)
            db.mark_notified(new)
    finally:
        STATE["fetching"] = False


def _bg_loop():
    _refresh()
    while True:
        time.sleep(WEB_REFRESH_MIN * 60)
        _refresh()


def _claim_progress(msg: str, i: int, n: int) -> None:
    STATE["claim_msg"] = msg
    STATE["claim_i"] = i
    STATE["claim_n"] = n


@app.route("/")
def index():
    return render_template_string(LAYOUT, title="DropAlert", page="free",
                                  content=FREE_PAGE, script=FREE_JS, refresh=WEB_REFRESH_MIN,
                                  version=APP_VERSION)


@app.route("/claimed")
def claimed_page():
    return render_template_string(LAYOUT, title="Claimed", page="claimed",
                                  content=CLAIMED_PAGE, script=CLAIMED_JS, refresh=WEB_REFRESH_MIN,
                                  version=APP_VERSION)


@app.route("/api/games")
def api_games():
    return jsonify(_games_payload())


@app.route("/api/img")
def img_proxy():
    url = unquote(request.args.get("u", "")).strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("https://", "http://")):
        return "", 404
    host = urlparse(url).netloc.lower()
    if not any(h in host for h in IMG_HOSTS):
        return "", 403
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        })
        with urlopen(req, timeout=15) as resp:
            data = resp.read()
            if not data:
                return "", 404
            ctype = resp.headers.get("Content-Type", "image/jpeg")
        return Response(data, mimetype=ctype, headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        return "", 404


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    if STATE["fetching"]:
        return jsonify({"ok": False})
    threading.Thread(target=_refresh, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/claimed")
def api_claimed():
    db = DB()
    by_fp = {g.fp(): g for g in (STATE["games"] or [])}
    by_key = {(g.title, g.platform): g for g in (STATE["games"] or [])}
    buckets: dict[str, list[dict]] = {}
    for row in db.claimed_list():
        g = by_fp.get(row["fp"]) or by_key.get((row["title"], row["platform"]))
        img = resolve_game_image(
            row.get("fp", ""), row["title"], row["platform"],
            row.get("url") or (g.url if g else ""),
            row.get("image", ""), g,
        )
        raw = row.get("claimed_at") or ""
        day = raw[:10] if len(raw) >= 10 else "Unknown"
        buckets.setdefault(day, []).append({
            "title": row["title"],
            "platform": row["platform"],
            "url": row.get("url") or (g.url if g else ""),
            "image": img,
            "claimed_at": raw[:16].replace("T", " "),
        })

    def _label(day: str) -> str:
        if day == "Unknown":
            return "Unknown date"
        try:
            dt = datetime.strptime(day, "%Y-%m-%d").date()
            today = datetime.now(timezone.utc).date()
            if dt == today:
                return "Today"
            if (today - dt).days == 1:
                return "Yesterday"
            return dt.strftime("%A, %B %d, %Y")
        except ValueError:
            return day

    groups = [
        {"date": day, "label": _label(day), "games": buckets[day]}
        for day in sorted(buckets.keys(), reverse=True)
    ]
    return jsonify({"groups": groups, "updated": STATE["updated"] or "never"})


@app.route("/api/subscribe", methods=["POST"])
def api_subscribe():
    email = (request.get_json(silent=True) or {}).get("email", "")
    if not re.match(r"^[^@]+@[^@]+\.[^@]+$", email.strip()):
        return jsonify({"ok": False, "error": "Invalid email"})
    if email.strip().lower() == OWNER_EMAIL:
        return jsonify({"ok": False, "error": "Owner uses auto-claim"})
    if not DB().add_subscriber(email):
        return jsonify({"ok": False, "error": "Already subscribed"})
    subj, body = subscriber_digest(STATE["games"] or fetch_all(), "Welcome to DropAlert")
    send_email_to(email.strip().lower(), subj, body)
    return jsonify({"ok": True})


@app.route("/api/claim", methods=["POST"])
def api_claim():
    if STATE["claiming"] or not CLAIM_ENABLED:
        return jsonify({"ok": False})
    body = request.get_json(silent=True) or {}
    fps = [x.strip() for x in body.get("fps", []) if x.strip()]
    if body.get("fp", "").strip():
        fps = [body["fp"].strip()]

    def job():
        STATE["claiming"] = True
        STATE["claim_msg"] = "Starting…"
        STATE["claim_i"] = 0
        STATE["claim_n"] = 0
        try:
            games = STATE["games"] or fetch_all()
            if body.get("all"):
                _, _, unclaimed = _split_games()
                fps = [g.fp() for g in unclaimed if g.claimable]
                if fps:
                    claim_specific(
                        games, fps, progress=_claim_progress,
                        email=len(fps) > 5,
                    )
            elif fps:
                claim_specific(
                    games, fps, progress=_claim_progress,
                    email=len(fps) > 5,
                )
            STATE["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        finally:
            STATE["claiming"] = False
            if not (STATE.get("claim_msg") or "").startswith("Store login"):
                STATE["claim_msg"] = ""

    threading.Thread(target=job, daemon=True).start()
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("Fetching games… (Steam F2P may take ~30s on first load)")
    threading.Thread(target=_bg_loop, daemon=True).start()
    print(f"Open http://localhost:{WEB_PORT}")
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False)
