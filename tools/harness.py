#!/usr/bin/env python3
"""Local judge harness: warmup -> ticks -> replies -> replay scenarios.

Mirrors judge_simulator.py's lifecycle and timeouts but needs no LLM key, so it
can run on every edit. Timeouts here are the *tight* ones from the API examples
(healthz 2s, context 5s, tick/reply 10s), not the generous 30s in the brief.
"""
import json, sys, time, argparse
from pathlib import Path
from urllib import request as R, error as E

ROOT = Path(__file__).resolve().parents[1]
G, Y, RD, C, B, X = "\033[92m", "\033[93m", "\033[91m", "\033[96m", "\033[1m", "\033[0m"

BUDGET = {"/v1/healthz": 2.0, "/v1/metadata": 2.0, "/v1/context": 5.0,
          "/v1/tick": 10.0, "/v1/reply": 10.0}


class Bot:
    def __init__(self, base): self.base = base.rstrip("/"); self.lat = {}
    def call(self, method, path, body=None, timeout=15):
        data = json.dumps(body).encode() if body is not None else None
        req = R.Request(self.base + path, data=data, method=method,
                        headers={"Content-Type": "application/json"})
        t0 = time.time()
        try:
            with R.urlopen(req, timeout=timeout) as r:
                out = json.loads(r.read().decode()); code = r.status
        except E.HTTPError as e:
            try: out = json.loads(e.read().decode())
            except Exception: out = {"_raw": "unparseable"}
            code = e.code
        except Exception as ex:
            return None, 0, (time.time() - t0)
        dt = time.time() - t0
        self.lat.setdefault(path.split("?")[0], []).append(dt)
        return out, code, dt


def load(bot, data_dir, expanded=False):
    pushed = 0
    specs = ([("category", "categories", "slug"), ("merchant", "merchants", "merchant_id"),
              ("customer", "customers", "customer_id"), ("trigger", "triggers", "id")]
             if expanded else [("category", "categories", "slug")])
    for scope, folder, key in specs:
        d = data_dir / folder
        if not d.exists(): continue
        for f in sorted(d.glob("*.json")):
            item = json.load(open(f))
            bot.call("POST", "/v1/context", {"scope": scope, "context_id": item.get(key, f.stem),
                                             "version": 1, "payload": item}, 5)
            pushed += 1
    if not expanded:
        for fname, scope, key in [("merchants_seed.json", "merchant", "merchant_id"),
                                  ("customers_seed.json", "customer", "customer_id"),
                                  ("triggers_seed.json", "trigger", "id")]:
            blob = json.load(open(data_dir / fname))
            for item in blob.get(scope + "s", []):
                bot.call("POST", "/v1/context", {"scope": scope, "context_id": item[key],
                                                 "version": 1, "payload": item}, 5)
                pushed += 1
    return pushed


def check(name, ok, detail=""):
    print(f"  {G+'PASS'+X if ok else RD+'FAIL'+X}  {name}" + (f"  {detail}" if detail else ""))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--data", default=str(ROOT / "dataset"))
    ap.add_argument("--expanded", action="store_true")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    bot = Bot(a.url)
    fails = []

    bot.call("POST", "/v1/teardown", {}, 5)   # clean slate so re-runs are comparable
    print(f"\n{B}== WARMUP =={X}")
    h, code, dt = bot.call("GET", "/v1/healthz", None, 5)
    fails.append(not check("healthz reachable", code == 200, f"{dt*1000:.0f}ms"))
    m, code, _ = bot.call("GET", "/v1/metadata", None, 5)
    fails.append(not check("metadata", code == 200, m.get("team_name", "") if m else ""))

    n = load(bot, Path(a.data), a.expanded)
    h, _, _ = bot.call("GET", "/v1/healthz", None, 5)
    counts = (h or {}).get("contexts_loaded", {})
    fails.append(not check(f"pushed {n} contexts", sum(counts.values()) == n, str(counts)))

    print(f"\n{B}== IDEMPOTENCY =={X}")
    cat = json.load(open(Path(a.data) / "categories" / "dentists.json"))
    r1, c1, _ = bot.call("POST", "/v1/context", {"scope": "category", "context_id": "dentists",
                                                 "version": 1, "payload": cat}, 5)
    fails.append(not check("same version is a no-op", c1 == 200 and r1.get("accepted")))
    r2, c2, _ = bot.call("POST", "/v1/context", {"scope": "category", "context_id": "dentists",
                                                 "version": 0, "payload": cat}, 5)
    fails.append(not check("lower version -> 409 stale", c2 == 409 and r2.get("reason") == "stale_version"))
    r3, c3, _ = bot.call("POST", "/v1/context", {"scope": "bogus", "context_id": "x",
                                                 "version": 1, "payload": {}}, 5)
    fails.append(not check("invalid scope -> 400", c3 == 400))

    print(f"\n{B}== TICKS =={X}")
    trig_ids = []
    d = Path(a.data)
    if a.expanded:
        trig_ids = [json.load(open(f))["id"] for f in sorted((d / "triggers").glob("*.json"))]
    else:
        trig_ids = [t["id"] for t in json.load(open(d / "triggers_seed.json"))["triggers"]]

    all_actions, slow = [], 0
    for i in range(0, len(trig_ids), 5):
        batch = trig_ids[i:i + 5]
        res, code, dt = bot.call("POST", "/v1/tick", {"now": "2026-08-24T10:00:00Z",
                                                      "available_triggers": batch}, 15)
        if dt > BUDGET["/v1/tick"]: slow += 1
        acts = (res or {}).get("actions", [])
        all_actions += acts
    fails.append(not check(f"{len(all_actions)} actions over {len(trig_ids)} triggers",
                           len(all_actions) > 0))
    fails.append(not check("no tick exceeded the 10s budget", slow == 0))

    req = ["conversation_id", "merchant_id", "send_as", "trigger_id", "body", "cta",
           "suppression_key", "rationale"]
    missing = [k for act in all_actions for k in req if k not in act]
    fails.append(not check("all actions carry required fields", not missing, str(set(missing))))
    bad_url = [a2["body"] for a2 in all_actions if "http" in a2["body"] or "www." in a2["body"]]
    fails.append(not check("no URLs in any body", not bad_url))
    multi = [a2["conversation_id"] for a2 in all_actions if a2["body"].count("?") > 1]
    fails.append(not check("single question per body", not multi, str(multi[:3])))
    bodies = [a2["body"] for a2 in all_actions]
    fails.append(not check("no duplicate bodies", len(bodies) == len(set(bodies)),
                           f"{len(bodies)-len(set(bodies))} dupes"))
    convs = [a2["conversation_id"] for a2 in all_actions]
    fails.append(not check("unique conversation ids", len(convs) == len(set(convs))))

    if a.show:
        for act in all_actions[:6]:
            print(f"\n  {C}{act['trigger_id']}{X}\n  {act['body']}")

    print(f"\n{B}== PHASE 3: adaptive context injection =={X}")
    # bump the dentists category with a brand-new compliance item, then fire a
    # matching trigger and check the bot uses the *new* content
    cat2 = json.load(open(Path(a.data) / "categories" / "dentists.json"))
    cat2["digest"] = [{"id": "d_INJECTED_newrule", "kind": "compliance",
                       "title": "State board caps single-visit RCT fees at Rs 3,500 from 1 Oct",
                       "source": "State Dental Board circular 2026-09-02",
                       "summary": "Applies to all private practices. Existing price lists must be revised.",
                       "actionable": "Revise any published RCT pricing above the cap"}] + cat2["digest"]
    r, code, _ = bot.call("POST", "/v1/context", {"scope": "category", "context_id": "dentists",
                                                  "version": 2, "payload": cat2}, 5)
    fails.append(not check("version bump accepted", code == 200 and r.get("accepted")))
    inj = {"id": "trg_INJECTED_rule", "scope": "merchant", "kind": "regulation_change",
           "source": "external", "merchant_id": "m_001_drmeera_dentist_delhi",
           "customer_id": None,
           "payload": {"category": "dentists", "top_item_id": "d_INJECTED_newrule",
                       "deadline_iso": "2026-10-01"},
           "urgency": 4, "suppression_key": "reg:dentists:injected",
           "expires_at": "2026-12-31T00:00:00Z"}
    bot.call("POST", "/v1/context", {"scope": "trigger", "context_id": inj["id"],
                                     "version": 1, "payload": inj}, 5)
    res, _, _ = bot.call("POST", "/v1/tick", {"now": "2026-08-24T10:05:00Z",
                                              "available_triggers": [inj["id"]]}, 15)
    acts = (res or {}).get("actions", [])
    got = acts[0]["body"] if acts else ""
    fails.append(not check("used the newly injected digest item",
                           "3,500" in got or "State Dental Board" in got, got[:90]))
    fails.append(not check("did not fall back to the stale item", "fluoride" not in got.lower()))

    print(f"\n{B}== REPLAY: auto-reply hell =={X}")
    mid = all_actions[0]["merchant_id"] if all_actions else "m_001_drmeera_dentist_delhi"
    auto = "Thank you for contacting us! Our team will respond shortly."
    seq = []
    for i in range(1, 5):
        r, _, _ = bot.call("POST", "/v1/reply", {"conversation_id": f"conv_auto_{i}",
                                                 "merchant_id": mid, "customer_id": None,
                                                 "from_role": "merchant", "message": auto,
                                                 "received_at": "2026-08-24T10:00:00Z",
                                                 "turn_number": i + 1}, 15)
        seq.append((r or {}).get("action"))
    fails.append(not check("ends within 4 canned replies", "end" in seq, str(seq)))
    fails.append(not check("does not reply to every auto-reply", seq.count("send") <= 1, str(seq)))

    print(f"\n{B}== REPLAY: intent transition =={X}")
    r, _, _ = bot.call("POST", "/v1/reply", {"conversation_id": "conv_intent_1",
                                             "merchant_id": "m_002_bharat_dentist_mumbai",
                                             "from_role": "merchant",
                                             "message": "Ok lets do it. Whats next?",
                                             "received_at": "2026-08-24T10:00:00Z",
                                             "turn_number": 2}, 15)
    body = ((r or {}).get("body") or "").lower()
    qual = [w for w in ["would you", "do you", "can you tell", "what if", "how about"] if w in body]
    act = [w for w in ["done", "sending", "draft", "here", "confirm", "proceed", "next"] if w in body]
    fails.append(not check("switches to action mode", bool(act) and not qual,
                           f"action={act} qualifying={qual}"))

    print(f"\n{B}== REPLAY: hostile =={X}")
    r, _, _ = bot.call("POST", "/v1/reply", {"conversation_id": "conv_hostile",
                                             "merchant_id": "m_003_studio11_salon_hyderabad",
                                             "from_role": "merchant",
                                             "message": "Stop messaging me. This is useless spam.",
                                             "received_at": "2026-08-24T10:00:00Z",
                                             "turn_number": 2}, 15)
    fails.append(not check("ends on hostile", (r or {}).get("action") == "end"))

    print(f"\n{B}== REPLAY: off-topic curveball =={X}")
    r, _, _ = bot.call("POST", "/v1/reply", {"conversation_id": "conv_gst",
                                             "merchant_id": "m_006_southindiancafe_restaurant_bangalore",
                                             "from_role": "merchant",
                                             "message": "Btw can you also help me with my GST filing?",
                                             "received_at": "2026-08-24T10:00:00Z",
                                             "turn_number": 2}, 15)
    b = ((r or {}).get("body") or "")
    fails.append(not check("declines politely and redirects",
                           (r or {}).get("action") == "send" and "CA" in b, b[:70]))

    print(f"\n{B}== LATENCY =={X}")
    for path, xs in sorted(bot.lat.items()):
        p95 = sorted(xs)[int(len(xs) * 0.95)] if len(xs) > 1 else xs[0]
        budget = BUDGET.get(path, 10)
        check(f"{path} p95 {p95*1000:.0f}ms (budget {budget*1000:.0f}ms)", p95 < budget)

    nfail = sum(1 for f in fails if f)
    print(f"\n{B}{'='*60}{X}")
    print(f"{(G+'ALL CHECKS PASSED'+X) if nfail==0 else RD+str(nfail)+' CHECK(S) FAILED'+X}")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
