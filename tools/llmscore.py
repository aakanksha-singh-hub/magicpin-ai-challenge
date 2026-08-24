#!/usr/bin/env python3
"""Score our composed messages with the judge's EXACT rubric and prompt format.

Imports LLMScorer.SYSTEM straight out of judge_simulator.py so we are tuning
against the real grader, not an approximation of it.
"""
import argparse, json, os, sys, re, importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as R

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vera.store import Store
from vera import decide
from vera.compose import compose
from tools.sweep import load, load_expanded

spec = importlib.util.spec_from_file_location("js", str(ROOT / "judge_simulator.py"))
js = importlib.util.module_from_spec(spec); spec.loader.exec_module(js)
SYSTEM = js.LLMScorer.SYSTEM

DIMS = ["specificity", "category_fit", "merchant_fit", "decision_quality",
        "engagement_compulsion"]
G, Y, RD, B, X = "\033[92m", "\033[93m", "\033[91m", "\033[1m", "\033[0m"


def judge_prompt(action, category, merchant, trigger, customer):
    body = action["body"]
    return f"""SCORE THIS MESSAGE:

=== CONTEXT PROVIDED TO BOT ===
Category: {category.get('slug', 'unknown')}
Voice: {category.get('voice', {}).get('tone', 'unknown')}
Taboos: {category.get('voice', {}).get('vocab_taboo', [])[:5]}

Merchant: {merchant.get('identity', {}).get('name', 'unknown')}
Owner: {merchant.get('identity', {}).get('owner_first_name', 'unknown')}
Locality: {merchant.get('identity', {}).get('locality', 'unknown')}
Languages: {merchant.get('identity', {}).get('languages', [])}
Performance: views={merchant.get('performance', {}).get('views', '?')}, calls={merchant.get('performance', {}).get('calls', '?')}, ctr={merchant.get('performance', {}).get('ctr', '?')}
Signals: {merchant.get('signals', [])}
Active Offers: {[o.get('title') for o in merchant.get('offers', []) if o.get('status') == 'active']}

Trigger Kind: {trigger.get('kind', 'unknown')}
Trigger Payload: {json.dumps(trigger.get('payload', {}))}
Trigger Urgency: {trigger.get('urgency', '?')}

Customer: {json.dumps(customer.get('identity', {})) if customer else 'None (merchant-facing)'}

=== BOT'S MESSAGE ===
Body ({len(body)} chars): "{body}"
CTA: {action.get('cta', 'none')}
Send As: {action.get('send_as', 'vera')}

Score each dimension 0-10 with clear reasoning. Be STRICT."""


_THROTTLE = __import__("threading").Semaphore(2)


def call_llm(model, prompt, key, attempts=8):
    """Exponential backoff that honours Retry-After. The scoring account
    rate-limits aggressively, and a half-empty sample is worse than a slow one."""
    import time as _t, random
    last = None
    for i in range(attempts):
        try:
            with _THROTTLE:
                return _call_once(model, prompt, key)
        except Exception as e:
            last = e
            msg = str(e)
            if not any(c in msg for c in ("429", "500", "502", "503", "504", "timed out")):
                raise
            hdrs = getattr(e, "headers", None)
            wait = hdrs.get("retry-after") if hdrs else None
            try:
                wait = float(wait) if wait else None
            except (TypeError, ValueError):
                wait = None
            # A multi-hour retry-after means the *daily* request cap is spent
            # (this key is 50 requests/day). Sleeping against a 24h reset just
            # hangs the run, so fail loudly instead.
            if wait and wait > 120:
                raise RuntimeError(
                    f"daily request quota exhausted - resets in {wait/3600:.1f}h "
                    f"(x-ratelimit-limit-requests=50/day). No further scoring today.")
            _t.sleep((wait or min(60.0, 2.0 * (2 ** i))) + random.uniform(0, 1.5))
    raise last


def _call_once(model, prompt, key):
    payload = {"model": model,
               "messages": [{"role": "system", "content": SYSTEM},
                            {"role": "user", "content": prompt}]}
    if model.startswith(("gpt-5", "o3", "o4")):
        payload["max_completion_tokens"] = 2500
    else:
        payload["max_tokens"] = 1500
        payload["temperature"] = 0.2
    req = R.Request("https://api.openai.com/v1/chat/completions",
                    data=json.dumps(payload).encode(),
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with R.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode())
    txt = d["choices"][0]["message"]["content"]
    m = re.search(r"\{[\s\S]*\}", txt)
    if not m:
        raise ValueError("no json in judge reply")
    return json.loads(m.group()), d["usage"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--data", default=str(ROOT / "dataset"))
    ap.add_argument("--expanded", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default="")
    ap.add_argument("--all-triggers", action="store_true",
                    help="score every trigger, not just the ones selection would send")
    ap.add_argument("--complaints", action="store_true",
                    help="print every sub-9 reason, grouped by dimension")
    a = ap.parse_args()
    key = os.environ["OAI"]

    store = Store(snapshot_path="/tmp/vera_score.json")
    (load_expanded if a.expanded else load)(store, Path(a.data))
    now = datetime.now(timezone.utc)
    if a.all_triggers:
        cands = []
        for tid in store.ids_of("trigger"):
            t = dict(store.trigger(tid)); t.setdefault("id", tid)
            m = store.merchant_for(t.get("merchant_id"))
            if not m:
                continue
            cands.append(decide.Candidate(trigger=t, merchant=m,
                                          category=store.category_for(m) or {},
                                          customer=store.get("customer", t.get("customer_id")),
                                          priority=0.0, reasons=[]))
    else:
        cands, _ = decide.select(store, store.ids_of("trigger"), now, limit=1000)
    if a.limit:
        cands = cands[:a.limit]

    jobs = []
    for c in cands:
        out = compose(c.category, c.merchant, c.trigger, c.customer, now=now,
                      priority_reasons=c.reasons)
        action = {"body": out.body, "cta": out.cta, "send_as": out.send_as}
        jobs.append((c, out, judge_prompt(action, c.category, c.merchant, c.trigger, c.customer)))

    print(f"scoring {len(jobs)} messages with {a.model} ...")
    results, tok_in, tok_out = [], 0, 0

    def run(job):
        c, out, prompt = job
        try:
            data, usage = call_llm(a.model, prompt, key)
            return c, out, data, usage
        except Exception as e:
            return c, out, {"error": str(e)}, {}

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for c, out, data, usage in ex.map(run, jobs):
            tok_in += usage.get("prompt_tokens", 0); tok_out += usage.get("completion_tokens", 0)
            results.append((c, out, data))

    scored = [(c, o, d) for c, o, d in results if "error" not in d]
    errs = [d["error"] for _, _, d in results if "error" in d]
    if errs:
        print(f"{RD}{len(errs)} error(s){X}: {errs[0][:200]}")
    errs = len(errs)
    if not scored:
        print("all scoring calls failed:", results[0][2] if results else "")
        return 1

    def total(d): return sum(int(d.get(k, 0)) for k in DIMS)
    scored.sort(key=lambda t: total(t[2]))

    print(f"\n{B}=== PER-DIMENSION AVERAGES (n={len(scored)}, errors={errs}) ==={X}")
    for dim in DIMS:
        vals = [int(d.get(dim, 0)) for _, _, d in scored]
        avg = sum(vals) / len(vals)
        col = G if avg >= 7.5 else (Y if avg >= 6 else RD)
        bar = "█" * int(avg * 2) + "░" * (20 - int(avg * 2))
        print(f"  {dim:24} {col}{bar}{X} {avg:5.2f}  (min {min(vals)}, max {max(vals)})")
    avg_total = sum(total(d) for _, _, d in scored) / len(scored)
    print(f"  {B}{'TOTAL':24} {avg_total:5.2f} / 50   ({avg_total/50*100:.0f}%){X}")

    print(f"\n{B}=== WEAKEST 6 ==={X}")
    for c, o, d in scored[:6]:
        print(f"\n  {RD}{total(d)}/50{X} [{c.trigger.get('kind')}] {c.merchant_id[:34]}")
        print(f"  {o.body[:150]}...")
        for dim in DIMS:
            v = int(d.get(dim, 0))
            if v <= 6:
                print(f"    {Y}{dim} {v}{X}: {d.get(dim + '_reason', d.get('engagement_reason',''))[:130]}")

    if a.complaints:
        print(f"\n{B}=== EVERY SUB-9 COMPLAINT, BY DIMENSION ==={X}")
        for dim in DIMS:
            rows = [(int(d.get(dim, 0)),
                     d.get(dim + "_reason", d.get("engagement_reason", "")), c.trigger.get("kind"))
                    for c, _, d in scored if int(d.get(dim, 0)) < 9]
            if not rows:
                continue
            rows.sort()
            print(f"\n  {Y}{dim}{X}  ({len(rows)} of {len(scored)} below 9)")
            for v, reason, kind in rows:
                print(f"    {v}  [{kind}] {reason[:190]}")

    print(f"\ntokens: {tok_in} in / {tok_out} out")
    if a.out:
        json.dump([{"trigger_id": c.trigger_id, "kind": c.trigger.get("kind"),
                    "merchant_id": c.merchant_id, "body": o.body, "scores": d}
                   for c, o, d in scored], open(a.out, "w"), indent=1, ensure_ascii=False)
        print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
