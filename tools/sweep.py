#!/usr/bin/env python3
"""Compose across the whole dataset and report grounding/guardrail problems."""
import json, sys, argparse
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vera.store import Store
from vera import decide
from vera.compose import compose

ROOT = Path(__file__).resolve().parents[1]


def load(store, data_dir: Path):
    for f in sorted((data_dir / "categories").glob("*.json")):
        d = json.load(open(f)); store.put_context("category", d.get("slug", f.stem), 1, d)
    for fname, key, scope in [("merchants_seed.json", "merchant_id", "merchant"),
                              ("customers_seed.json", "customer_id", "customer"),
                              ("triggers_seed.json", "id", "trigger")]:
        p = data_dir / fname
        if not p.exists():
            for alt in ["merchants", "customers", "triggers"]:
                pass
            continue
        blob = json.load(open(p))
        items = blob.get(scope + "s", blob.get(scope, []))
        for it in items:
            store.put_context(scope, it[key], 1, it)


def load_expanded(store, data_dir: Path):
    """The generator writes one JSON file per entity into per-scope folders."""
    for scope, folder, key in [("category", "categories", "slug"),
                               ("merchant", "merchants", "merchant_id"),
                               ("customer", "customers", "customer_id"),
                               ("trigger", "triggers", "id")]:
        d = data_dir / folder
        if not d.exists():
            continue
        for f in sorted(d.glob("*.json")):
            item = json.load(open(f))
            store.put_context(scope, item.get(key, f.stem), 1, item)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "dataset"))
    ap.add_argument("--expanded", action="store_true")
    ap.add_argument("--all-triggers", action="store_true",
                    help="compose for every trigger, bypassing per-merchant selection")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    store = Store(snapshot_path="/tmp/vera_sweep.json")
    data_dir = Path(args.data)
    (load_expanded if args.expanded else load)(store, data_dir)
    now = datetime.now(timezone.utc)

    tids = store.ids_of("trigger")
    if args.all_triggers:
        cands = []
        for tid in tids:
            t = dict(store.trigger(tid)); t.setdefault("id", tid)
            m = store.merchant_for(t.get("merchant_id"))
            if not m:
                continue
            cands.append(decide.Candidate(trigger=t, merchant=m,
                                          category=store.category_for(m) or {},
                                          customer=store.get("customer", t.get("customer_id")),
                                          priority=0.0, reasons=[]))
    else:
        cands, _ = decide.select(store, tids, now, limit=1000)

    rows, problems = [], []
    for c in cands:
        out = compose(c.category, c.merchant, c.trigger, c.customer, now=now,
                      priority_reasons=c.reasons)
        rows.append((c, out))
        for w in out.warnings:
            if "UNGROUNDED" in w or "anchor" in w or "url" in w or "taboo" in w:
                problems.append((c.trigger_id, w))
        if not args.quiet:
            tag = f"[{c.trigger.get('kind')}]"
            who = c.customer_id or c.merchant_id
            print(f"\n\033[96m{tag} {who}\033[0m  cta={out.cta} send_as={out.send_as}")
            print(f"  {out.body}")
            if out.warnings:
                print(f"  \033[93m! {'; '.join(out.warnings)}\033[0m")

    print(f"\n{'='*72}\ncomposed {len(rows)} messages")
    lens = [len(o.body) for _, o in rows]
    if lens:
        print(f"body chars: min {min(lens)} / median {sorted(lens)[len(lens)//2]} / max {max(lens)}")
    dup = len(rows) - len({o.body for _, o in rows})
    print(f"duplicate bodies: {dup}")
    print(f"guardrail problems: {len(problems)}")
    for tid, w in problems[:25]:
        print(f"  - {tid}: {w}")

    if args.out:
        with open(args.out, "w") as fh:
            for c, o in rows:
                fh.write(json.dumps({"trigger_id": c.trigger_id, "kind": c.trigger.get("kind"),
                                     "merchant_id": c.merchant_id, "customer_id": c.customer_id,
                                     "body": o.body, "cta": o.cta, "send_as": o.send_as,
                                     "suppression_key": o.suppression_key,
                                     "rationale": o.rationale}, ensure_ascii=False) + "\n")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
