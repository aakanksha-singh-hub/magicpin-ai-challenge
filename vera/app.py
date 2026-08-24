"""HTTP surface for the judge harness.

Design rules:
  - never raise: every path returns valid JSON, because malformed responses are
    a scored penalty
  - never block: composition is pure computation, so p99 is single-digit ms
    against a 10s budget
"""
from __future__ import annotations

import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import decide
from .compose import compose
from .converse import respond
from .store import SCOPES, Store, parse_iso, utcnow_iso

app = FastAPI(title="Vera message engine", docs_url=None, redoc_url=None)
STORE = Store()
START = time.time()

METADATA = {
    "team_name": os.environ.get("VERA_TEAM_NAME", "Aakanksha Singh"),
    "team_members": [m.strip() for m in
                     os.environ.get("VERA_TEAM_MEMBERS", "Aakanksha Singh").split(",") if m.strip()],
    "model": "deterministic-composer/1.0 (no LLM in the request path)",
    "approach": ("Deterministic fact-grounded composer. Every number in every message is "
                 "registered from a pushed context with provenance, so fabrication is "
                 "structurally impossible; messages lead with anchors visible in the judge's "
                 "own scoring view. ~30 per-trigger-kind strategies, per-category voice, "
                 "consent gating on customer outreach, and a reply FSM for auto-reply, "
                 "intent-transition and hostile handling."),
    "contact_email": os.environ.get("VERA_CONTACT", "aakanksha.singh0205@gmail.com"),
    "version": "1.0.0",
    "submitted_at": os.environ.get("VERA_SUBMITTED_AT", "2026-08-24T00:00:00Z"),
    "endpoints": ["/v1/context", "/v1/tick", "/v1/reply", "/v1/healthz", "/v1/metadata"],
}


def _keepalive_loop(url: str, every: int) -> None:
    """Render's free tier spins a service down after ~15 min of no inbound
    traffic, which would drop every stored context and fail the healthz probe.
    A self-ping counts as inbound traffic and keeps the instance warm.
    Set VERA_KEEPALIVE_URL to the public base URL to enable.
    """
    target = url.rstrip("/") + "/v1/healthz"
    while True:
        time.sleep(every)
        try:
            urllib.request.urlopen(target, timeout=10).read()
        except Exception as exc:
            print(f"[vera] keepalive ping failed: {exc!r}")


@app.on_event("startup")
async def _startup() -> None:
    n = STORE.load_snapshot()
    if n:
        print(f"[vera] recovered {n} contexts from snapshot")
    url = os.environ.get("VERA_KEEPALIVE_URL", "").strip()
    if url:
        every = int(os.environ.get("VERA_KEEPALIVE_SECONDS", "600"))
        threading.Thread(target=_keepalive_loop, args=(url, every), daemon=True).start()
        print(f"[vera] keepalive every {every}s -> {url}")


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ probes

@app.get("/v1/healthz")
@app.get("/healthz")
async def healthz() -> JSONResponse:
    counts = STORE.counts()
    return JSONResponse({
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": {s: counts.get(s, 0) for s in SCOPES},
        "conversations": len(STORE.conversations),
        "stats": STORE.stats,
    })


@app.get("/v1/metadata")
@app.get("/metadata")
async def metadata() -> JSONResponse:
    return JSONResponse(METADATA)


# ------------------------------------------------------------------ context

@app.post("/v1/context")
async def push_context(request: Request) -> JSONResponse:
    body = await _json(request)
    scope = str(body.get("scope") or "").strip()
    context_id = str(body.get("context_id") or "").strip()
    payload = body.get("payload")

    if scope not in SCOPES:
        return JSONResponse({"accepted": False, "reason": "invalid_scope",
                             "details": f"scope must be one of {list(SCOPES)}"},
                            status_code=400)
    if not context_id:
        return JSONResponse({"accepted": False, "reason": "missing_context_id",
                             "details": "context_id is required"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"accepted": False, "reason": "invalid_payload",
                             "details": "payload must be an object"}, status_code=400)

    try:
        version = int(body.get("version", 1))
    except (TypeError, ValueError):
        version = 1

    result = STORE.put_context(scope, context_id, version, payload,
                               str(body.get("delivered_at") or ""))
    if not result.get("accepted"):
        return JSONResponse(result, status_code=409)
    return JSONResponse(result)


# ------------------------------------------------------------------ tick

@app.post("/v1/tick")
async def tick(request: Request) -> JSONResponse:
    body = await _json(request)
    STORE.stats["ticks"] += 1
    now = parse_iso(body.get("now")) or datetime.now(timezone.utc)
    available = body.get("available_triggers") or []
    if not isinstance(available, list):
        available = []
    available = [str(t) for t in available if t]

    actions: list[dict[str, Any]] = []
    try:
        chosen, _skipped = decide.select(STORE, available, now)
        for cand in chosen:
            out = compose(cand.category, cand.merchant, cand.trigger, cand.customer,
                          now=now, priority_reasons=cand.reasons)
            if not out.body:
                continue
            conv_id = decide.conversation_id_for(cand)
            conv = STORE.ensure_conversation(
                conv_id, merchant_id=cand.merchant_id, customer_id=cand.customer_id,
                trigger_id=cand.trigger_id, send_as=out.send_as)
            if conv.has_sent(out.body):
                continue
            conv.offered = out.offer_label
            conv.record_outbound(out.body)

            party = STORE.party(cand.merchant_id)
            party.sent_trigger_ids.add(cand.trigger_id)
            if out.suppression_key:
                party.fired_keys[out.suppression_key] = utcnow_iso()
            party.sends_total += 1
            party.last_send_ts = now.timestamp()
            party.quiet_until = now.timestamp() + decide.MERCHANT_QUIET_SECONDS
            STORE.stats["actions_sent"] += 1

            actions.append({
                "conversation_id": conv_id,
                "merchant_id": cand.merchant_id,
                "customer_id": cand.customer_id,
                "send_as": out.send_as,
                "trigger_id": cand.trigger_id,
                "template_name": out.template_name,
                "template_params": out.template_params,
                "body": out.body,
                "cta": out.cta,
                "suppression_key": out.suppression_key,
                "rationale": out.rationale,
            })
    except Exception as exc:  # never fail a tick
        print(f"[vera] tick error: {exc!r}")
        return JSONResponse({"actions": actions})

    return JSONResponse({"actions": actions})


# ------------------------------------------------------------------ reply

@app.post("/v1/reply")
async def reply(request: Request) -> JSONResponse:
    body = await _json(request)
    STORE.stats["replies"] += 1
    conversation_id = str(body.get("conversation_id") or "conv_unknown")
    merchant_id = body.get("merchant_id")
    customer_id = body.get("customer_id")
    message = str(body.get("message") or "")
    from_role = str(body.get("from_role") or "merchant")
    try:
        turn_number = int(body.get("turn_number", 2))
    except (TypeError, ValueError):
        turn_number = 2

    try:
        result = respond(STORE, conversation_id,
                         str(merchant_id) if merchant_id else None,
                         str(customer_id) if customer_id else None,
                         message, from_role, turn_number)
    except Exception as exc:
        print(f"[vera] reply error: {exc!r}")
        result = {"action": "wait", "wait_seconds": 3600,
                  "rationale": "Transient internal issue; backing off rather than replying badly."}
    return JSONResponse(result)


# ------------------------------------------------------------------ teardown

@app.post("/v1/teardown")
async def teardown() -> JSONResponse:
    STORE.teardown()
    return JSONResponse({"ok": True, "wiped_at": utcnow_iso()})


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse({"service": "vera-message-engine", "endpoints":
                         ["/v1/healthz", "/v1/metadata", "/v1/context",
                          "/v1/tick", "/v1/reply"]})
