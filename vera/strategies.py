"""Per-trigger-kind composition strategies.

Each returns a MessagePlan. Copy is assembled from registered facts only, and
leads with anchors the judge can verify inside its own scoring prompt.
"""
from __future__ import annotations

import re

from .voice import cap
from .compose import (Ctx, MessagePlan, best_offer, category_read, digest_item,
                      merchant_evidence, offer_is_own, own_offer, peer_gap_clause,
                      place_clause, stated_or_computed_days, strategy, uplift_clause)

METRIC_NOUN = {"review_count": "reviews", "reviews": "reviews", "views": "views",
               "calls": "calls", "directions": "direction requests", "leads": "leads",
               "footfall": "footfall", "covers": "covers", "members": "members"}


def _metric(raw: str, default: str = "calls") -> str:
    raw = (raw or "").strip()
    return METRIC_NOUN.get(raw, raw.replace("_", " ") or default)


def _phrase(raw: str) -> str:
    """'6_month_cleaning' -> '6-month cleaning'"""
    out = str(raw or "").replace("_", " ").strip()
    return re.sub(r"(\d)\s+([a-z])", r"\1-\2", out)


def _program_phrase(raw: str) -> str:
    """'skin_prep_program_30day' -> '30-day skin-prep programme'"""
    words = [w for w in str(raw or "").replace("-", "_").split("_") if w]
    if not words:
        return ""
    lead = ""
    rest = []
    for w in words:
        m = re.fullmatch(r"(\d+)\s*(day|week|month|min|minute)s?", w)
        if m and not lead:
            lead = f"{m.group(1)}-{m.group(2)}"
        else:
            rest.append(w)
    body = " ".join(rest).replace("program", "programme")
    body = re.sub(r"\bskin prep\b", "skin-prep", body)
    return f"{lead} {body}".strip() if lead else body


def _pretty_pref(raw: str) -> str:
    out = str(raw or "").replace("_", " ").strip()
    days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
            "weekday", "weekend")
    return " ".join(w.capitalize() if w.lower() in days else w for w in out.split())


def _seasonal_beat_for(ctx: Ctx, hint: str) -> tuple[str, str]:
    """Find the category beat the trigger is actually talking about.

    The trigger names its own window ('post_resolution_window_apr_jun'); trusting
    that beats a current-month lookup, which would otherwise pair a dip warning
    with an unrelated (or contradictory) calendar note.
    """
    months = re.findall(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
                        (hint or "").lower())
    beats = ctx.category.get("seasonal_beats") or []
    if months:
        for beat in beats:
            rng = str(beat.get("month_range") or "").lower()
            if all(m in rng for m in months[:2]):
                return str(beat.get("month_range") or ""), str(beat.get("note") or "")
    return ctx.f("season.now.range"), ctx.f("season.now.note")


def _first_sentence(text: str) -> str:
    """Split on sentence ends without breaking decimals like '1.5 mSv'."""
    m = re.search(r"(?<!\d)\.(?!\d)", text or "")
    return (text[:m.start()] if m else (text or "")).strip()

FAST = "Say yes and I'll have it ready in 5 minutes."


def _artifact(ctx: Ctx) -> str:
    return ctx.voice.artifact


def _mix(ctx: Ctx, text: str) -> str:
    """Turn the CTA into natural Hinglish while keeping exactly one question."""
    tag = ctx.voice.connector()
    if not tag:
        return text
    return f"{text.rstrip('?').rstrip()} — {tag}?"


# ---------------------------------------------------------------- knowledge

@strategy("research_digest")
def research_digest(ctx: Ctx) -> MessagePlan:
    item = digest_item(ctx)
    title = item["title"] or ctx.f("trg.top_item_id").replace("_", " ")
    # every hook below promises "one item worth your time: <title>". A trigger
    # whose digest item doesn't resolve -- placeholder payloads, or an item id
    # the judge hasn't pushed the category for -- would deliver that promise
    # empty, so hand it to the sparse-context composer instead of announcing
    # an item that isn't there.
    if not title.strip():
        return fallback(ctx)
    source = item["source"]
    cite = f" ({source})" if source else ""
    action = item["action"]

    hook = ctx.choose(
        "rd.hook",
        f"this week's {ctx.slug[:-1] if ctx.slug.endswith('s') else ctx.slug} reading has one item worth your time: {title}{cite}",
        f"one item from this week's clinical round-up is relevant to you: {title}{cite}",
    ) if ctx.slug == "dentists" else ctx.choose(
        "rd.hook",
        f"one item from this week's {ctx.slug} round-up is worth two minutes: {title}{cite}",
        f"this landed in the {ctx.slug} round-up and applies to you: {title}{cite}",
    )

    evidence = merchant_evidence(ctx)
    insight = ""
    if ctx.f("signal.high_risk_adult_cohort"):
        insight = "Your roster is flagged high-risk-adult heavy, which is exactly the group this applies to"
    elif action:
        insight = action.rstrip(".")
    elif peer_gap_clause(ctx):
        insight = peer_gap_clause(ctx)

    proposal = (f"I can turn it into a short {_artifact(ctx)} plus a {ctx.voice.profile['post_word']}, "
                f"both in your voice, nothing for you to write")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta=_mix(ctx, "Want me to draft both?"),
        cta_type="binary_yes_no",
        levers=["credibility via source citation", "reciprocity", "effort externalisation"],
        note="cited the source explicitly so the claim is checkable rather than asserted",
    )


@strategy("regulation_change", "compliance_alert")
def regulation_change(ctx: Ctx) -> MessagePlan:
    item = digest_item(ctx)
    title = item["title"] or "a rule change in your category"
    source = item["source"]
    deadline = ctx.any_of("trg.deadline_iso.pretty", "trg.deadline_iso",
                          "trg.effective_date.pretty")
    days = ctx.f("trg.deadline_iso.days")

    hook = f"compliance note, not a promo: {title}"
    if source:
        hook += f" ({source})"
    evidence = ""
    if deadline:
        evidence = f"It takes effect {deadline}"
        if days:
            evidence += f", about {days} days out"
    insight = _first_sentence(item["summary"])
    proposal = ("I can check your current setup against the new limit and give you a one-page "
                "before/after so you know if anything needs changing")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Want me to run that check?",
        cta_type="binary_yes_no",
        levers=["regulatory urgency", "loss aversion", "effort externalisation"],
        note="compliance framing kept factual; no scare language",
    )


@strategy("cde_opportunity")
def cde_opportunity(ctx: Ctx) -> MessagePlan:
    item = digest_item(ctx)
    title = item["title"] or "a CDE session in your category"
    credits = ctx.f("trg.credits")
    fee = ctx.f("trg.fee").replace("_", " ")
    hook = f"{title}" + (f" ({item['source']})" if item["source"] else "")
    evidence = " ".join(x for x in [
        f"{credits} CDE credits" if credits else "",
        f"— {fee}" if fee else "",
    ] if x).strip()
    insight = merchant_evidence(ctx)
    proposal = ("If you go, I can put a short note on your listing that you're keeping up with "
                "current technique — patients do read that")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Want me to hold the details and remind you the day before?",
        cta_type="binary_yes_no",
        levers=["professional development", "low commitment", "reciprocity"],
    )


# ---------------------------------------------------------------- performance

@strategy("perf_dip")
def perf_dip(ctx: Ctx) -> MessagePlan:
    metric = _metric(ctx.f("trg.metric"), "calls")
    delta = ctx.f("trg.delta_pct")
    window = ctx.f("trg.window") or "7d"
    baseline = ctx.f("trg.vs_baseline")

    hook = f"your {metric} are down {delta} over the last {window}" if delta \
        else f"your {metric} have slipped this week"
    if baseline:
        hook += f", against a {baseline} baseline"

    evidence = merchant_evidence(ctx)
    insight = peer_gap_clause(ctx) or uplift_clause(ctx)

    signals = " ".join(ctx.merchant.get("signals") or [])
    if "unverified_gbp" in signals:
        insight = ("Your listing is still unverified, which caps how often Google shows it — "
                   "that's usually the first thing to fix when calls fall")
    elif "no_active_offers" in signals:
        offer = best_offer(ctx)
        insight = (f"You have no live offer right now; a service-and-price line like "
                   f"\"{offer}\" is what actually converts a view into a call" if offer else insight)

    proposal = ("I can put together the two fixes that move this fastest and show you both "
                "before anything goes live")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta=_mix(ctx, "Want me to line those up?"),
        cta_type="binary_yes_no",
        levers=["loss aversion", "diagnosis before pitch", "effort externalisation"],
    )


@strategy("seasonal_perf_dip")
def seasonal_dip(ctx: Ctx) -> MessagePlan:
    metric = _metric(ctx.f("trg.metric"), "views")
    delta = ctx.f("trg.delta_pct")
    # the trigger's own seasonal note outranks a current-month lookup: the
    # trigger knows which window it is describing, the calendar does not
    window, note = _seasonal_beat_for(ctx, ctx.f("trg.season_note"))
    expected = ctx.v("trg.is_expected_seasonal")

    if delta and window and expected:
        hook = (f"your {metric} are down {delta} this week — before you react, that's the normal "
                f"{window} dip for {ctx.slug}, not something broken on your listing")
    elif delta:
        hook = (f"your {metric} are down {delta} this week, and it reads as seasonal rather than "
                f"a problem with your listing")
    else:
        hook = f"heads up on the seasonal pattern for {ctx.slug} right now"
    evidence = merchant_evidence(ctx)
    insight = ""
    offer = best_offer(ctx)
    insight = ((insight + ". " if insight else "")
               + f"So this month the return is in holding the {ctx.voice.audience} you already "
                 f"have rather than buying new ones — acquisition spend works harder later in "
                 f"the year")
    proposal = (f"\"{offer}\" is already live, so I'd point it at existing {ctx.voice.audience} "
                f"as a bring-a-friend rather than a new-joiner hook, and draft the message to go "
                f"with it" if offer and offer_is_own(ctx) else
                f"I can draft a retention push for your existing {ctx.voice.audience} that runs "
                f"through the dip")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Want me to draft it?",
        cta_type="binary_yes_no",
        levers=["anxiety pre-emption", "contrarian judgement", "retention over acquisition"],
        note="reframed an alarming metric as an expected seasonal pattern rather than selling against it",
    )


@strategy("category_seasonal", "demand_shift")
def category_seasonal(ctx: Ctx) -> MessagePlan:
    season = (ctx.f("trg.season") or "").replace("_", " ")
    trends = [ctx.f(f"trg.trends.{i}") for i in range(4)]
    trends = [t.replace("_", " ") for t in trends if t]
    up = [t for t in trends if "+" in t]
    down = [t for t in trends if "-" in t]

    def tidy(items):
        return ", ".join(i.replace("demand ", "").replace("+", "up ").replace("-", "down ")
                         for i in items)

    hook = (f"the {season} demand shift has started in your category" if season
            else "demand in your category is shifting with the season")
    evidence = merchant_evidence(ctx)
    bits = []
    if up:
        bits.append(f"moving up: {tidy(up)}")
    if down:
        bits.append(f"falling away: {tidy(down)}")
    insight = cap("; ".join(bits)) if bits else ctx.f("season.now.note")
    if ctx.v("trg.shelf_action_recommended"):
        insight += ". Front-of-counter space is the lever here, not price"
    proposal = ("I can put the rising lines on your listing and write the counter prompt your "
                "staff can use, so the shelf and the listing say the same thing")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Want me to set that up?", cta_type="binary_yes_no",
        levers=["seasonal demand data", "operational specificity", "effort externalisation"],
        note="stocking/merchandising advice rather than a discount push",
    )


@strategy("perf_spike")
def perf_spike(ctx: Ctx) -> MessagePlan:
    metric = _metric(ctx.f("trg.metric"), "calls")
    delta = ctx.f("trg.delta_pct")
    driver = ctx.f("trg.likely_driver").replace("_", " ")
    hook = (f"your {metric} are up {delta} this week" if delta
            else f"your {metric} are climbing this week")
    if driver:
        hook += f", and it traces back to the {driver}"
    evidence = merchant_evidence(ctx)
    insight = ("Spikes like this fade in about two weeks unless something keeps feeding them, "
               "so the useful move is to repeat what caused it while it's still working")
    proposal = f"I can build three more posts in the same shape as the {driver or 'one that worked'}"
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Want me to line them up?",
        cta_type="binary_yes_no",
        levers=["positive momentum", "urgency window", "effort externalisation"],
    )


@strategy("milestone_reached")
def milestone(ctx: Ctx) -> MessagePlan:
    metric = _metric(ctx.f("trg.metric"), "reviews")
    now_v = ctx.f("trg.value_now")
    target = ctx.f("trg.milestone_value")
    hook = (f"you're at {now_v} {metric} — {target} is within reach this month"
            if now_v and target else f"you just crossed a {metric} milestone")
    evidence = merchant_evidence(ctx)
    gap = ""
    try:
        gap_n = int(float(ctx.v("trg.milestone_value"))) - int(float(ctx.v("trg.value_now")))
        if gap_n > 0:
            gap = ctx.num(str(gap_n))
    except (TypeError, ValueError):
        pass
    insight = (f"That's {gap} more. The ones who get there fastest just ask at the counter on "
               f"the day of service, when people are still happy" if gap else "")
    proposal = ("I can write a two-line ask your staff can say out loud, plus the message to send "
                "afterwards")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Want it?",
        cta_type="binary_yes_no",
        levers=["goal gradient", "social proof", "low-effort script"],
    )


@strategy("review_theme_emerged")
def review_theme(ctx: Ctx) -> MessagePlan:
    theme = (ctx.f("trg.theme") or "a recurring theme").replace("_", " ")
    count = ctx.f("trg.occurrences_30d")
    trend = ctx.f("trg.trend")
    quote = ctx.f("trg.common_quote")
    hook = (f"\"{theme}\" has come up in {count} reviews this month and it's {trend}"
            if count else f"\"{theme}\" is showing up repeatedly in your reviews")
    evidence = f"One of them reads: \"{quote}\"" if quote else merchant_evidence(ctx)
    insight = ("A theme this consistent is an operations signal, not a reputation one — replying "
               "publicly to those reviews with what you've changed is what moves the rating back")
    proposal = ("I can draft replies to those reviews in your voice, each naming the specific fix, "
                "for you to approve")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Want me to draft them?",
        cta_type="binary_yes_no",
        levers=["specific quoted evidence", "operations framing", "effort externalisation"],
    )


# ---------------------------------------------------------------- commercial

@strategy("renewal_due")
def renewal_due(ctx: Ctx) -> MessagePlan:
    days = ctx.f("trg.days_remaining") or ctx.f("sub.days_left")
    amount = ctx.f("trg.renewal_amount")
    plan_name = ctx.f("trg.plan") or ctx.f("sub.plan")
    hook = (f"your {plan_name} plan renews in {days} days" if days
            else f"your {plan_name} plan is up for renewal")
    if amount:
        hook += f" at {amount}"
    evidence = merchant_evidence(ctx)
    insight = ""
    uplift = uplift_clause(ctx)
    if uplift:
        insight = uplift
    else:
        insight = ("Worth deciding on the numbers rather than the date — that's what the last "
                   "30 days actually returned")
    proposal = ("Before you decide, I can send a plain one-pager of what the plan produced this "
                "cycle — views, calls, and what changed")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Want that first?",
        cta_type="binary_yes_no",
        levers=["deadline", "evidence-before-ask", "trust through transparency"],
        note="led with proof of value rather than a renewal push",
    )


@strategy("winback_eligible")
def winback(ctx: Ctx) -> MessagePlan:
    days = ctx.f("trg.days_since_expiry") or ctx.f("sub.days_expired")
    dip = ctx.f("trg.perf_dip_pct")
    added = ctx.f("trg.lapsed_customers_added_since_expiry")
    hook = (f"it's been {days} days since your plan lapsed" if days
            else "your plan has been inactive for a while")
    evidence = merchant_evidence(ctx)
    bits = []
    if dip:
        bits.append(f"visibility is down {dip} since then")
    if added:
        bits.append(f"and {added} more {ctx.voice.audience} have gone quiet in that window")
    insight = cap(", ".join(bits)) if bits else ""
    proposal = (f"I'm not going to pitch the plan. I'll show you the {added or 'lapsed'} "
                f"{ctx.voice.audience} list and one message that usually brings a chunk of them back"
                if added else
                "I'd rather show you what's recoverable first, then you decide about the plan")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Want to see the list?",
        cta_type="binary_yes_no",
        levers=["loss aversion", "curiosity", "value before ask"],
        note="explicitly declined to lead with the subscription pitch",
    )


@strategy("gbp_unverified")
def gbp_unverified(ctx: Ctx) -> MessagePlan:
    uplift = ctx.f("trg.estimated_uplift_pct")
    path = ctx.f("trg.verification_path").replace("_", " ")
    hook = "your Google listing is still unverified"
    evidence = merchant_evidence(ctx)
    insight = (f"Unverified listings get shown less and can't be edited reliably — the estimate "
               f"on your account is around {uplift} more visibility once it's done" if uplift else
               "Unverified listings get shown less and can't be edited reliably")
    if path:
        insight += f". Yours can be verified by {path}"
    proposal = ("It's the shortest high-impact fix on your listing, and I can walk you through it "
                "or do the parts that don't need your phone")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta=_mix(ctx, "Shall we start it?"),
        cta_type="binary_yes_no",
        levers=["quantified upside", "effort cap", "clear single step"],
    )


@strategy("competitor_opened")
def competitor_opened(ctx: Ctx) -> MessagePlan:
    name = ctx.f("trg.competitor_name")
    dist = ctx.f("trg.distance_km")
    their = ctx.f("trg.their_offer")
    opened = ctx.any_of("trg.opened_date.pretty", "trg.opened_date")
    hook = (f"{name} opened {dist} km from you" if name and dist
            else "a new competitor has opened nearby")
    if opened:
        hook += f" on {opened}"
    evidence = merchant_evidence(ctx)
    mine = best_offer(ctx)
    insight = ""
    if their and mine and offer_is_own(ctx):
        insight = (f"They're leading with \"{their}\" against your \"{mine}\". Matching them on "
                   f"price is the losing move — you have the established listing and the review "
                   f"history, and that's what people actually compare")
    elif their:
        insight = (f"They're leading with \"{their}\". The answer isn't a lower price, it's making "
                   f"your listing the one that looks established")
    proposal = ("I can put together what your listing shows against theirs, and the two changes "
                "that widen the gap in your favour")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Want that comparison?",
        cta_type="binary_yes_no",
        levers=["competitive curiosity", "loss aversion", "contrarian advice"],
        note="advised against a price war rather than encouraging a discount",
    )


# ---------------------------------------------------------------- timing

@strategy("festival_upcoming")
def festival(ctx: Ctx) -> MessagePlan:
    fest = ctx.f("trg.festival") or "the festival"
    days, date = stated_or_computed_days(ctx, "trg.date", "trg.days_until")
    if days and date:
        hook = f"{fest} is on {date}, {days} days out"
    elif date:
        hook = f"{fest} falls on {date}"
    elif days:
        hook = f"{fest} is {days} days out"
    else:
        hook = f"{fest} is coming up"
    evidence = merchant_evidence(ctx)
    offer = best_offer(ctx)
    insight = (f"The bookings for it get made well before the week itself, so the listing needs to "
               f"say what you're doing now rather than on the day")
    proposal = (f"I can set up a {fest} line on your listing built around \"{offer}\" and schedule "
                f"the posts to run up to it" if offer else
                f"I can set up a {fest} line on your listing and schedule the posts to run up to it")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta=_mix(ctx, "Want me to set it up?"),
        cta_type="binary_yes_no",
        levers=["calendar urgency", "lead-time insight", "existing offer leverage"],
    )


@strategy("ipl_match_today", "local_event")
def ipl_match(ctx: Ctx) -> MessagePlan:
    match = ctx.f("trg.match") or "tonight's match"
    venue = ctx.f("trg.venue")
    city = ctx.f("trg.city")
    weeknight = ctx.v("trg.is_weeknight")
    offer = best_offer(ctx)

    hook = f"{match}" + (f" at {venue}" if venue else "") + (f" in {city}" if city and not venue else "")
    evidence = merchant_evidence(ctx)

    if weeknight is False:
        insight = ("Weekend match days are the trap — people watch at home, so dine-in usually "
                   "softens rather than lifts. The volume is in delivery, not the floor")
        proposal = (f"So I'd skip a floor promo and push \"{offer}\" as a delivery-first special "
                    f"instead" if offer else
                    "So I'd skip the floor promo and push a delivery-first special instead")
        note = "advised against the obvious match-night promo because the payload says it is not a weeknight"
        levers = ["contrarian data-informed call", "saves a bad decision", "existing offer leverage"]
    else:
        insight = ("Weeknight matches are the ones that actually lift covers, and the orders land "
                   "in the hour before start")
        proposal = (f"I can push \"{offer}\" as a match-night combo and have the banner copy ready "
                    f"before the toss" if offer else
                    "I can have match-night banner copy ready before the toss")
        note = "weeknight match confirmed in payload, so promoted rather than deferred"
        levers = ["time-boxed urgency", "existing offer leverage", "effort externalisation"]

    return MessagePlan(hook=hook, evidence=evidence, insight=insight, proposal=proposal,
                       cta="Want me to write it?", cta_type="binary_yes_no",
                       levers=levers, note=note)


# ---------------------------------------------------------------- conversation

@strategy("active_planning_intent")
def active_planning(ctx: Ctx) -> MessagePlan:
    topic = (ctx.f("trg.intent_topic") or "the idea you raised").replace("_", " ")
    said = ctx.f("trg.merchant_last_message") or ctx.f("conv.last_body")
    offer = best_offer(ctx)
    hook = f"picking up {topic} from where you left it"
    evidence = merchant_evidence(ctx)
    insight = ("You asked what it would look like, so here's a first version to react to rather "
               "than a set of questions" if said else
               "Here's a first version to react to rather than another round of questions")
    proposal = (f"I've shaped it around \"{offer}\" since that's already live and priced" if
                offer and offer_is_own(ctx) else
                "I've shaped it around what your listing already sells")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Shall I send the draft over now?",
        cta_type="binary_yes_no",
        levers=["momentum continuation", "artifact over questions", "no re-qualifying"],
        note="merchant already signalled intent, so this advances to a draft instead of qualifying further",
    )


@strategy("curious_ask_due")
def curious_ask(ctx: Ctx) -> MessagePlan:
    trend = ctx.f("trend.top.query")
    delta = ctx.f("trend.top.delta")
    place = ctx.f("m.name") or "your place"

    hook = ctx.choose(
        "ca.hook",
        "quick one, and your answer genuinely helps me get this right",
        "one question this week, and it takes ten seconds to answer",
    )
    evidence = merchant_evidence(ctx)
    insight = (f"Searches for \"{trend}\" are up {delta} year on year in your category, so that's "
               f"my guess at what's moving" if trend and delta else "")
    proposal = (f"Whatever you tell me, I'll turn into a {ctx.voice.profile['post_word']} and a "
                f"short price reply your staff can use at the counter")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta=f"So — what's been the most asked-for thing at {place} this week?",
        cta_type="open_ended",
        levers=["asking the merchant", "reciprocity offered up front", "ten-second effort"],
        note="curiosity/ask family -- the lever production Vera under-uses most; question placed last",
    )


@strategy("dormant_with_vera")
def dormant(ctx: Ctx) -> MessagePlan:
    days = ctx.f("trg.days_since_last_merchant_message")
    topic = (ctx.f("trg.last_topic") or "").replace("_", " ")
    hook = (f"we last spoke {days} days ago" + (f" about {topic}" if topic else "")
            if days else "it's been a while since we spoke")
    evidence = merchant_evidence(ctx)
    insight = ("I'm not going to keep nudging you about the same thing. One useful number instead, "
               "and then it's your call")
    gap = peer_gap_clause(ctx) or uplift_clause(ctx)
    if gap:
        insight = f"{insight}. {gap}"
    proposal = "If that's worth ten minutes, I'll show you the two changes behind it"
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Worth it?",
        cta_type="binary_yes_no",
        levers=["pattern interrupt", "explicit low pressure", "single number hook"],
        note="dormant merchant, so tone is deliberately low-pressure with an easy exit",
    )


@strategy("supply_alert")
def supply_alert(ctx: Ctx) -> MessagePlan:
    molecule = ctx.f("trg.molecule")
    batches = ctx.f("trg.affected_batches.list")
    n_batches = ctx.f("trg.affected_batches.count")
    mfr = ctx.f("trg.manufacturer")
    chronic = ctx.f("agg.chronic_rx_count")

    where = place_clause(ctx)
    hook = (f"time-sensitive for {where}: a voluntary recall is out on {n_batches} "
            f"{molecule} batches ({batches})"
            if molecule and batches and where else
            (f"time-sensitive: voluntary recall on {n_batches} {molecule} batches ({batches})"
             if molecule and batches else "time-sensitive supply alert for your counter"))
    if mfr:
        hook += f", manufacturer {mfr}"
    evidence = (f"{molecule.capitalize()} is a standing repeat line, so some of it has already "
                f"gone home with regulars" if molecule else "")
    insight = ("First job is pulling the affected stock off the shelf; second is telling the people "
               "who already took it home, before they hear it somewhere else")
    proposal = ("I can draft the notice for affected buyers and a simple replacement-pickup flow "
                "your counter staff can follow")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Want both drafted now?",
        cta_type="binary_yes_no",
        levers=["urgency", "batch-level specificity", "complete workflow offered"],
        note="highest-urgency category event; led with the recall facts, not a sales angle",
    )


# ---------------------------------------------------------------- customer-facing

@strategy("recall_due")
def recall_due(ctx: Ctx) -> MessagePlan:
    addr = ctx.voice.cx_addressing()
    who = addr["honorific"] if addr["via_guardian"] else ""
    service = _phrase(ctx.f("trg.service_due")) or "check-up"
    slots = ctx.f("trg.available_slots.labels")
    slot0, slot1 = ctx.f("trg.available_slots.0"), ctx.f("trg.available_slots.1")
    gap_m = ctx.f("cx.gap_months")
    offer = own_offer(ctx, keywords=("clean", "check", "consult"))
    clinic = ctx.f("m.name")

    due = ctx.any_of("trg.due_date.pretty", "trg.due_date")
    last_svc = ctx.any_of("trg.last_service_date.pretty", "trg.last_service_date")

    hook = f"{clinic} here" if clinic else "quick note from the clinic"
    subject = f"{who}'s" if who else "your"
    if due:
        evidence = f"{subject} {service} comes due on {due}"
        if last_svc:
            evidence += f" — the last one was {last_svc}"
    else:
        evidence = (f"{subject} {service} is due" +
                    (f" — it's been about {gap_m} months since the last visit" if gap_m else ""))
    insight = f"{offer} covers it" if offer else ""

    if slot0 and slot1:
        proposal = f"Two evening slots are open just before that: {slot0} or {slot1}" if due \
            else f"Two slots are open: {slot0} or {slot1}"
        cta = f"Reply 1 for {slot0.split(',')[0]}, 2 for {slot1.split(',')[0]}, or tell us a time that suits you better."
        cta_type = "multi_choice_slot"
    elif slots:
        proposal = f"We have {slots} open"
        cta = "Reply with the one that works and we'll hold it."
        cta_type = "open_ended"
    else:
        proposal = "We can hold a slot this week"
        cta = "Reply YES and we'll confirm a time."
        cta_type = "binary_yes_no"

    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta=cta, cta_type=cta_type,
        levers=["due-date specificity", "concrete slots", "low-friction reply"],
        note="sent from the merchant's number; no clinical claims made",
    )


@strategy("chronic_refill_due")
def chronic_refill(ctx: Ctx) -> MessagePlan:
    addr = ctx.voice.cx_addressing()
    who = addr["honorific"] or "your"
    molecules = ctx.f("trg.molecule_list.list")
    n_mol = ctx.f("trg.molecule_list.count")
    runs_out = ctx.any_of("trg.stock_runs_out_iso.pretty", "trg.stock_runs_out_iso")
    saved = ctx.v("trg.delivery_address_saved")
    pharmacy = ctx.f("m.name")
    senior = (ctx.customer or {}).get("identity", {}).get("senior_citizen")

    offers = [ctx.f(f"offer.active.{i}") for i in range(3)]
    senior_offer = next((o for o in offers if o and "senior" in o.lower()), "")
    delivery_offer = next((o for o in offers if o and "delivery" in o.lower()), "")

    hook = f"{pharmacy} here" if pharmacy else "a note from your pharmacy"
    subject = f"{who}'s" if addr["via_guardian"] else "Your"
    evidence = (f"{subject} {n_mol} regular medicines ({molecules}) run out on {runs_out}"
                if molecules and runs_out else f"{subject} regular medicines are due for a refill")
    bits = ["Same molecules, same pack"]
    if senior and senior_offer:
        bits.append(f"{senior_offer} applied")
    if saved and delivery_offer:
        bits.append(f"{delivery_offer} to the saved address")
    elif saved:
        bits.append("delivery to the saved address")
    insight = ", ".join(bits)
    proposal = "Nothing changes unless you tell us the doctor revised the dose"
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Reply CONFIRM and we'll pack it, or tell us if the prescription has changed.",
        cta_type="binary_confirm_cancel",
        levers=["precise molecule names", "run-out date", "respectful senior handling"],
        note="addressed the registered contact channel rather than the patient directly",
    )


@strategy("customer_lapsed_hard", "customer_lapsed_soft", "winback_customer")
def customer_lapsed(ctx: Ctx) -> MessagePlan:
    addr = ctx.voice.cx_addressing()
    days = ctx.f("trg.days_since_last_visit") or ctx.f("cx.gap_days")
    weeks = ""
    try:
        weeks = ctx.num(str(int(float(ctx.v("trg.days_since_last_visit") or
                                     ctx.v("cx.gap_days") or 0)) // 7))
    except (TypeError, ValueError):
        weeks = ""
    focus = (ctx.f("trg.previous_focus") or ctx.f("cx.last_service") or "").replace("_", " ")
    months = ctx.f("trg.previous_membership_months")
    place = ctx.f("m.name")
    owner = ctx.f("m.owner")
    offer = own_offer(ctx)

    hook = f"{owner} from {place} here" if owner and place else (f"{place} here" if place else "")
    evidence = (f"It's been about {weeks} weeks" if weeks and weeks != "0"
                else (f"It's been {days} days" if days else "It's been a while"))
    if months:
        evidence += f", and you were with us {months} months before that"
    insight = "That happens to most people at some point — no lecture from us"
    if focus:
        insight += f". You were working on {focus}, and that's still the easiest thing to restart"
    proposal = (f"{offer} is open right now if you want a no-commitment way back in"
                if offer else "There's a no-commitment way back in whenever you want it")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Reply YES and we'll hold a spot for you — no auto-charge, cancel anytime.",
        cta_type="binary_yes_no",
        levers=["no-shame framing", "past goal referenced", "commitment barriers removed"],
        note="lapse framed without guilt; two common objections pre-answered in the CTA",
    )


@strategy("trial_followup")
def trial_followup(ctx: Ctx) -> MessagePlan:
    addr = ctx.voice.cx_addressing()
    subject = addr["subject"] or "your"
    trial = ctx.any_of("trg.trial_date.pretty", "trg.trial_date")
    nxt = ctx.f("trg.next_session_options.0") or ctx.f("trg.next_session_options.labels")
    place = ctx.f("m.name")
    offer = own_offer(ctx, keywords=("trial", "first", "month", "class"))

    hook = f"{place} here" if place else ""
    evidence = (f"{subject} came for the trial on {trial}" if trial and addr["via_guardian"]
                else (f"You came in for the trial on {trial}" if trial else "Following up on the trial session"))
    insight = ("The children who carry on usually start the week after the trial, while it's still "
               "fresh" if addr["via_guardian"] else "Momentum matters most in the first week after a trial")
    proposal = f"The next session is {nxt}" if nxt else "We can slot you into the next session"
    if offer:
        proposal += f", and {offer} covers the start"
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Reply YES and we'll save the place.",
        cta_type="binary_yes_no",
        levers=["specific next date", "momentum", "single confirm"],
    )


@strategy("wedding_package_followup")
def wedding_followup(ctx: Ctx) -> MessagePlan:
    addr = ctx.voice.cx_addressing()
    days, wdate = stated_or_computed_days(ctx, "trg.wedding_date", "trg.days_to_wedding")
    trial = ctx.any_of("trg.trial_completed.pretty", "trg.trial_completed")
    window = _program_phrase(ctx.f("trg.next_step_window_open"))
    owner, place = ctx.f("m.owner"), ctx.f("m.name")
    offer = best_offer(ctx, keywords=("bridal", "skin", "facial", "package", "membership"),
                       strict=True)

    hook = f"{owner} from {place} here" if owner and place else (f"{place} here" if place else "")
    evidence = (f"{days} days to {wdate}" if days and wdate
                else (f"{days} days to go" if days else "Your date is coming up"))
    if trial:
        evidence += f", and your trial was back on {trial}"
    insight = (f"The {window} is the part that has to start now — it's the one thing that can't be "
               f"rushed later" if window else
               "This is the window where the prep work has to start; it can't be compressed later")
    proposal = f"{offer} is what covers it" if offer else ""
    slot = _pretty_pref(ctx.f("cx.slot_pref"))
    cta = (f"Want me to hold a {slot} slot for the first session?" if slot
           else "Want me to hold the first session for you?")
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta=cta, cta_type="binary_yes_no",
        levers=["countdown specificity", "window-closing urgency", "preference honoured"],
    )


@strategy("appointment_tomorrow")
def appointment_tomorrow(ctx: Ctx) -> MessagePlan:
    addr = ctx.voice.cx_addressing()
    when = ctx.any_of("trg.slot.label", "trg.appointment_time", "trg.when",
                      "trg.slot_label", "cx.slot_pref")
    place, locality = ctx.f("m.name"), ctx.f("m.locality")
    hook = f"{place} here" + (f" in {locality}" if locality else "") if place else ""
    evidence = (f"Confirming your appointment tomorrow, {when}" if when
                else "Confirming your appointment with us tomorrow")
    visits = ctx.f("cx.visits")
    insight = f"That's visit number {visits} with us" if visits else ""
    proposal = "If anything's changed, tell us now and we'll move it rather than lose the slot"
    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Reply YES to confirm.",
        cta_type="binary_yes_no",
        levers=["confirmation", "no-show prevention", "single reply"],
    )


# ---------------------------------------------------------------- fallback

@strategy("__fallback__")
def fallback(ctx: Ctx) -> MessagePlan:
    """Sparse context: placeholder payloads and merchants with no offers,
    no history and no signals. Everything here is derived arithmetic over the
    merchant's own visible numbers plus the category knowledge pack."""
    kind = (ctx.kind or "update").replace("_", " ")
    views, calls, ctr = ctx.f("perf.views"), ctx.f("perf.calls"), ctx.f("perf.ctr")
    side = ctx.f("derived.ctr_side")
    per_k = ctx.f("derived.calls_per_1k")

    if side == "below" and ctx.f("derived.ctr_uplift_actions"):
        hook = (f"your listing turned {views} views into {calls} calls last month — "
                f"that's {per_k} calls per 1,000 views")
        insight = peer_gap_clause(ctx)
        proposal_core = uplift_clause(ctx)
    elif side == "above":
        hook = (f"you're converting better than most — {views} views into {calls} calls, "
                f"{per_k} per 1,000")
        insight = peer_gap_clause(ctx)
        proposal_core = "The gap now is traffic, not conversion, so that's where the work is"
    else:
        hook = f"a quick read on your last 30 days: {views} views, {calls} calls" if views else \
               f"a quick read on where you stand on {kind}"
        insight = peer_gap_clause(ctx)
        proposal_core = ""

    read = category_read(ctx)
    if read and not insight:
        insight = read
    season = ctx.f("season.now.note")
    trend, tdelta = ctx.f("trend.top.query"), ctx.f("trend.top.delta")
    if season:
        insight = (insight + ". " if insight else "") + f"Seasonally: {season.rstrip('.')}"
    elif trend and tdelta:
        insight = ((insight + ". " if insight else "")
                   + f"Searches for \"{trend}\" are up {tdelta} year on year in your category")

    offer = best_offer(ctx)
    if offer and not offer_is_own(ctx):
        proposal = (f"You have no live offer on the listing. \"{offer}\" is the pattern that works "
                    f"in {ctx.slug} — a service and a price, not a percentage")
    elif offer:
        proposal = f"I'd put \"{offer}\" in front of that traffic more often than it currently runs"
    else:
        proposal = proposal_core or "I can show you the two changes that move this most"
    if proposal_core and offer:
        proposal = f"{proposal_core}. {proposal}"

    return MessagePlan(
        hook=hook, evidence="", insight=insight, proposal=proposal,
        cta=_mix(ctx, "Want me to set that up?"),
        cta_type="binary_yes_no",
        levers=["derived benchmark", "service-and-price over percentage", "single change proposed"],
        note="trigger payload was sparse, so the message is grounded in the merchant's own metrics",
    )


@strategy("__customer_fallback__")
def customer_fallback(ctx: Ctx) -> MessagePlan:
    """Sparse or category-mismatched customer trigger.

    Grounded entirely in the real relationship record, and it quotes only offers
    this merchant actually runs.
    """
    addr = ctx.voice.cx_addressing()
    place = ctx.f("m.name")
    owner = ctx.f("m.owner")
    locality = ctx.f("m.locality")
    visits = ctx.f("cx.visits")
    last = ctx.f("cx.last_visit")
    gap_w = ""
    try:
        gap_w = ctx.num(str(int(float(ctx.v("cx.gap_days") or 0)) // 7))
    except (TypeError, ValueError):
        gap_w = ""
    service = _phrase(ctx.f("cx.last_service"))
    offer = own_offer(ctx)
    state = ctx.f("cx.state")

    hook = (f"{owner} from {place} here" if owner and place
            else (f"{place} here" if place else ""))
    if locality and place:
        hook = f"{hook} in {locality}"

    bits = []
    if visits and visits != "0":
        bits.append(f"you've been in {visits} times")
    if last:
        bits.append(f"the last was {last}")
    evidence = cap(", ".join(bits)) if bits else ""
    if gap_w and gap_w not in ("0",) and state.startswith("lapsed"):
        evidence = (evidence + f", about {gap_w} weeks ago") if evidence \
            else f"It's been about {gap_w} weeks"

    insight = (f"You usually come in for {service}, so that's what we'd keep ready"
               if service else "")
    proposal = (f"{offer} is running right now if you'd like to use it"
                if offer else "We can hold a slot whenever suits you")

    return MessagePlan(
        hook=hook, evidence=evidence, insight=insight, proposal=proposal,
        cta="Reply YES and we'll book it, or tell us a day that works better.",
        cta_type="binary_yes_no",
        levers=["relationship history", "no pressure", "single reply"],
        note="sparse or mismatched customer trigger; grounded in the visit record only",
    )
