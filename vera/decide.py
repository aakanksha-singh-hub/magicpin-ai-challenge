"""Decision layer: which signal deserves this moment, for whom, right now.

Restraint is explicitly rewarded by the harness ("Can my bot refuse to send when
nothing's worth saying? Yes -- restraint is rewarded; spam is penalised"), so
this module is as much about *not* sending as sending.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .store import Store, parse_iso

MAX_ACTIONS_PER_TICK = 20
# The harness sends real wall-clock time in `now` (judge_simulator.py uses
# datetime.utcnow()), so ticks can arrive seconds apart rather than the 5
# simulated minutes the brief describes. A long quiet window would therefore
# silence a merchant for the whole test. Keep it short: the real protection is
# one-action-per-merchant-per-tick plus per-trigger suppression.
MERCHANT_QUIET_SECONDS = 60.0
MAX_SENDS_PER_MERCHANT = 3

# trigger kind -> the consent scope a customer must plausibly have granted
CONSENT_SCOPE = {
    "recall_due": "recall_reminders",
    "appointment_tomorrow": "appointment_reminders",
    "chronic_refill_due": "refill_reminders",
    "customer_lapsed_soft": "promotional_offers",
    "customer_lapsed_hard": "promotional_offers",
    "winback_eligible": "promotional_offers",
    "wedding_package_followup": "promotional_offers",
    "trial_followup": "kids_program_updates",
}

# kinds whose whole point is a merchant-state signal; matching signal = bonus
SIGNAL_AFFINITY = {
    "perf_dip": ("perf_dip", "perf_dip_severe", "seasonal_dip"),
    "seasonal_perf_dip": ("seasonal_dip", "perf_dip"),
    "perf_spike": ("growing_views_7d", "high_engagement", "above_peer"),
    "renewal_due": ("renewal_due_soon", "trial_ending_soon"),
    "winback_eligible": ("winback_eligible", "perf_dip_post_expiry"),
    "dormant_with_vera": ("dormant_with_vera",),
    "gbp_unverified": ("unverified_gbp",),
    "review_theme_emerged": ("review",),
    "curious_ask_due": ("high_engagement", "engaged_in_last"),
    "milestone_reached": ("high_volume", "stable_growth"),
    "active_planning_intent": ("active_planning", "engaged_in_last"),
}


@dataclass
class Candidate:
    trigger: dict
    merchant: dict
    category: dict
    customer: dict | None
    priority: float
    reasons: list[str]

    @property
    def trigger_id(self) -> str:
        return str(self.trigger.get("id") or "")

    @property
    def merchant_id(self) -> str:
        return str(self.merchant.get("merchant_id") or "")

    @property
    def customer_id(self) -> str | None:
        cid = (self.customer or {}).get("customer_id")
        return str(cid) if cid else None


# ------------------------------------------------------------------ consent

def consent_block(customer: dict | None, kind: str) -> str:
    """Return a blocking reason, or '' if outreach is permitted."""
    if not customer:
        return ""
    prefs = customer.get("preferences") or {}
    consent = customer.get("consent") or {}
    scope = consent.get("scope") or []
    ident = customer.get("identity") or {}

    if prefs.get("reminder_opt_in") is False:
        return "customer has not opted in to reminders"
    if not consent.get("opted_in_at"):
        return "no recorded opt-in timestamp"
    if not scope:
        return "empty consent scope"
    if str(prefs.get("channel") or "").lower() in ("none_recorded", "none", ""):
        return "no recorded contact channel"
    if not ident.get("phone_redacted"):
        return "no contact number on file"
    if str(customer.get("state") or "").lower() == "churned":
        return "customer state is churned"
    return ""


def consent_match(customer: dict | None, kind: str) -> bool:
    if not customer:
        return True
    scope = (customer.get("consent") or {}).get("scope") or []
    want = CONSENT_SCOPE.get(kind)
    return not want or want in scope


# ------------------------------------------------------------------ scoring

def payload_richness(trigger: dict) -> int:
    """How much verifiable material the payload actually carries.

    The generated half of the dataset ships `{"placeholder": true}` payloads;
    those still deserve a message, just a differently-sourced one.
    """
    payload = trigger.get("payload") or {}
    if payload.get("placeholder"):
        return 0
    return sum(1 for k, v in payload.items()
               if v not in (None, "", [], {}) and k != "category")


def signal_affinity(trigger: dict, merchant: dict) -> float:
    kind = str(trigger.get("kind") or "")
    wants = SIGNAL_AFFINITY.get(kind)
    if not wants:
        return 0.0
    signals = " ".join(merchant.get("signals") or []).lower()
    return 2.0 if any(w in signals for w in wants) else 0.0


def score(trigger: dict, merchant: dict, customer: dict | None,
          now: datetime) -> tuple[float, list[str]]:
    reasons: list[str] = []
    kind = str(trigger.get("kind") or "")

    urgency = trigger.get("urgency")
    try:
        urgency = float(urgency)
    except (TypeError, ValueError):
        urgency = 2.0
    total = urgency * 2.0
    reasons.append(f"urgency {urgency:g}")

    rich = payload_richness(trigger)
    total += min(rich, 5) * 0.8
    if rich == 0:
        reasons.append("payload is a placeholder; grounding on merchant metrics instead")
    else:
        reasons.append(f"{rich} payload facts")

    aff = signal_affinity(trigger, merchant)
    if aff:
        total += aff
        reasons.append("merchant signals corroborate this trigger")

    # expiry pressure
    exp = parse_iso(trigger.get("expires_at"))
    if exp:
        hours = (exp - now).total_seconds() / 3600.0
        if 0 < hours < 48:
            total += 2.0
            reasons.append("expires within 48h")

    # merchant state modifiers
    signals = " ".join(merchant.get("signals") or []).lower()
    if "dormant" in signals and kind in ("curious_ask_due", "research_digest", "cde_opportunity"):
        total -= 1.0
        reasons.append("merchant is dormant; low-stakes topic deprioritised")
    sub = (merchant.get("subscription") or {}).get("status")
    if sub == "expired" and kind not in ("winback_eligible", "renewal_due", "dormant_with_vera"):
        total -= 1.5
        reasons.append("subscription expired; non-winback topics deprioritised")

    if customer is not None:
        if consent_match(customer, kind):
            total += 1.0
            reasons.append("consent scope covers this message type")
        else:
            total -= 1.0
            reasons.append("consent scope does not name this message type")

    return total, reasons


# ------------------------------------------------------------------ selection

def eligible(store: Store, trigger: dict, now: datetime,
             judge_listed: bool = False) -> tuple[bool, str]:
    tid = str(trigger.get("id") or "")
    merchant_id = trigger.get("merchant_id")
    merchant = store.merchant_for(merchant_id)
    if not merchant:
        return False, "merchant context not received"

    party = store.party(str(merchant_id))
    if party.opted_out:
        return False, "merchant opted out"
    if tid and tid in party.sent_trigger_ids:
        return False, "already actioned this trigger"

    key = str(trigger.get("suppression_key") or "")
    if party.suppressed(key):
        return False, "suppression key already fired"

    # `available_triggers` is the judge's statement of what is live right now;
    # it outranks a stale expires_at (the shipped dataset is dated months back,
    # so every seed trigger reads as expired against a real clock).
    exp = parse_iso(trigger.get("expires_at"))
    if exp and exp <= now and not judge_listed:
        return False, "trigger expired and not listed as active"

    if party.sends_total >= MAX_SENDS_PER_MERCHANT:
        return False, "merchant send cap reached"

    customer_id = trigger.get("customer_id")
    if customer_id:
        customer = store.get("customer", customer_id)
        if not customer:
            return False, "customer context not received"
        block = consent_block(customer, str(trigger.get("kind") or ""))
        if block:
            return False, f"consent gate: {block}"

    return True, ""


def select(store: Store, trigger_ids: list[str], now: datetime,
           limit: int = MAX_ACTIONS_PER_TICK) -> tuple[list[Candidate], list[dict]]:
    """Rank available triggers and return at most one action per merchant."""
    candidates: list[Candidate] = []
    skipped: list[dict] = []

    judge_listed = bool(trigger_ids)
    ids = list(dict.fromkeys(trigger_ids or []))
    if not ids:
        ids = store.ids_of("trigger")

    for tid in ids:
        trigger = store.trigger(tid)
        if not trigger:
            skipped.append({"trigger_id": tid, "reason": "trigger context not received"})
            continue
        trigger = dict(trigger)
        trigger.setdefault("id", tid)

        ok, why = eligible(store, trigger, now, judge_listed=judge_listed)
        if not ok:
            skipped.append({"trigger_id": tid, "reason": why})
            continue

        merchant = store.merchant_for(trigger.get("merchant_id"))
        category = store.category_for(merchant) or {}
        customer = store.get("customer", trigger.get("customer_id"))

        prio, reasons = score(trigger, merchant, customer, now)
        if prio < 0:
            skipped.append({"trigger_id": tid, "reason": reasons[0] if reasons else "low value"})
            continue
        candidates.append(Candidate(trigger=trigger, merchant=merchant, category=category,
                                    customer=customer, priority=prio, reasons=reasons))

    # deterministic ordering: priority desc, then trigger id
    candidates.sort(key=lambda c: (-c.priority, c.trigger_id))

    chosen: list[Candidate] = []
    seen_merchants: set[str] = set()
    for cand in candidates:
        if len(chosen) >= limit:
            skipped.append({"trigger_id": cand.trigger_id, "reason": "tick action cap"})
            continue
        if cand.merchant_id in seen_merchants:
            skipped.append({"trigger_id": cand.trigger_id,
                            "reason": "one message per merchant per tick"})
            continue
        party = store.party(cand.merchant_id)
        if party.quiet_until and now.timestamp() < party.quiet_until:
            skipped.append({"trigger_id": cand.trigger_id, "reason": "merchant in quiet period"})
            continue
        seen_merchants.add(cand.merchant_id)
        chosen.append(cand)

    return chosen, skipped


def conversation_id_for(cand: Candidate) -> str:
    """Decodable + resumable, per case-study guidance."""
    def slug(text: str, n: int = 18) -> str:
        out = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
        return out[:n].strip("_")

    who = slug(cand.customer_id or cand.merchant_id, 24)
    kind = slug(cand.trigger.get("kind"), 20)
    stamp = ""
    key = str(cand.trigger.get("suppression_key") or "")
    if key:
        tail = key.split(":")[-1]
        if re.search(r"\d", tail):
            stamp = "_" + slug(tail, 12)
    return f"conv_{who}_{kind}{stamp}"
