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

TAGS = [
    {"name": "probes", "description": "Liveness and identity. Polled by the judge every 60s; "
                                      "three consecutive healthz failures disqualify the bot."},
    {"name": "context", "description": "Incremental context push across the four layers. "
                                       "Idempotent on (scope, context_id, version)."},
    {"name": "conversation", "description": "Proactive sends and inbound replies."},
    {"name": "admin", "description": "Optional teardown."},
]

DESCRIPTION = """
Deterministic message engine behind **Vera**, magicpin's merchant AI assistant.

`compose(category, merchant, trigger, customer?)` returns the next WhatsApp message,
its CTA, the sending identity, a suppression key and a rationale.

**No LLM in the request path.** Every number in every message is registered from a pushed
context with provenance before it can be used, so fabrication is structurally impossible
rather than prompt-discouraged. Typical `/v1/tick` latency is single-digit milliseconds.

Request bodies are parsed leniently on purpose: unknown fields are ignored and malformed
input returns a documented JSON error rather than a framework 422, because the judge
scores malformed responses as a penalty.
"""

app = FastAPI(
    title="Vera message engine",
    description=DESCRIPTION,
    version="1.0.0",
    openapi_tags=TAGS,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    contact={"name": "Aakanksha Singh", "email": "aakanksha.singh0205@gmail.com"},
)

# ---------------------------------------------------------------- schemas
# Declared for the OpenAPI document only. Runtime parsing stays hand-rolled and
# lenient so that a surprising payload degrades gracefully instead of 422-ing.

_CONTEXT_BODY = {
    "required": True,
    "content": {"application/json": {"schema": {
        "type": "object",
        "required": ["scope", "context_id", "version", "payload"],
        "properties": {
            "scope": {"type": "string", "enum": list(SCOPES),
                      "description": "Which context layer this payload belongs to."},
            "context_id": {"type": "string", "example": "m_001_drmeera_dentist_delhi"},
            "version": {"type": "integer", "minimum": 1, "example": 3,
                        "description": "Higher replaces atomically; equal is a no-op; "
                                       "lower is rejected with 409."},
            "payload": {"type": "object", "description": "The full context object."},
            "delivered_at": {"type": "string", "format": "date-time"},
        },
    }, "example": {"scope": "merchant", "context_id": "m_001_drmeera_dentist_delhi",
                   "version": 3, "payload": {"identity": {}, "performance": {}, "offers": []},
                   "delivered_at": "2026-04-29T10:00:00Z"}}},
}

_TICK_BODY = {
    "required": True,
    "content": {"application/json": {"schema": {
        "type": "object",
        "properties": {
            "now": {"type": "string", "format": "date-time",
                    "description": "Current simulated time."},
            "available_triggers": {"type": "array", "items": {"type": "string"},
                                   "description": "Trigger ids the judge considers live now. "
                                                  "Treated as authoritative over expires_at."},
        },
    }, "example": {"now": "2026-04-26T10:30:00Z",
                   "available_triggers": ["trg_001_research_digest_dentists"]}}},
}

_REPLY_BODY = {
    "required": True,
    "content": {"application/json": {"schema": {
        "type": "object",
        "required": ["conversation_id", "message"],
        "properties": {
            "conversation_id": {"type": "string", "example": "conv_m_001_research_digest"},
            "merchant_id": {"type": "string", "nullable": True},
            "customer_id": {"type": "string", "nullable": True},
            "from_role": {"type": "string", "enum": ["merchant", "customer"]},
            "message": {"type": "string", "example": "Ok lets do it. Whats next?"},
            "received_at": {"type": "string", "format": "date-time"},
            "turn_number": {"type": "integer", "example": 2},
        },
    }}},
}


def _ex(example: dict, description: str = "Success") -> dict:
    return {200: {"description": description,
                  "content": {"application/json": {"example": example}}}}
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

@app.get("/v1/healthz", tags=["probes"], summary="Liveness probe",
         responses=_ex({"status": "ok", "uptime_seconds": 3600,
                        "contexts_loaded": {"category": 5, "merchant": 50,
                                            "customer": 200, "trigger": 100}}))
@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    counts = STORE.counts()
    return JSONResponse({
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": {s: counts.get(s, 0) for s in SCOPES},
        "conversations": len(STORE.conversations),
        "stats": STORE.stats,
    })


@app.get("/v1/metadata", tags=["probes"], summary="Bot identity and approach",
         responses=_ex(METADATA))
@app.get("/metadata", include_in_schema=False)
async def metadata() -> JSONResponse:
    return JSONResponse(METADATA)


# ------------------------------------------------------------------ context

@app.post("/v1/context", tags=["context"], summary="Push or update a context",
          openapi_extra={"requestBody": _CONTEXT_BODY},
          responses={200: {"description": "Stored, or an accepted no-op for an identical version",
                           "content": {"application/json": {"example": {
                               "accepted": True, "ack_id": "ack_merchant_m_001_v3",
                               "stored_at": "2026-04-29T10:00:00.123Z"}}}},
                     400: {"description": "Malformed: unknown scope, missing id, bad payload",
                           "content": {"application/json": {"example": {
                               "accepted": False, "reason": "invalid_scope", "details": "..."}}}},
                     409: {"description": "A higher version is already stored",
                           "content": {"application/json": {"example": {
                               "accepted": False, "reason": "stale_version",
                               "current_version": 5}}}}})
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

@app.post("/v1/tick", tags=["conversation"],
          summary="Wake-up; the bot decides what (if anything) to send",
          description="Returns at most one action per merchant per tick, capped at 20. "
                      "An empty list is a valid and deliberate answer -- restraint is "
                      "rewarded and spam is penalised.",
          openapi_extra={"requestBody": _TICK_BODY},
          responses=_ex({"actions": [{
              "conversation_id": "conv_m_001_drmeera_research_digest",
              "merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": None,
              "send_as": "vera", "trigger_id": "trg_001_research_digest_dentists",
              "template_name": "vera_research_digest_v1",
              "template_params": ["Meera", "...", "..."],
              "body": "Dr. Meera - one item from this week's clinical round-up ...",
              "cta": "binary_yes_no", "suppression_key": "research:dentists:2026-W17",
              "rationale": "research_digest trigger (urgency 2); anchored on the "
                           "merchant's own views/calls ..."}]}))
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

@app.post("/v1/reply", tags=["conversation"], summary="Inbound reply from merchant or customer",
          description="Returns exactly one of `send`, `wait` or `end`. Auto-reply detection is "
                      "keyed on the merchant rather than the conversation, because the same "
                      "canned text arrives across different conversation ids.",
          openapi_extra={"requestBody": _REPLY_BODY},
          responses=_ex({"action": "send",
                         "body": "Done - I'm drafting it now ...",
                         "cta": "binary_confirm_cancel",
                         "rationale": "Explicit go-ahead detected; switching from proposal to "
                                      "execution with no further qualifying questions."}))
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

@app.post("/v1/teardown", tags=["admin"], summary="Wipe all stored context and conversations",
          responses=_ex({"ok": True, "wiped_at": "2026-04-29T11:00:00.000Z"}))
async def teardown() -> JSONResponse:
    STORE.teardown()
    return JSONResponse({"ok": True, "wiped_at": utcnow_iso()})


@app.get("/", tags=["probes"], summary="Service index")
async def root() -> JSONResponse:
    return JSONResponse({
        "service": "vera-message-engine",
        "version": METADATA["version"],
        "docs": "/docs",
        "openapi": "/openapi.json",
        "endpoints": ["/v1/context", "/v1/tick", "/v1/reply",
                      "/v1/healthz", "/v1/metadata"],
    })
