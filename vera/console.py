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


# ------------------------------------------------------------------ labels
# The engine speaks in slugs -- research_digest, binary_yes_no,
# m_001_drmeera_dentist_delhi. Those are the right identifiers for code and the
# wrong words for a person reading the screen, so the console renders a title
# for anything a human sees and keeps the slug only where it is genuinely the
# subject (a fact key, a suppression key).

KIND_TITLES = {
    "research_digest": "Research digest",
    "regulation_change": "Regulation change",
    "compliance_alert": "Compliance alert",
    "cde_opportunity": "Continuing education",
    "supply_alert": "Supply alert",
    "recall_due": "Recall due",
    "chronic_refill_due": "Repeat prescription due",
    "appointment_tomorrow": "Appointment tomorrow",
    "trial_followup": "Trial follow-up",
    "wedding_package_followup": "Wedding package follow-up",
    "curious_ask_due": "Unanswered question",
    "active_planning_intent": "Planning already underway",
    "perf_spike": "Performance spike",
    "perf_dip": "Performance dip",
    "seasonal_perf_dip": "Seasonal dip",
    "milestone_reached": "Milestone reached",
    "review_theme_emerged": "New theme in reviews",
    "competitor_opened": "New competitor nearby",
    "gbp_unverified": "Google listing unverified",
    "renewal_due": "Renewal due",
    "dormant_with_vera": "Gone quiet with Vera",
    "category_seasonal": "Seasonal demand shift",
    "demand_shift": "Demand shift",
    "festival_upcoming": "Festival coming up",
    "ipl_match_today": "IPL match today",
    "local_event": "Local event",
    "customer_lapsed_soft": "Customer drifting away",
    "customer_lapsed_hard": "Customer long lapsed",
    "winback_eligible": "Win-back opportunity",
    "winback_customer": "Win-back opportunity",
    "__fallback__": "General update",
    "__customer_fallback__": "Customer update",
}

CTA_TITLES = {
    "binary_yes_no": "Yes / no",
    "binary_confirm_cancel": "Confirm / cancel",
    "open_ended": "Open question",
}

SEND_AS_TITLES = {
    "vera": "Vera",
    "merchant_on_behalf": "The merchant, via Vera",
}

ACRONYMS = {"ctr": "CTR", "cde": "CDE", "ipl": "IPL", "rct": "RCT", "gbp": "GBP",
            "id": "ID", "cta": "CTA", "ors": "ORS", "sms": "SMS", "eta": "ETA"}


def humanize(slug: str) -> str:
    """Fallback for anything not in a title map -- an unseen trigger kind from
    the judge still has to render as words rather than as a slug."""
    words = str(slug or "").strip("_").replace("__", " ").replace("_", " ").split()
    if not words:
        return ""
    out = [ACRONYMS.get(w.lower(), w) for w in words]
    if out[0] not in ACRONYMS.values():
        out[0] = out[0][:1].upper() + out[0][1:]
    return " ".join(out)


def kind_title(kind: str) -> str:
    return KIND_TITLES.get(str(kind or ""), humanize(kind)) or "Update"


# facts.py labels dynamic keys with k.replace("_", " "), which is right for the
# engine and wrong for a table a person reads: it leaves "retention 6mo pct" and
# "match time iso" on screen. Expanded here, in the display layer, rather than in
# facts.py -- those labels also feed rationale text the judge scores.
TERMS = {
    "pct": "%", "ytd": "year to date", "iso": "", "avg": "average",
    "rx": "prescription", "qty": "quantity", "freq": "frequency",
    "num": "number", "cnt": "count", "pref": "preference", "mins": "minutes",
    "3mo": "3-month", "6mo": "6-month", "12mo": "12-month",
    "7d": "7-day", "30d": "30-day", "60d": "60-day", "90d": "90-day",
    "180d": "180-day", "365d": "365-day",
}

_QUOTED = re.compile(r"'([a-z0-9_]+)'")


def pretty_label(label: str) -> str:
    """Turn an engine-side fact label into something worth reading."""
    text = _QUOTED.sub(lambda m: '"' + m.group(1).replace("_", " ") + '"', str(label or ""))
    out = []
    for word in text.replace("_", " ").split():
        low = word.lower()
        if low in TERMS:
            if TERMS[low]:
                out.append(TERMS[low])
        elif low in ACRONYMS:
            out.append(ACRONYMS[low])
        else:
            out.append(word)
    if not out:
        return ""
    first = out[0]
    if first not in ACRONYMS.values() and not first.startswith('"'):
        out[0] = first[:1].upper() + first[1:]
    return " ".join(out)


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
            "label": pretty_label(best.label) if best else "no registered fact",
            "source": best.source if best else "",
            "judge_visible": bool(best.visible) if best else False,
            "alternatives": [{"key": f.key, "label": pretty_label(f.label)}
                             for f in pool if best is not None and f.key != best.key][:4],
        })
    return out


def fact_rows(pack: F.FactPack) -> list[dict]:
    return sorted(
        ({"key": f.key, "text": f.text, "label": pretty_label(f.label),
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
        "category_title": humanize(m.get("category_slug") or ident.get("category") or ""),
        "locality": ident.get("locality") or "",
        "city": ident.get("city") or "",
        "verified": ident.get("verified"),
        "performance": _perf(m),
        "offers": [o.get("title") for o in (m.get("offers") or []) if o.get("title")],
        "signals": [humanize(str(s if isinstance(s, str) else s.get("type", "")).split(":")[0])
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
                 "title": kind_title(t.get("kind")),
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
                        "title": kind_title(trigger.get("kind")),
                        "payload": trigger.get("payload") or {},
                        "urgency": trigger.get("urgency")},
            "merchant_id": merchant.get("merchant_id"),
            "customer_id": (customer or {}).get("customer_id"),
            "recipient": ((customer or {}).get("identity") or {}).get("name")
                         or (customer or {}).get("name")
                         or (merchant.get("identity") or {}).get("owner_first_name") or "",
            "message": {
                "body": out.body,
                "cta": out.cta,
                "cta_title": CTA_TITLES.get(out.cta, humanize(out.cta)),
                "send_as": out.send_as,
                "send_as_title": SEND_AS_TITLES.get(out.send_as, humanize(out.send_as)),
                "suppression_key": out.suppression_key,
                "rationale": out.rationale,
                "levers": out.levers,
                "template": out.template_name,
                "template_title": humanize(str(out.template_name).replace("vera_", "").replace("_v1", "")),
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
                        "merchant": _merchant_row(c.merchant)["name"],
                        "customer_id": c.customer_id, "kind": c.trigger.get("kind"),
                        "title": kind_title(c.trigger.get("kind")),
                        "priority": round(c.priority, 3), "reasons": c.reasons}
                       for c in chosen],
            "skipped": [{**sk, "title": kind_title((store.trigger(sk.get("trigger_id")) or {}).get("kind"))}
                        for sk in skipped],
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
  --bg:#0d1117; --panel:#161b22; --raise:#1c2230; --line:#2a3240; --line2:#39424f;
  --ink:#e6edf3; --dim:#8b949e; --faint:#848d97;
  --acc:#58a6ff; --acc-soft:rgba(88,166,255,.14);
  --ok:#3fb950; --ok-soft:rgba(63,185,80,.15);
  --warn:#d29922; --bad:#f85149; --bad-soft:rgba(248,81,73,.16);
  --bubble:#1f3f2e;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --r:10px;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;
  -webkit-font-smoothing:antialiased}
button,input{font-family:inherit}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px;border-radius:4px}

/* ---------- header ---------- */
header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;
  padding:14px 22px;border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,#171d26,#12171f);position:sticky;top:0;z-index:20}
.brand{display:flex;align-items:baseline;gap:9px}
.brand b{font-size:16px;font-weight:650;letter-spacing:-.2px}
.brand span{color:var(--dim);font-size:13px}
.lede{color:var(--faint);font-size:12.5px;max-width:430px;line-height:1.5}
.spacer{flex:1}
.pill{font-size:11.5px;color:var(--dim);background:var(--panel);
  border:1px solid var(--line);border-radius:999px;padding:5px 13px;white-space:nowrap}

/* ---------- layout ---------- */
main{display:grid;grid-template-columns:288px minmax(0,1fr);height:calc(100vh - 61px)}
/* grid children default to min-width:auto and refuse to shrink below their
   content, which is what puts a horizontal scrollbar on the whole page */
#side,#work{min-width:0}
#side{border-right:1px solid var(--line);background:var(--panel);
  display:flex;flex-direction:column;min-height:0}
.sidehead{padding:13px 14px 10px;border-bottom:1px solid var(--line)}
.sidehead h3{margin:0 0 9px;font-size:11px;letter-spacing:.9px;text-transform:uppercase;
  color:var(--faint);font-weight:650}
#q{width:100%;background:var(--bg);border:1px solid var(--line);color:var(--ink);
  border-radius:8px;padding:8px 11px;font-size:13px;transition:border-color .15s}
#q:focus{border-color:var(--acc);outline:none}
#q::placeholder{color:var(--faint)}
#list{overflow-y:auto;flex:1;padding:6px}
.mrow{padding:10px 12px;border-radius:8px;cursor:pointer;margin-bottom:2px;
  border:1px solid transparent;transition:background .13s,border-color .13s}
.mrow:hover{background:var(--raise)}
.mrow.on{background:var(--acc-soft);border-color:rgba(88,166,255,.35)}
.mrow b{display:block;font-weight:600;font-size:13.5px;letter-spacing:-.1px}
.mrow .meta{color:var(--faint);font-size:11.5px;margin-top:2px}
.mrow .nums{color:var(--dim);font-size:11.5px;font-family:var(--mono);margin-top:3px}
#work{overflow-y:auto;padding:22px 26px 70px;min-width:0}

/* ---------- cards ---------- */
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
  padding:18px 20px;margin-bottom:16px}
.card>h2{font-size:11px;text-transform:uppercase;letter-spacing:.9px;
  color:var(--faint);margin:0 0 14px;font-weight:650}
.mname{font-size:20px;font-weight:650;letter-spacing:-.3px;margin:0}
.msub{color:var(--dim);font-size:13px;margin-top:3px}
.badge{display:inline-block;font-size:11px;padding:2.5px 9px;border-radius:999px;
  background:var(--raise);border:1px solid var(--line2);color:var(--dim);
  letter-spacing:.2px;vertical-align:2px}

/* stat tiles - no plot, so no hover layer */
.stats{display:flex;gap:26px;flex-wrap:wrap;margin-top:16px;
  padding-top:16px;border-top:1px solid var(--line)}
.stat b{display:block;font-size:21px;font-weight:650;letter-spacing:-.5px;
  font-variant-numeric:tabular-nums;line-height:1.25}
.stat span{color:var(--faint);font-size:11px;text-transform:uppercase;letter-spacing:.7px}
.note{color:var(--faint);font-size:12.5px;margin-top:12px}
.note b{color:var(--dim);font-weight:600}

/* ---------- triggers ---------- */
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{border:1px solid var(--line);background:var(--raise);border-radius:8px;
  padding:7px 13px;font-size:12.5px;cursor:pointer;color:var(--ink);
  transition:border-color .13s,background .13s,color .13s}
.chip:hover{border-color:var(--line2);background:#232b3a}
.chip.on{background:var(--acc);color:#06090f;border-color:var(--acc);font-weight:650}
.chip i{font-style:normal;opacity:.65;font-size:11px;margin-left:6px}

/* ---------- message bubble ---------- */
.thread{background:var(--bg);border:1px solid var(--line);border-radius:var(--r);
  padding:18px}
.to{color:var(--faint);font-size:11.5px;margin-bottom:10px;letter-spacing:.2px}
.bubble{background:var(--bubble);border:1px solid #2c5a41;border-radius:14px 14px 14px 4px;
  padding:14px 16px;max-width:640px;font-size:14.5px;line-height:1.72;
  white-space:pre-wrap;position:relative}
.num{border-bottom:1.5px solid var(--ok);cursor:pointer;padding:0 1px;font-weight:650;
  transition:background .12s}
.num:hover{background:var(--ok-soft)}
.num.bad{border-color:var(--bad);background:var(--bad-soft)}
.num.on{background:var(--ok);color:#06120a;border-radius:3px}
.hint{color:var(--faint);font-size:12px;margin-top:11px;display:flex;gap:7px;
  align-items:center;flex-wrap:wrap}
.dot{width:5px;height:5px;border-radius:50%;background:var(--faint);flex:none}

/* ---------- meta grid ---------- */
.kv{display:grid;grid-template-columns:150px minmax(0,1fr);gap:8px 18px;
  font-size:13px;margin-top:16px;padding-top:16px;border-top:1px solid var(--line)}
.kv dt{color:var(--faint);font-size:12px}
.kv dd{margin:0;word-break:break-word}
.kv dd code{font-family:var(--mono);font-size:12px;color:var(--dim);
  background:var(--raise);padding:1.5px 6px;border-radius:4px}

/* ---------- provenance ---------- */
#prov:empty{display:none}
.provcard{background:var(--raise);border:1px solid var(--line2);border-radius:9px;
  padding:14px 16px;margin-top:14px}
.provcard h4{margin:0 0 10px;font-size:13px;font-weight:650}
.provcard h4 em{font-style:normal;color:var(--ok);font-family:var(--mono)}

/* ---------- tables ---------- */
.scroll{overflow-x:auto;margin:0 -20px;padding:0 20px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--faint);font-weight:650;font-size:10.5px;
  text-transform:uppercase;letter-spacing:.7px;padding:7px 10px;
  border-bottom:1px solid var(--line2);white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr{transition:background .12s}
tbody tr:hover{background:var(--raise)}
tr.hl{background:var(--acc-soft)!important}
td code{font-family:var(--mono);font-size:11.5px;color:var(--dim)}
#facts{table-layout:fixed;min-width:760px}
#facts th:nth-child(1),#facts td:nth-child(1){width:23%}
#facts th:nth-child(2),#facts td:nth-child(2){width:20%}
#facts th:nth-child(3),#facts td:nth-child(3){width:17%}
#facts th:nth-child(4),#facts td:nth-child(4){width:92px}
#facts td:nth-child(5){overflow-wrap:anywhere}
.tag{font-size:10px;padding:2px 8px;border-radius:5px;font-weight:650;
  letter-spacing:.3px;white-space:nowrap;display:inline-block}
.tag.vis{background:var(--ok-soft);color:var(--ok)}
.tag.hid{background:rgba(139,148,158,.14);color:var(--faint)}
.tag.bad{background:var(--bad-soft);color:var(--bad)}

/* ---------- tabs ---------- */
.tabs{display:flex;gap:4px;margin-bottom:18px;border-bottom:1px solid var(--line)}
.tabs button{background:transparent;border:0;border-bottom:2px solid transparent;
  color:var(--dim);padding:9px 15px;font-size:13px;cursor:pointer;margin-bottom:-1px;
  transition:color .13s,border-color .13s;font-weight:500}
.tabs button:hover{color:var(--ink)}
.tabs button.on{color:var(--ink);border-bottom-color:var(--acc);font-weight:650}

/* ---------- reply ---------- */
.row{display:flex;gap:9px;align-items:center;flex-wrap:wrap}
.go{background:var(--acc);color:#06090f;border:0;border-radius:8px;
  padding:9px 17px;font-weight:650;cursor:pointer;font-size:13px;transition:filter .13s}
.go:hover{filter:brightness(1.12)}
.ghost{background:var(--raise);color:var(--dim);border:1px solid var(--line);
  border-radius:7px;padding:7px 13px;cursor:pointer;font-size:12.5px;
  transition:border-color .13s,color .13s}
.ghost:hover{border-color:var(--line2);color:var(--ink)}
#msg{flex:1;min-width:220px;background:var(--bg);border:1px solid var(--line);
  color:var(--ink);border-radius:8px;padding:10px 12px;font-size:13.5px}
#msg:focus{border-color:var(--acc);outline:none}
.turn{margin:14px 0;max-width:600px}
.turn .who{color:var(--faint);font-size:10.5px;text-transform:uppercase;
  letter-spacing:.7px;margin-bottom:5px;font-weight:650}
.turn .said{border-radius:12px;padding:11px 14px;font-size:13.5px;line-height:1.65}
.turn.them .said{background:var(--raise);border:1px solid var(--line);
  border-radius:12px 12px 12px 4px}
.turn.us{margin-left:auto}
.turn.us .who{text-align:right}
.turn.us .said{background:var(--bubble);border:1px solid #2c5a41;
  border-radius:12px 12px 4px 12px}
.turn .why{color:var(--faint);font-size:12px;margin-top:6px;font-style:italic}

.empty{color:var(--faint);padding:56px 24px;text-align:center;line-height:1.8}
.empty b{color:var(--dim)}
.skel{height:13px;background:var(--raise);border-radius:5px;margin:9px 14px;
  animation:pulse 1.3s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.45}50%{opacity:.85}}

@media (max-width:860px){
  main{grid-template-columns:minmax(0,1fr);height:auto}
  #side{border-right:0;border-bottom:1px solid var(--line);max-height:300px}
  #work{padding:18px 16px 50px}
  .card{padding:16px 15px}
  .scroll{margin:0 -15px;padding:0 15px}
  .kv{grid-template-columns:1fr;gap:2px 0}
  .kv dt{margin-top:9px}
  .lede{display:none}
  .stats{gap:18px}
}
</style></head><body>

<header>
  <div class="brand"><b>Vera</b><span>message engine</span></div>
  <div class="lede">Pick a merchant and a moment. Every number in the message it writes
    traces back to a fact that was pushed to it.</div>
  <span class="spacer"></span>
  <span class="pill" id="counts">loading</span>
</header>

<main>
  <div id="side">
    <div class="sidehead">
      <h3>Merchants</h3>
      <input id="q" placeholder="Filter by name, area or trade…" oninput="paint()" autocomplete="off">
    </div>
    <div id="list"><div class="skel"></div><div class="skel"></div><div class="skel"></div></div>
  </div>
  <div id="work"><div class="empty">Loading the dataset…</div></div>
</main>

<script>
const $ = s => document.querySelector(s);
// The console always runs on the shipped seed data. It deliberately does not
// render the judged store: that store is empty except during an actual harness
// run, so a live view would show a reviewer a blank page nearly every time.
// The server still accepts ?source=live for inspecting a run over curl.
const SRC='demo';
let MERCHANTS=[], SEL=null, DETAIL=null, OUT=null, TAB='message', LOG=[];
let TICK=null, TICKING=false;

const esc = s => String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt = n => n==null ? '—' : (typeof n==='number' ? n.toLocaleString('en-IN') : n);
const api = (p,o) => fetch(p,o).then(r=>r.json());

async function load(){
  let d;
  try { d = await api('/console/api/state?source='+SRC); }
  catch(e){ return fail('Could not reach the console API.'); }
  MERCHANTS = d.merchants||[];
  const c = d.counts||{};
  $('#counts').textContent =
    `${c.merchant||0} merchants · ${c.trigger||0} moments · ${c.customer||0} customers · ${c.category||0} trades`;
  if(!MERCHANTS.length) return fail('No merchant data loaded.');
  paint();
  pick(MERCHANTS[0].merchant_id);
}

function fail(msg){
  $('#list').innerHTML = '<div class="empty">'+esc(msg)+'</div>';
  $('#work').innerHTML = '<div class="empty">The engine itself is unaffected — the scored '
    + 'endpoints live at <b>/v1/healthz</b> and <b>/docs</b>.</div>';
}

function paint(){
  const q = ($('#q')?.value||'').toLowerCase().trim();
  const rows = MERCHANTS.filter(m => !q ||
    [m.name,m.locality,m.city,m.category_title,m.owner].join(' ').toLowerCase().includes(q));
  $('#list').innerHTML = rows.length ? rows.map(m=>{
    const p=m.performance||{};
    return `<div class="mrow ${SEL===m.merchant_id?'on':''}" onclick="pick('${esc(m.merchant_id)}')">
      <b>${esc(m.name)}</b>
      <div class="meta">${esc(m.category_title||m.category)} · ${esc(m.locality)}</div>
      <div class="nums">${fmt(p.views)} views · ${fmt(p.calls)} calls</div></div>`;
  }).join('') : '<div class="empty">Nothing matches that.</div>';
}

async function pick(id){
  SEL=id; OUT=null; LOG=[]; paint();
  DETAIL = await api(`/console/api/merchant/${encodeURIComponent(id)}?source=${SRC}`);
  const t = DETAIL.triggers||[];
  if(t.length) await run(t[0].id); else draw();
}

async function run(tid){
  OUT = await api('/console/api/compose', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({trigger_id:tid, source:SRC})});
  TAB = hashTab(); draw();
}

function markup(body, prov){
  // One pass over the raw text. Replacing token by token would re-scan markup
  // already inserted and corrupt attributes like data-i="14".
  const idx = new Map();
  (prov||[]).forEach((p,i)=>{
    const k = String(p.token).replace(/[.,]+$/,'').replace(/,/g,'');
    if(!idx.has(k)) idx.set(k, {p, i});
  });
  return esc(body).replace(/\d[\d,.]*/g, tok => {
    const tail = (tok.match(/[.,]+$/)||[''])[0];
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
    ? `<div class="provcard">
        <h4>Where <em>${esc(p.token)}</em> comes from</h4>
        <dl class="kv" style="margin:0;padding:0;border:0">
          <dt>Means</dt><dd>${esc(p.label)}</dd>
          <dt>Pushed in</dt><dd>${esc(p.source||'—')}</dd>
          <dt>Fact key</dt><dd><code>${esc(p.key)}</code></dd>
          <dt>Judge sees it</dt><dd>${p.judge_visible
            ? '<span class="tag vis">Yes — it is in the scoring payload</span>'
            : '<span class="tag hid">No — supporting colour only</span>'}</dd>
          ${p.alternatives&&p.alternatives.length
            ? `<dt>Also renders as</dt><dd style="color:var(--faint)">${
                p.alternatives.map(a=>esc(a.label)).join(', ')}</dd>`:''}
        </dl></div>`
    : `<div class="provcard"><h4 style="color:var(--bad)">No fact behind ${esc(p.token)}</h4>
        <div class="note" style="margin:0">Nothing in the pushed context authorises this number.
        <b>validate.py</b> refuses to emit a message in this state.</div></div>`;
  document.querySelectorAll('#facts tr').forEach(tr=>
    tr.classList.toggle('hl', tr.dataset.key===p.key));
}

const TABS = {message:'Message', grounding:'Grounding', decision:'Decision', reply:'Reply'};
// the pane lives in the hash so a tab is linkable and reloads where you left it
const tab = t => { TAB=t; history.replaceState(null,'','#'+t); draw(); };
const hashTab = () => TABS[location.hash.slice(1)] ? location.hash.slice(1) : 'message';
addEventListener('hashchange', ()=>{ const t=hashTab(); if(t!==TAB){ TAB=t; draw(); } });

function draw(){
  if(!DETAIL){ $('#work').innerHTML='<div class="empty">Loading…</div>'; return; }
  const m = DETAIL.merchant, p = m.performance||{}, trs = DETAIL.triggers||[];

  const head = `<div class="card">
    <h2>Merchant</h2>
    <div style="display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap">
      <div style="flex:1;min-width:200px">
        <p class="mname">${esc(m.name)}</p>
        <div class="msub">${esc(m.owner)} · ${esc(m.locality)}${m.city?', '+esc(m.city):''}</div>
      </div>
      <span class="badge">${esc(m.category_title||m.category)}</span>
    </div>
    <div class="stats">
      <div class="stat"><b>${fmt(p.views)}</b><span>Views</span></div>
      <div class="stat"><b>${fmt(p.calls)}</b><span>Calls</span></div>
      <div class="stat"><b>${p.ctr!=null?(p.ctr*100).toFixed(1)+'%':'—'}</b><span>Click-through</span></div>
      <div class="stat"><b>${fmt(p.directions)}</b><span>Directions</span></div>
      ${p.window_days?`<div class="stat"><b>${p.window_days}d</b><span>Window</span></div>`:''}
    </div>
    ${m.offers&&m.offers.length?`<div class="note"><b>Live offers</b> — ${m.offers.map(esc).join(' · ')}</div>`
      :'<div class="note">No live offers on the listing.</div>'}
    ${m.signals&&m.signals.length?`<div class="note"><b>Signals</b> — ${m.signals.map(esc).join(' · ')}</div>`:''}
  </div>`;

  const trigs = `<div class="card">
    <h2>Why message now — ${trs.length} live ${trs.length===1?'moment':'moments'}</h2>
    <div class="chips">${trs.length ? trs.map(t=>
      `<button class="chip ${OUT&&OUT.trigger.id===t.id?'on':''}" onclick="run('${esc(t.id)}')"
        >${esc(t.title)}${t.customer_id?'<i>to a customer</i>':''}</button>`).join('')
      : '<span class="note" style="margin:0">Nothing live for this merchant.</span>'}</div>
  </div>`;

  let main = `<div class="card"><div class="empty">Pick a moment above.</div></div>`;
  if(OUT && OUT.message){
    const msg = OUT.message, prov = OUT.provenance||[];
    const ung = prov.filter(x=>!x.grounded).length;
    const tabs = `<div class="tabs">${Object.entries(TABS).map(([k,v])=>
      `<button class="${TAB===k?'on':''}" onclick="tab('${k}')">${v}</button>`).join('')}</div>`;

    let pane='';
    if(TAB==='message'){
      pane = `<div class="thread">
          <div class="to">To ${esc(OUT.recipient||'the merchant')} · sent as ${esc(msg.send_as_title)}</div>
          <div class="bubble">${markup(msg.body, prov)}</div>
        </div>
        <div class="hint"><span>Click any underlined number to see the fact behind it.</span>
          <span class="dot"></span>
          <span>${ung?`<b style="color:var(--bad)">${ung} ungrounded</b>`:'All grounded'}</span>
          <span class="dot"></span>
          <span>${OUT.visible_anchors} anchor${OUT.visible_anchors===1?'':'s'} the judge can see</span></div>
        <div id="prov"></div>
        <dl class="kv">
          <dt>Reply options</dt><dd>${esc(msg.cta_title)}</dd>
          <dt>Sent as</dt><dd>${esc(msg.send_as_title)}</dd>
          <dt>Strategy</dt><dd>${esc(msg.template_title||'—')}</dd>
          <dt>Persuasion levers</dt><dd>${(msg.levers||[]).map(esc).join(', ')||'—'}</dd>
          <dt>Won't repeat under</dt><dd><code>${esc(msg.suppression_key)}</code></dd>
          <dt>Rationale</dt><dd>${esc(msg.rationale)}</dd>
        </dl>
        ${msg.warnings&&msg.warnings.length
          ? `<div class="note" style="color:var(--warn)"><b>Guardrails fired</b> — ${msg.warnings.map(esc).join('; ')}</div>`:''}`;
    } else if(TAB==='grounding'){
      pane = `<div class="scroll"><table id="facts"><thead><tr>
        <th>Fact</th><th>Value</th><th>Pushed in</th><th>Judge</th><th>Key</th></tr></thead><tbody>
        ${(OUT.facts||[]).map(f=>`<tr data-key="${esc(f.key)}">
          <td>${esc(f.label)}</td>
          <td style="font-family:var(--mono);color:var(--ink)">${esc(f.text)}</td>
          <td style="color:var(--faint)">${esc(f.source||'—')}</td>
          <td>${f.judge_visible?'<span class="tag vis">Sees it</span>':'<span class="tag hid">Blind</span>'}</td>
          <td><code>${esc(f.key)}</code></td></tr>`).join('')}</tbody></table></div>
        <div class="note"><b>${(OUT.facts||[]).length} facts</b> registered for this message.
          A number that is not in this table cannot appear in it.</div>`;
    } else if(TAB==='decision'){
      pane = `<div class="note" style="margin:0 0 14px">Ranks every live moment across all
          merchants and keeps at most one each.
          <button class="ghost" style="margin-left:8px" onclick="tick(true)">Run again</button></div>
        <div id="dec">${TICK ? tickTable(TICK) : '<div class="note">Ranking…</div>'}</div>`;
      if(!TICK && !TICKING) tick();
    } else {
      pane = `<div class="row" style="margin-bottom:14px">
          <button class="ghost" onclick="say('Ok lets do it. Whats next?')">Commit</button>
          <button class="ghost" onclick="say('Thank you for contacting us! Our team will respond shortly.')">Auto-reply</button>
          <button class="ghost" onclick="say('stop messaging me')">Opt out</button>
          <button class="ghost" onclick="say('can you help me file my taxes')">Off topic</button>
          <button class="ghost" onclick="resetConv()">Reset</button>
        </div>
        <div class="row"><input id="msg" placeholder="Reply as the merchant…"
            onkeydown="if(event.key==='Enter')say()"><button class="go" onclick="say()">Send</button></div>
        <div id="log">${LOG.length ? LOG.map(t=>`<div class="turn ${t.who==='Merchant'?'us':'them'}">
            <div class="who">${esc(t.who)}${t.action?' · '+esc(t.action):''}</div>
            <div class="said">${esc(t.text)}</div>
            ${t.why?`<div class="why">${esc(t.why)}</div>`:''}</div>`).join('')
          : '<div class="note">This drives the real reply state machine. Send the same auto-reply '
            + 'four times and watch it flag the merchant, wait, then end the conversation.</div>'}</div>`;
    }
    main = `<div class="card">
      <h2>${esc(OUT.trigger.title)}</h2>${tabs}${pane}</div>`;
  }
  $('#work').innerHTML = head + trigs + main;
  if(TAB==='reply') $('#msg')?.focus();
}

async function tick(force){
  if(TICKING) return;
  TICKING = true;
  if(force){ TICK=null; draw(); }
  try {
    TICK = await api('/console/api/tick', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({source:SRC})});
  } finally { TICKING = false; }
  if(TAB==='decision') draw();
}

function tickTable(d){
  return `<div class="note" style="margin:0 0 14px">
      <b>${d.considered} moments considered</b> → ${d.chosen.length} sent, ${d.skipped.length} held back.
      Restraint is the point: one message per merchant, ranked.</div>
    <div class="scroll"><table><thead><tr><th></th><th>Merchant</th><th>Moment</th>
      <th>Score</th><th>Reasoning</th></tr></thead><tbody>
    ${d.chosen.map(c=>`<tr><td><span class="tag vis">Send</span></td>
      <td>${esc(c.merchant||c.merchant_id)}</td><td>${esc(c.title)}</td>
      <td style="font-family:var(--mono)">${c.priority}</td>
      <td style="color:var(--dim)">${(c.reasons||[]).map(esc).join('; ')}</td></tr>`).join('')}
    ${d.skipped.map(s=>`<tr><td><span class="tag bad">Hold</span></td>
      <td colspan="3" style="color:var(--dim)">${esc(s.title||'')}</td>
      <td style="color:var(--faint)">${esc(s.reason)}</td></tr>`).join('')}
    </tbody></table></div>`;
}

async function say(preset){
  const box=$('#msg'); const text = preset || (box?box.value:''); if(!text.trim()) return;
  if(box && !preset) box.value='';
  LOG.push({who:'Merchant', text});
  const d = await api('/console/api/reply', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({conversation_id:'console_'+SEL, merchant_id:SEL,
      message:text, from_role:'merchant',
      turn_number: LOG.filter(t=>t.who==='Merchant').length})});
  const ACTION = {send:'replied', wait:'held back', end:'ended the conversation'};
  LOG.push({who:'Vera', action:ACTION[d.action]||d.action, why:d.rationale||'',
            text: d.body || d.message || '(nothing sent)'});
  draw();
}

async function resetConv(){
  await api('/console/api/reset', {method:'POST'});
  LOG=[]; draw();
}

load();
</script></body></html>
"""
