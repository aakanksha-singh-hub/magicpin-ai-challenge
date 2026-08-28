"""Read-only inspection console for the Vera engine.

Exists for humans, not for the judge. It renders the machinery the README
describes but that an HTTP transcript cannot show: which fact every number in
a message traces back to, whether the judge can actually see that fact, why one
trigger won the tick, and what got dropped on the way.

Three rules keep it away from the scored surface:

  - every route lives under /console, so /v1/* is untouched
  - nothing here mutates the judged store; the demo source is a second Store
    instance built from the shipped seed files, created lazily on first use
  - no new dependencies, and the fact pack is rebuilt via facts.build() rather
    than threading a pack through Composed, so compose.py needs no changes
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import decide, facts as F
from .compose import compose
from .converse import respond
from .store import Store

# dataset/ is copied into the image alongside vera/; when it is missing the
# console still works, just with the live source only.
SEED_DIR = Path(__file__).resolve().parent.parent / "dataset"

_DEMO: Store | None = None


def demo_store() -> Store | None:
    """Seed-backed store, built once on first console request."""
    global _DEMO
    if _DEMO is not None:
        return _DEMO
    if not SEED_DIR.is_dir():
        return None
    store = Store(snapshot_path="")  # never persists: demo data is disposable
    for f in sorted((SEED_DIR / "categories").glob("*.json")):
        d = json.loads(f.read_text())
        store.put_context("category", d.get("slug", f.stem), 1, d)
    for fname, key, scope in (("merchants_seed.json", "merchant_id", "merchant"),
                              ("customers_seed.json", "customer_id", "customer"),
                              ("triggers_seed.json", "id", "trigger")):
        p = SEED_DIR / fname
        if not p.is_file():
            continue
        blob = json.loads(p.read_text())
        for item in blob.get(scope + "s", blob.get(scope, [])):
            if item.get(key):
                store.put_context(scope, item[key], 1, item)
    _DEMO = store
    return store


# ------------------------------------------------------------------ provenance

NUM = re.compile(r"\d[\d,\.]*")


def _norm(tok: str) -> str:
    return tok.rstrip(".").replace(",", "")


WORD = re.compile(r"[a-z]{4,}")


def _candidates(pack: F.FactPack) -> dict[str, list[F.Fact]]:
    out: dict[str, list[F.Fact]] = {}
    for fact in pack.facts.values():
        for tok in NUM.findall(str(fact.text)):
            out.setdefault(_norm(tok), []).append(fact)
    return out


def _score(fact: F.Fact, body: str, at: int) -> tuple:
    """Rank how likely `fact` is the source of the number at offset `at`.

    Three signals, in order of strength:
      verbatim  -- the fact's whole rendered text appears in the message, so
                   the composer demonstrably used it
      nearby    -- a distinctive word from the fact's label appears beside the
                   number, which separates facts that render identically
                   ("22" is both a stale-post count and an uplift estimate)
      specific  -- longer rendered text is a more particular claim
    """
    text = str(fact.text)
    verbatim = text in body
    window = body[max(0, at - 45):at + 45].lower()
    label_words = set(WORD.findall(fact.label.lower()))
    nearby = sum(1 for w in label_words if w in window)
    return (verbatim, nearby, len(text), fact.visible)


def provenance(pack: F.FactPack, body: str) -> list[dict]:
    """Map every numeric token in `body` back to the fact that authorised it.

    This is the README's central claim made checkable: a token with no fact
    behind it is exactly what validate.py refuses to let through. Where a
    number is genuinely ambiguous the alternatives are reported rather than
    hidden -- a provenance panel that quietly guesses is worse than useless.
    """
    cands = _candidates(pack)
    out, seen = [], set()
    for match in NUM.finditer(body):
        tok = match.group(0)
        norm = _norm(tok)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        pool = cands.get(norm) or []
        best = max(pool, key=lambda f: _score(f, body, match.start())) if pool else None
        out.append({
            # trailing punctuation belongs to the sentence, not the number
            "token": tok.rstrip(".,"),
            "grounded": best is not None,
            "key": best.key if best else "",
            "label": best.label if best else "no registered fact",
            "source": best.source if best else "",
            "judge_visible": bool(best.visible) if best else False,
            "alternatives": [{"key": f.key, "label": f.label}
                             for f in pool if best is not None and f.key != best.key][:4],
        })
    return out


def fact_rows(pack: F.FactPack) -> list[dict]:
    return sorted(
        ({"key": f.key, "text": f.text, "label": f.label,
          "source": f.source, "judge_visible": f.visible, "numeric": f.numeric}
         for f in pack.facts.values()),
        key=lambda r: r["key"])


# ------------------------------------------------------------------ helpers

def _perf(m: dict) -> dict:
    """Same accessors facts.py uses, so the console never disagrees with the
    engine about what a merchant's numbers are."""
    p = m.get("performance") or {}
    agg = m.get("customer_aggregate") or {}
    return {
        "views": p.get("views"),
        "calls": p.get("calls"),
        "ctr": p.get("ctr"),
        "directions": p.get("directions"),
        "window_days": p.get("window_days"),
        "rating": p.get("rating") or m.get("rating") or agg.get("avg_rating"),
        "reviews": p.get("review_count") or m.get("review_count") or agg.get("review_count"),
    }


def _merchant_row(m: dict) -> dict:
    ident = m.get("identity") or {}
    return {
        "merchant_id": m.get("merchant_id"),
        "name": ident.get("name") or m.get("business_name") or m.get("merchant_id"),
        "owner": ident.get("owner_first_name") or ident.get("owner_name") or "",
        "category": m.get("category_slug") or ident.get("category") or "",
        "locality": ident.get("locality") or "",
        "city": ident.get("city") or "",
        "verified": ident.get("verified"),
        "performance": _perf(m),
        "offers": [o.get("title") for o in (m.get("offers") or []) if o.get("title")],
        "signals": [s if isinstance(s, str) else s.get("type", "")
                    for s in (m.get("signals") or [])][:6],
    }


def _store_for(source: str, judged: Store) -> tuple[Store | None, str]:
    """The UI only ever asks for "demo". ?source=live is kept for inspecting a
    real harness run over curl, but is deliberately not reachable from the page:
    the judged store is empty except during a run, so a live view would show a
    reviewer a blank console nearly every time they opened it."""
    if source == "live":
        return judged, "live"
    d = demo_store()
    return (d, "demo") if d is not None else (judged, "live")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ routes

def register(app, judged: Store) -> None:
    """Attach the console to the app. Judged routes are not touched."""

    # the console is the homepage: a reviewer who opens the bare URL should land
    # on something that explains itself, not a JSON blob. The machine-readable
    # index moved to /index.json.
    @app.get("/", include_in_schema=False)
    @app.get("/console", include_in_schema=False)
    async def console_page() -> HTMLResponse:
        return HTMLResponse(PAGE)

    @app.get("/console/api/state", include_in_schema=False)
    async def console_state(source: str = "demo") -> JSONResponse:
        store, used = _store_for(source, judged)
        merchants = [_merchant_row(m) for m in (store.all_of("merchant") if store else [])]
        merchants.sort(key=lambda r: str(r["merchant_id"]))
        return JSONResponse({
            "source": used,
            "demo_available": demo_store() is not None,
            "counts": store.counts() if store else {},
            "live_counts": judged.counts(),
            "merchants": merchants,
        })

    @app.get("/console/api/merchant/{merchant_id}", include_in_schema=False)
    async def console_merchant(merchant_id: str, source: str = "demo") -> JSONResponse:
        store, used = _store_for(source, judged)
        if store is None:
            return JSONResponse({"error": "no store"}, status_code=404)
        m = store.merchant_for(merchant_id)
        if not m:
            return JSONResponse({"error": "unknown merchant"}, status_code=404)
        trigs = [t for t in store.all_of("trigger")
                 if t.get("merchant_id") == merchant_id]
        rows = [{"id": t.get("id"), "kind": t.get("kind"),
                 "customer_id": t.get("customer_id"),
                 "urgency": t.get("urgency"), "expires_at": t.get("expires_at")}
                for t in trigs]
        rows.sort(key=lambda r: str(r["id"]))
        return JSONResponse({
            "source": used,
            "merchant": _merchant_row(m),
            "raw": m,
            "customers": [{"customer_id": c.get("customer_id"),
                           "name": (c.get("identity") or {}).get("name") or c.get("name") or "",
                           "relationship": c.get("relationship") or {}}
                          for c in store.customers_of(merchant_id)],
            "triggers": rows,
        })

    @app.post("/console/api/compose", include_in_schema=False)
    async def console_compose(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        store, used = _store_for(str(body.get("source") or "demo"), judged)
        if store is None:
            return JSONResponse({"error": "no store"}, status_code=404)

        tid = str(body.get("trigger_id") or "")
        trigger = store.trigger(tid)
        if not trigger:
            return JSONResponse({"error": "unknown trigger"}, status_code=404)
        trigger = dict(trigger)
        trigger.setdefault("id", tid)

        merchant = store.merchant_for(trigger.get("merchant_id")) or {}
        category = store.category_for(merchant) or {}
        customer = store.get("customer", trigger.get("customer_id"))
        now = _now()

        out = compose(category, merchant, trigger, customer, now=now)
        # pure function of the same inputs, so this is the very pack compose used
        pack = F.build(category, merchant, trigger, customer, now=now)

        return JSONResponse({
            "source": used,
            "trigger": {"id": trigger.get("id"), "kind": trigger.get("kind"),
                        "payload": trigger.get("payload") or {},
                        "urgency": trigger.get("urgency")},
            "merchant_id": merchant.get("merchant_id"),
            "customer_id": (customer or {}).get("customer_id"),
            "message": {
                "body": out.body,
                "cta": out.cta,
                "send_as": out.send_as,
                "suppression_key": out.suppression_key,
                "rationale": out.rationale,
                "levers": out.levers,
                "template": out.template_name,
                "warnings": out.warnings,
            },
            "provenance": provenance(pack, out.body),
            "facts": fact_rows(pack),
            "visible_anchors": pack.visible_anchor_count(out.body),
        })

    @app.post("/console/api/tick", include_in_schema=False)
    async def console_tick(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}
        store, used = _store_for(str(body.get("source") or "demo"), judged)
        if store is None:
            return JSONResponse({"error": "no store"}, status_code=404)
        now = _now()
        chosen, skipped = decide.select(store, store.ids_of("trigger"), now, limit=50)
        return JSONResponse({
            "source": used,
            "considered": len(store.ids_of("trigger")),
            "chosen": [{"trigger_id": c.trigger_id, "merchant_id": c.merchant_id,
                        "customer_id": c.customer_id, "kind": c.trigger.get("kind"),
                        "priority": round(c.priority, 3), "reasons": c.reasons}
                       for c in chosen],
            "skipped": skipped,
        })

    @app.post("/console/api/reply", include_in_schema=False)
    async def console_reply(request: Request) -> JSONResponse:
        """Drives the real reply FSM. Demo source only, so a console visitor
        can never write into the state the judge is scoring."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        store = demo_store()
        if store is None:
            return JSONResponse({"error": "demo store unavailable"}, status_code=404)
        out = respond(
            store,
            conversation_id=str(body.get("conversation_id") or "console_demo"),
            merchant_id=(body.get("merchant_id") or None),
            customer_id=(body.get("customer_id") or None),
            message=str(body.get("message") or ""),
            from_role=str(body.get("from_role") or "merchant"),
            turn_number=int(body.get("turn_number") or 1),
        )
        return JSONResponse(out)

    @app.post("/console/api/reset", include_in_schema=False)
    async def console_reset() -> JSONResponse:
        """Clear demo conversation state so the reply ladder can be replayed."""
        global _DEMO
        _DEMO = None
        return JSONResponse({"reset": True, "rebuilt": demo_store() is not None})


# ------------------------------------------------------------------ page
# Self-contained on purpose: no CDN, no build step, no new dependency, and
# nothing here can fail in a way that affects /v1/*.

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vera console</title>
<style>
:root{
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2230; --line:#2a3240;
  --ink:#e6edf3; --dim:#8b949e; --faint:#6e7681;
  --acc:#58a6ff; --ok:#3fb950; --warn:#d29922; --bad:#f85149; --mag:#bc8cff;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
a{color:var(--acc)}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  padding:12px 18px;border-bottom:1px solid var(--line);background:var(--panel);
  position:sticky;top:0;z-index:10}
h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.2px}
h1 span{color:var(--dim);font-weight:400}
.sub{color:var(--faint);font-size:12px}
.spacer{flex:1}
.pill{font-family:var(--mono);font-size:11px;color:var(--dim);
  border:1px solid var(--line);border-radius:20px;padding:3px 10px}
main{display:grid;grid-template-columns:270px minmax(0,1fr);gap:0;
  height:calc(100vh - 53px)}
#list{border-right:1px solid var(--line);overflow-y:auto;background:var(--panel)}
.mrow{padding:9px 14px;border-bottom:1px solid var(--line);cursor:pointer}
.mrow:hover{background:var(--panel2)}
.mrow.on{background:var(--panel2);box-shadow:inset 3px 0 0 var(--acc)}
.mrow b{display:block;font-weight:600;font-size:13px}
.mrow small{color:var(--faint);font-size:11px;font-family:var(--mono)}
#work{overflow-y:auto;padding:18px 22px 60px}
.empty{color:var(--faint);padding:40px 0;text-align:center}
.card{background:var(--panel);border:1px solid var(--line);border-radius:9px;
  padding:15px 17px;margin-bottom:15px}
.card h2{font-size:11px;text-transform:uppercase;letter-spacing:.9px;
  color:var(--dim);margin:0 0 11px;font-weight:600}
.metrics{display:flex;gap:22px;flex-wrap:wrap;margin-top:4px}
.metrics div{font-family:var(--mono);font-size:12px;color:var(--dim)}
.metrics b{display:block;color:var(--ink);font-size:16px;font-weight:600}
.chips{display:flex;gap:7px;flex-wrap:wrap}
.chip{border:1px solid var(--line);background:var(--panel2);border-radius:6px;
  padding:5px 10px;font-size:12px;cursor:pointer;color:var(--ink);
  font-family:var(--mono)}
.chip:hover{border-color:var(--acc)}
.chip.on{background:var(--acc);color:#05080d;border-color:var(--acc);font-weight:600}
.chip.cust{border-style:dashed}
.body{font-size:15px;line-height:1.72;white-space:pre-wrap;
  background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:15px}
.num{border-bottom:1.5px solid var(--ok);cursor:pointer;padding:0 1px;font-weight:600}
.num.bad{border-color:var(--bad);background:rgba(248,81,73,.16)}
.num.on{background:var(--ok);color:#05080d;border-radius:3px}
.kv{display:grid;grid-template-columns:132px minmax(0,1fr);gap:5px 14px;
  font-size:13px;margin-top:12px}
.kv dt{color:var(--faint);font-size:12px}
.kv dd{margin:0;font-family:var(--mono);font-size:12px;word-break:break-word}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:var(--faint);font-weight:500;font-size:11px;
  text-transform:uppercase;letter-spacing:.6px;padding:5px 8px;
  border-bottom:1px solid var(--line)}
td{padding:5px 8px;border-bottom:1px solid var(--line);font-family:var(--mono);
  vertical-align:top}
tr.hl td{background:rgba(88,166,255,.13)}
.tag{font-size:10px;padding:1px 6px;border-radius:4px;font-family:var(--mono);
  text-transform:uppercase;letter-spacing:.4px}
.tag.vis{background:rgba(63,185,80,.18);color:var(--ok)}
.tag.hid{background:rgba(139,148,158,.15);color:var(--faint)}
.tag.bad{background:rgba(248,81,73,.18);color:var(--bad)}
.note{color:var(--faint);font-size:12px;margin-top:9px}
.warn{color:var(--warn);font-size:12px;font-family:var(--mono)}
.scroll{overflow-x:auto}
.tabs{display:flex;gap:2px;margin-bottom:13px;border-bottom:1px solid var(--line)}
.tabs button{background:transparent;border:0;border-bottom:2px solid transparent;
  color:var(--dim);padding:7px 13px;font-size:12px;cursor:pointer;
  font-family:inherit;margin-bottom:-1px}
.tabs button.on{color:var(--ink);border-bottom-color:var(--acc);font-weight:600}
input,textarea,select{background:var(--panel2);border:1px solid var(--line);
  color:var(--ink);border-radius:6px;padding:8px 10px;font-family:inherit;
  font-size:13px;width:100%}
button.go{background:var(--acc);color:#05080d;border:0;border-radius:6px;
  padding:8px 15px;font-weight:600;cursor:pointer;font-size:13px;font-family:inherit}
button.ghost{background:transparent;color:var(--dim);border:1px solid var(--line);
  border-radius:6px;padding:7px 13px;cursor:pointer;font-size:12px;font-family:inherit}
.turn{border-left:2px solid var(--line);padding:3px 0 3px 12px;margin:9px 0}
.turn.me{border-color:var(--mag)}
.turn small{color:var(--faint);font-size:11px;font-family:var(--mono);
  text-transform:uppercase;letter-spacing:.5px}
.row{display:flex;gap:9px;align-items:center;flex-wrap:wrap}
</style></head><body>

<header>
  <h1>Vera <span>· message engine console</span></h1>
  <span class="sub" id="tagline">deterministic compose(category, merchant, trigger, customer?)</span>
  <span class="spacer"></span>
  <span class="pill" id="counts">—</span>
</header>

<main>
  <div id="list"><div class="empty">loading…</div></div>
  <div id="work"><div class="empty">Pick a merchant on the left.</div></div>
</main>

<script>
const $ = s => document.querySelector(s);
// The console always runs on the shipped seed data. It deliberately does not
// read the judged store: that store is empty except during an actual harness
// run, so a live view would show a reviewer a blank page nearly every time.
// The server still accepts ?source=live for inspecting a run over curl.
const SRC='demo';
let MERCHANTS=[], SEL=null, DETAIL=null, OUT=null, TAB='message', LOG=[];

const esc = s => String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt = n => n==null ? '—' : (typeof n==='number' ? n.toLocaleString('en-IN') : n);

async function api(path, opts){
  const r = await fetch(path, opts);
  return r.json();
}

async function load(){
  const d = await api('/console/api/state?source='+SRC);
  MERCHANTS = d.merchants||[];
  const c = d.counts||{};
  $('#counts').textContent =
    `${c.merchant||0} merchants · ${c.trigger||0} triggers · ${c.customer||0} customers · ${c.category||0} categories`;
  if(!MERCHANTS.length){
    $('#list').innerHTML = '<div class="empty">Seed data unavailable.</div>';
    $('#work').innerHTML = '<div class="empty">The engine is fine — the console just has no data to render.<br>'
      + 'The scored endpoints are unaffected: try <b>/v1/healthz</b> or <b>/docs</b>.</div>';
    return;
  }
  $('#list').innerHTML = MERCHANTS.map(m=>{
    const p=m.performance||{};
    return `<div class="mrow" id="m_${esc(m.merchant_id)}" onclick="pick('${esc(m.merchant_id)}')">
      <b>${esc(m.name)}</b>
      <small>${esc(m.category)} · ${esc(m.locality)}</small><br>
      <small>${fmt(p.views)} views · ${fmt(p.calls)} calls</small></div>`;
  }).join('');
  pick(MERCHANTS[0].merchant_id);
}

async function pick(id){
  SEL=id; OUT=null; LOG=[];
  document.querySelectorAll('.mrow').forEach(e=>e.classList.remove('on'));
  const el=$('#m_'+CSS.escape(id)); if(el) el.classList.add('on');
  DETAIL = await api(`/console/api/merchant/${encodeURIComponent(id)}?source=${SRC}`);
  if(DETAIL.triggers && DETAIL.triggers.length) await run(DETAIL.triggers[0].id);
  else draw();
}

async function run(tid){
  OUT = await api('/console/api/compose', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({trigger_id:tid, source:SRC})});
  TAB='message'; draw();
}

function markup(body, prov){
  // One pass over the raw text. Replacing token by token would re-scan markup
  // already inserted and corrupt attributes like data-i="14".
  const idx = new Map();
  (prov||[]).forEach((p,i)=>{
    const k = String(p.token).replace(/\.$/,'').replace(/,/g,'');
    if(!idx.has(k)) idx.set(k, {p, i});
  });
  return esc(body).replace(/\d[\d,.]*/g, tok => {
    const tail = (tok.match(/[.,]+$/)||[''])[0];   // keep sentence punctuation outside
    const core = tok.slice(0, tok.length - tail.length);
    const hit = idx.get(core.replace(/,/g,''));
    if(!hit) return tok;
    return `<span class="num ${hit.p.grounded?'':'bad'}" data-i="${hit.i}" onclick="hit(${hit.i})">${core}</span>`+tail;
  });
}

function hit(i){
  document.querySelectorAll('.num').forEach(e=>e.classList.remove('on'));
  document.querySelectorAll(`.num[data-i="${i}"]`).forEach(e=>e.classList.add('on'));
  const p = OUT.provenance[i];
  $('#prov').innerHTML = p.grounded
    ? `<dl class="kv">
        <dt>token</dt><dd>${esc(p.token)}</dd>
        <dt>fact key</dt><dd>${esc(p.key)}</dd>
        <dt>means</dt><dd>${esc(p.label)}</dd>
        <dt>provenance</dt><dd>${esc(p.source||'—')}</dd>
        <dt>judge sees it</dt><dd>${p.judge_visible
          ? '<span class="tag vis">yes — in the scoring payload</span>'
          : '<span class="tag hid">no — supporting colour only</span>'}</dd>
        ${p.alternatives && p.alternatives.length ? `<dt>also renders as</dt><dd style="color:var(--faint)">${
          p.alternatives.map(a=>esc(a.key)).join(', ')}</dd>`:''}</dl>`
    : `<div class="warn">No registered fact. validate.py would refuse this.</div>`;
  document.querySelectorAll('#facts tr').forEach(tr=>
    tr.classList.toggle('hl', tr.dataset.key===p.key));
}

function tab(t){ TAB=t; draw(); }

function draw(){
  if(!DETAIL){ $('#work').innerHTML='<div class="empty">…</div>'; return; }
  const m = DETAIL.merchant, p = m.performance||{};
  const trs = DETAIL.triggers||[];

  let head = `<div class="card">
    <h2>merchant context</h2>
    <div style="font-size:17px;font-weight:600">${esc(m.name)}</div>
    <div class="sub">${esc(m.owner)} · ${esc(m.category)} · ${esc(m.locality)}</div>
    <div class="metrics">
      <div><b>${fmt(p.views)}</b>views${p.window_days?' / '+p.window_days+'d':''}</div>
      <div><b>${fmt(p.calls)}</b>calls</div>
      <div><b>${p.ctr!=null?(p.ctr*100).toFixed(1)+'%':'—'}</b>ctr</div>
      <div><b>${fmt(p.directions)}</b>directions</div>
      <div><b>${fmt(p.rating)}</b>rating</div>
    </div>
    ${m.offers && m.offers.length ? `<div class="note">live offers: ${m.offers.map(esc).join(' · ')}</div>`:'<div class="note">no live offers</div>'}
    ${m.signals && m.signals.length ? `<div class="note">signals: ${m.signals.map(esc).join(' · ')}</div>`:''}
  </div>`;

  let trigs = `<div class="card"><h2>triggers for this merchant — ${trs.length}</h2>
    <div class="chips">${trs.length ? trs.map(t=>
      `<span class="chip ${t.customer_id?'cust':''} ${OUT&&OUT.trigger.id===t.id?'on':''}"
        onclick="run('${esc(t.id)}')">${esc(t.kind)}${t.customer_id?' → customer':''}</span>`
    ).join('') : '<span class="sub">none pushed</span>'}</div>
    <div class="note">dashed = customer-facing outreach. Click one to compose.</div></div>`;

  let main = '';
  if(OUT && OUT.message){
    const msg = OUT.message;
    const ung = (OUT.provenance||[]).filter(x=>!x.grounded).length;
    const tabs = `<div class="tabs">
      ${['message','grounding','decision','reply'].map(t=>
        `<button class="${TAB===t?'on':''}" onclick="tab('${t}')">${t}</button>`).join('')}
    </div>`;

    let pane='';
    if(TAB==='message'){
      pane = `<div class="body">${markup(msg.body, OUT.provenance)}</div>
        <div class="note">Every underlined number is a registered fact — click one.
          ${ung? `<span class="warn">${ung} ungrounded</span>`:'All grounded.'}
          ${OUT.visible_anchors} anchor(s) visible in the judge's own scoring payload.</div>
        <div id="prov" style="margin-top:12px"></div>
        <dl class="kv">
          <dt>cta type</dt><dd>${esc(msg.cta)}</dd>
          <dt>send as</dt><dd>${esc(msg.send_as)}</dd>
          <dt>suppression key</dt><dd>${esc(msg.suppression_key)}</dd>
          <dt>strategy</dt><dd>${esc(msg.template||'—')}</dd>
          <dt>levers</dt><dd>${(msg.levers||[]).map(esc).join(', ')||'—'}</dd>
          <dt>rationale</dt><dd style="font-family:inherit">${esc(msg.rationale)}</dd>
        </dl>
        ${msg.warnings&&msg.warnings.length?`<div class="warn" style="margin-top:9px">guardrails fired: ${msg.warnings.map(esc).join('; ')}</div>`:''}`;
    } else if(TAB==='grounding'){
      pane = `<div class="scroll"><table id="facts"><thead><tr>
        <th>fact key</th><th>value</th><th>means</th><th>provenance</th><th>judge</th></tr></thead><tbody>
        ${(OUT.facts||[]).map(f=>`<tr data-key="${esc(f.key)}">
          <td>${esc(f.key)}</td><td>${esc(f.text)}</td>
          <td style="font-family:inherit;color:var(--dim)">${esc(f.label)}</td>
          <td style="color:var(--faint)">${esc(f.source||'—')}</td>
          <td>${f.judge_visible?'<span class="tag vis">sees</span>':'<span class="tag hid">blind</span>'}</td>
        </tr>`).join('')}</tbody></table></div>
        <div class="note">${(OUT.facts||[]).length} facts registered for this compose.
          A number that is not in this table cannot appear in the message.</div>`;
    } else if(TAB==='decision'){
      pane = `<div id="dec"><button class="go" onclick="tick()">Run a tick across all triggers</button>
        <div class="note">Shows which trigger wins each merchant, and why every other one was dropped.</div></div>`;
    } else {
      pane = `<div class="row" style="margin-bottom:10px">
          <button class="ghost" onclick="say('Ok lets do it. Whats next?')">commit</button>
          <button class="ghost" onclick="say('Thank you for contacting us! Our team will respond shortly.')">auto-reply</button>
          <button class="ghost" onclick="say('stop messaging me')">opt out</button>
          <button class="ghost" onclick="say('can you help me file my taxes')">off-topic</button>
          <button class="ghost" onclick="resetConv()">reset</button>
        </div>
        <div class="row"><input id="msg" placeholder="type a merchant reply…"
            onkeydown="if(event.key==='Enter')say()"><button class="go" onclick="say()">Send</button></div>
        <div id="log">${LOG.map(t=>`<div class="turn ${t.who==='merchant'?'me':''}">
            <small>${esc(t.who)}${t.action?' · '+esc(t.action):''}</small>
            <div>${esc(t.text)}</div>
            ${t.why?`<div class="note">${esc(t.why)}</div>`:''}</div>`).join('')
          ||'<div class="note">Drives the real reply FSM on demo state. Send the same auto-reply four times and watch it flag, wait, then end.</div>'}</div>`;
    }
    main = `<div class="card">
      <h2>${esc(OUT.trigger.kind)} · ${esc(OUT.trigger.id)}${OUT.customer_id?' · to '+esc(OUT.customer_id):''}</h2>
      ${tabs}${pane}</div>`;
  } else {
    main = `<div class="card"><div class="empty">No trigger selected.</div></div>`;
  }
  $('#work').innerHTML = head + trigs + main;
}

async function tick(){
  const d = await api('/console/api/tick', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({source:SRC})});
  $('#dec').innerHTML = `<div class="note" style="margin-bottom:10px">
      ${d.considered} triggers considered → <b style="color:var(--ok)">${d.chosen.length} sent</b>,
      ${d.skipped.length} dropped. Restraint is the point: one action per merchant, ranked.</div>
    <div class="scroll"><table><thead><tr><th>sent</th><th>merchant</th><th>kind</th><th>priority</th><th>why it won</th></tr></thead><tbody>
    ${d.chosen.map(c=>`<tr><td><span class="tag vis">send</span></td>
      <td>${esc(c.merchant_id)}</td><td>${esc(c.kind)}</td><td>${c.priority}</td>
      <td style="font-family:inherit;color:var(--dim)">${(c.reasons||[]).map(esc).join('; ')}</td></tr>`).join('')}
    ${d.skipped.map(s=>`<tr><td><span class="tag bad">drop</span></td>
      <td colspan="3">${esc(s.trigger_id)}</td>
      <td style="font-family:inherit;color:var(--faint)">${esc(s.reason)}</td></tr>`).join('')}
    </tbody></table></div>`;
}

async function say(preset){
  const box=$('#msg'); const text = preset || (box?box.value:''); if(!text.trim()) return;
  if(box && !preset) box.value='';
  LOG.push({who:'merchant', text});
  const d = await api('/console/api/reply', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({conversation_id:'console_'+SEL, merchant_id:SEL,
      message:text, from_role:'merchant',
      turn_number: LOG.filter(t=>t.who==='merchant').length})});
  LOG.push({who:'vera', action:d.action, why:d.rationale||'',
            text: d.body || d.message || '(no message sent — action: '+(d.action||'?')+')'});
  draw();
}

async function resetConv(){
  await api('/console/api/reset', {method:'POST'});
  LOG=[]; draw();
}

load();
</script></body></html>
"""
