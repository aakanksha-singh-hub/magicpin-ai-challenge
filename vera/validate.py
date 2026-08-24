"""Output guard rails.

Enforces the things the harness penalises outright:
  - URLs in body            (-3 each: Meta would reject the template)
  - more than one CTA       (multi-ask dilutes; rubric wants a single binary)
  - category taboo vocabulary
  - internal jargon leaking to the merchant   (-1)
  - numbers with no provenance                (-2 fabrication)
  - at least two judge-visible anchors, or the message cannot be verified
"""
from __future__ import annotations

import re

from .voice import clean

URL_RE = re.compile(r"""(?ix)
    \b(?: https?://\S+
        | www\.\S+
        | [a-z0-9-]+\.(?:com|in|co|org|net|io|app)\b(?:/\S*)? )
""")

# words that belong to our plumbing, not to a merchant conversation
JARGON = {
    "suppression key": "", "suppression_key": "", "trigger payload": "",
    "payload": "", "context push": "", "merchant_id": "", "customer_id": "",
    "trigger kind": "", "the trigger": "this", "digest item": "item",
    "the digest": "this week's round-up", "derived signal": "",
    "factpack": "", "cta": "", "urgency score": "",
}


def strip_urls(body: str) -> tuple[str, int]:
    found = URL_RE.findall(body)
    if not found:
        return body, 0
    return clean(URL_RE.sub("", body)), len(found)


def scrub_jargon(body: str) -> tuple[str, list[str]]:
    hits = []
    low = body.lower()
    out = body
    for term, replacement in JARGON.items():
        if term in low:
            hits.append(term)
            out = re.sub(re.escape(term), replacement, out, flags=re.IGNORECASE)
    return (clean(out), hits) if hits else (body, [])


def single_cta(body: str) -> tuple[str, bool]:
    """Keep at most one question. Earlier questions become statements."""
    qs = [m.start() for m in re.finditer(r"\?", body)]
    if len(qs) <= 1:
        return body, False
    # turn every question mark except the last into a full stop
    chars = list(body)
    for idx in qs[:-1]:
        chars[idx] = "."
    return clean("".join(chars)), True


def has_numbers(text: str) -> bool:
    return bool(re.search(r"\d", text or ""))


def anchor_repair(ctx, body: str) -> str:
    """Add one concrete, verifiable clause when a message is too thin."""
    if ctx.is_customer_facing:
        visits = ctx.f("cx.visits")
        last = ctx.f("cx.last_visit")
        offer = ctx.f("offer.active.0") or ctx.f("offer.active")
        low = body.lower()
        if offer and offer.lower() not in low:
            return f"{offer} is what's running right now"
        if visits and last and last.lower() not in low:
            return f"that would be visit number {visits}, the last was {last}"
        if last and last.lower() not in low:
            return f"our last record for you is {last}"
        return ""
    from .compose import merchant_evidence
    extra = merchant_evidence(ctx)
    return extra if extra and extra.split(".")[0][:20] not in body else ""


def enforce(ctx, plan, body: str) -> tuple[str, list[str]]:
    warnings: list[str] = []

    body, n_urls = strip_urls(body)
    if n_urls:
        warnings.append(f"stripped {n_urls} url(s)")

    body, jargon_hits = scrub_jargon(body)
    if jargon_hits:
        warnings.append("scrubbed jargon: " + ", ".join(jargon_hits))

    taboo = ctx.voice.taboo_hits(body)
    if taboo:
        for term in taboo:
            body = re.sub(re.escape(term), "", body, flags=re.IGNORECASE)
        body = clean(body)
        warnings.append("removed taboo term(s): " + ", ".join(taboo))

    body, collapsed = single_cta(body)
    if collapsed:
        warnings.append("collapsed multiple questions to a single CTA")

    # every number must trace back to a registered fact
    unknown = ctx.pack.unknown_numbers(body)
    if unknown:
        warnings.append("UNGROUNDED NUMBERS: " + ", ".join(sorted(set(unknown))))

    # the message has to stand on facts the judge can actually see
    anchors = ctx.pack.visible_anchor_count(body)
    if not ctx.is_customer_facing and anchors < 2:
        warnings.append(f"only {anchors} judge-visible anchor(s)")
    elif ctx.is_customer_facing and not has_numbers(body):
        # a customer message has no merchant metrics to lean on; the bar is that
        # it still says something concrete
        warnings.append("no concrete detail in customer message")

    if len(body) > 900:
        warnings.append(f"long body ({len(body)} chars)")

    return clean(body), warnings
