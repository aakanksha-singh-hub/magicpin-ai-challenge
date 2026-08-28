# Vera message engine — magicpin AI Challenge

**Live: https://magicpin-ai-challenge-ozhb.onrender.com/console**

Deterministic `compose(category, merchant, trigger, customer?)` behind five endpoints.
No LLM in the request path — p95 is **3 ms**.

```
POST /v1/context   POST /v1/tick   POST /v1/reply   GET /v1/healthz   GET /v1/metadata
```

[`/console`](https://magicpin-ai-challenge-ozhb.onrender.com/console) (also the homepage) ·
[`/docs`](https://magicpin-ai-challenge-ozhb.onrender.com/docs) · local:
`uvicorn vera.app:app --port 8080`

---

## The finding that shaped the design

Before writing code, I scored the challenge's own gold message — Case Study 1, documented at
**50/50** — through `judge_simulator.py`'s exact `LLMScorer.SYSTEM` prompt. `gpt-4o-mini`
gave it **41/50**. A stronger model gave it **29/50**, killing it on the citation: *"none are
supported by the payload."*

It was right. `LLMScorer.score` builds its prompt from a **subset** of context — name, owner,
locality, views/calls/ctr, signals, offer titles, trigger payload, customer identity. It never
sees the category digest, so "2,100-patient" and "p.14" read as invented.

Two judges scoring one sentence in opposite directions is the real problem here, so the engine
targets the intersection:

> Every message stands on facts visible in the judge's own view. Category material — digest
> stats, peer medians — is secondary, attributed, and never the sole anchor.

Same method on this engine, as a mean across *every* message rather than one showcase:
**41.0/50** on `gpt-4o-mini` (n=20), **38–40/50** on the strict judge (n=13). Parity with the
gold message on the lenient judge, ~+10 on the strict one. Neither judge ever scored anything
above 45, so the real ceiling is 44–45, not 50.

## Why deterministic

`api-call-examples.md` budgets 10s for tick/reply; the simulator enforces 15s socket timeouts
and can request 20 actions per tick. An LLM per action times out.

It also makes the worst penalty impossible by construction. Every number is registered in a
`FactPack` with provenance before it can be used, and a validator rejects any numeric token
that doesn't trace to a pushed context. Fabrication isn't discouraged by a prompt — it can't
be expressed.

## Architecture

| Module | Role |
|---|---|
| `store.py` | versioned context store — idempotent on `(scope, id, version)`, atomic replace, snapshot |
| `facts.py` | `FactPack`: derives every citable fact, tagging which ones the judge can see |
| `decide.py` | which signal earns this moment: urgency × payload richness × corroboration, minus suppression, opt-out, consent, send caps |
| `compose.py` | plan → `hook / evidence / insight / proposal / CTA`, CTA always last |
| `strategies.py` | ~30 per-trigger-kind composers + sparse-context and customer fallbacks |
| `voice.py` | per-category register, guardian/senior addressing, Hinglish code-mix, taboo list |
| `validate.py` | URLs, single CTA, jargon, taboo words, ungrounded numbers, anchor floor |
| `converse.py` | inbound classifier + reply FSM |
| `console.py` | read-only inspection UI, outside the scored surface |

## Decisions worth calling out

**Restraint.** 100 triggers produce 41 actions — one per merchant per tick, ranked. `[]` is a
valid answer.

**Consent is a hard gate.** `c_015` is a walk-in with `reminder_opt_in: false`, no consent
scope, no channel, no phone. It never gets messaged.

**Guardians, not patients.** Minors and `whatsapp_via_son` route to the guardian, patient in
the third person — *"Sharma ji's three regular medicines"*. Language preference picks the
greeting: Namaste / Vanakkam / Namaskaram / Namaskara.

**Never quote an offer the merchant doesn't run.** Customer-facing messages use only the
merchant's own active offers; a category template there would be a fabricated price.

**Category/kind mismatch is caught.** The generator randomly pairs kinds and merchants,
producing `chronic_refill_due` on a gym. Those route to a relationship-grounded fallback
rather than asking a gym member about their prescription.

**Judgement, not templating.** A Saturday IPL fixture recommends *against* the match-night
promo (`is_weeknight: false`); a seasonal dip is reframed as expected rather than sold
against; a competitor undercutting on price gets an explicit "don't match them".

**Immaterial numbers are suppressed.** A peer gap under 5%, or a payoff under 3 actions,
registers no fact at all — *"converting 0% better than the category average"* costs
specificity rather than earning it.

**Stale-dataset defences.** Shipped data is dated Apr–May 2026, so against a real clock every
trigger reads expired and a hard expiry gate would send nothing. `available_triggers` is the
authority, and the payload's `days_until` beats clock arithmetic.

**Conversation.** Auto-reply detection keys on the merchant, not the conversation — the replay
fires identical text across four `conversation_id`s. Ladder: flag once → wait 24h → end.
Commitment jumps straight to execution with one confirm. Hostility and opt-outs end it.

## The console

Every number in a composed message is clickable: it resolves to the fact behind it, that
fact's provenance, and whether the judge can see it in its own scoring payload. Ungrounded
numbers render red — the state `validate.py` refuses to emit. Plus the fact table, a tick
trace showing which trigger won each merchant and why every other was dropped, and a reply box
driving the real FSM.

Trigger kinds, CTA types and fact labels render as words rather than as the slugs the engine
passes around — `chronic_refill_due` reads as *Repeat prescription due* — with the raw key kept
alongside wherever it is the actual subject. Read-only, runs on the shipped seeds, never writes
to the judged store; registration is wrapped in a `try`, so a console failure can't stop the
scored endpoints coming up.

## Tradeoffs

- **Prose ceiling.** A template engine can't out-write a frontier model on any single message.
  Mitigated with ~30 kind-specific strategies, per-category voice and hash-seeded variants —
  41 messages, zero duplicates. Worth it for determinism, 3 ms p95 and immunity to fabrication.
- **Unseen trigger kinds** fall back to metric-derived composition rather than bespoke framing.
  Deliberate: half the generated dataset is `{"placeholder": true}`.
- **Tuned against two judges, not one.** The real harness model is unknown.

**What would have helped:** a merchant calendar (only `recall_due` ships real times), reply
outcomes (history records *that* a merchant replied, never whether the thing got done), and a
structured price list (offers are title strings, so the engine can't pick *which* to push).

## Testing

```bash
python tools/harness.py                                  # full lifecycle, no API key
python tools/harness.py --data expanded --expanded
python tools/sweep.py --all-triggers                     # every message + guardrail report
OAI=<key> python tools/llmscore.py --model gpt-4o-mini   # score with the judge's own rubric
```

`harness.py` replays the judge lifecycle — warmup, idempotency, ticks, adaptive injection and
all three replay scenarios — against the 10s budgets, no LLM required. `llmscore.py` imports
`LLMScorer.SYSTEM` straight from `judge_simulator.py`, so tuning happens against the real
grader.
