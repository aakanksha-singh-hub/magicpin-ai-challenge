"""Versioned context store + conversation/suppression state.

The judge pushes context incrementally and expects idempotency on
(scope, context_id, version).  Everything is held in memory for speed and
mirrored to a JSON snapshot so an unexpected process restart mid-test is
survivable rather than fatal.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

SCOPES = ("category", "merchant", "customer", "trigger")

SNAPSHOT_PATH = os.environ.get("VERA_SNAPSHOT", "/tmp/vera_snapshot.json")
SNAPSHOT_EVERY_SECONDS = 5.0


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    txt = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Entry:
    version: int
    payload: dict
    delivered_at: str = ""
    stored_at: str = field(default_factory=utcnow_iso)


@dataclass
class Conversation:
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    trigger_id: str | None = None
    send_as: str = "vera"
    state: str = "open"                       # open | waiting | ended
    turns: list[dict] = field(default_factory=list)
    sent_bodies: list[str] = field(default_factory=list)
    stage: str = "pitch"                      # pitch | action | closing
    outbound_count: int = 0
    inbound_count: int = 0
    unanswered_nudges: int = 0
    offered: str = ""                         # short label of what we proposed
    opened_at: str = field(default_factory=utcnow_iso)

    def record_outbound(self, body: str) -> None:
        self.turns.append({"role": "bot", "body": body, "ts": utcnow_iso()})
        if body:
            self.sent_bodies.append(body)
        self.outbound_count += 1
        self.unanswered_nudges += 1

    def record_inbound(self, body: str) -> None:
        self.turns.append({"role": "merchant", "body": body, "ts": utcnow_iso()})
        self.inbound_count += 1
        self.unanswered_nudges = 0

    def has_sent(self, body: str) -> bool:
        norm = " ".join(body.lower().split())
        return any(" ".join(b.lower().split()) == norm for b in self.sent_bodies)


@dataclass
class PartyState:
    """Per-merchant (or per-customer) state that must survive across conversations.

    The auto-reply replay in judge_simulator.py fires the same canned text on four
    *different* conversation_ids for one merchant, so repetition has to be tracked
    at this level, not per conversation.
    """
    party_id: str
    opted_out: bool = False
    autoreply_streak: int = 0
    last_inbound_norm: str = ""
    repeat_streak: int = 0
    fired_keys: dict[str, str] = field(default_factory=dict)   # suppression_key -> iso ts
    sent_trigger_ids: set[str] = field(default_factory=set)
    conversations: list[str] = field(default_factory=list)
    sends_total: int = 0
    last_send_ts: float = 0.0
    quiet_until: float = 0.0

    def suppressed(self, key: str) -> bool:
        return bool(key) and key in self.fired_keys


class Store:
    def __init__(self, snapshot_path: str = SNAPSHOT_PATH):
        self._lock = threading.RLock()
        self.entries: dict[tuple[str, str], Entry] = {}
        self.conversations: dict[str, Conversation] = {}
        self.parties: dict[str, PartyState] = {}
        self.started_at = time.time()
        self.snapshot_path = snapshot_path
        self._last_snapshot = 0.0
        self._dirty = False
        self.stats = {"context_pushes": 0, "ticks": 0, "replies": 0, "actions_sent": 0}

    # ---------------- context ----------------

    def put_context(self, scope: str, context_id: str, version: int,
                    payload: dict, delivered_at: str = "") -> dict:
        """Idempotent on (scope, context_id, version).

        same version  -> accepted no-op (spec: "re-posting the same version is a no-op")
        lower version -> stale_version
        higher        -> atomic replace
        """
        with self._lock:
            key = (scope, context_id)
            cur = self.entries.get(key)
            if cur is not None and version < cur.version:
                return {"accepted": False, "reason": "stale_version",
                        "current_version": cur.version}
            if cur is not None and version == cur.version:
                return {"accepted": True, "idempotent": True,
                        "ack_id": f"ack_{scope}_{context_id}_v{version}",
                        "stored_at": cur.stored_at}
            self.entries[key] = Entry(version=version, payload=payload,
                                      delivered_at=delivered_at or "")
            self.stats["context_pushes"] += 1
            self._dirty = True
            self._maybe_snapshot()
            return {"accepted": True,
                    "ack_id": f"ack_{scope}_{context_id}_v{version}",
                    "stored_at": self.entries[key].stored_at}

    def get(self, scope: str, context_id: str | None) -> dict | None:
        if not context_id:
            return None
        entry = self.entries.get((scope, context_id))
        return entry.payload if entry else None

    def version_of(self, scope: str, context_id: str) -> int:
        entry = self.entries.get((scope, context_id))
        return entry.version if entry else 0

    def all_of(self, scope: str) -> list[dict]:
        return [e.payload for (s, _), e in self.entries.items() if s == scope]

    def ids_of(self, scope: str) -> list[str]:
        return [cid for (s, cid) in self.entries if s == scope]

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in SCOPES}
        for (scope, _) in self.entries:
            out[scope] = out.get(scope, 0) + 1
        return out

    # ---------------- resolution helpers ----------------

    def merchant_for(self, merchant_id: str | None) -> dict | None:
        return self.get("merchant", merchant_id)

    def category_for(self, merchant: dict | None) -> dict | None:
        if not merchant:
            return None
        return self.get("category", merchant.get("category_slug"))

    def trigger(self, trigger_id: str | None) -> dict | None:
        return self.get("trigger", trigger_id)

    def customers_of(self, merchant_id: str) -> list[dict]:
        return [p for p in self.all_of("customer") if p.get("merchant_id") == merchant_id]

    # ---------------- conversations / parties ----------------

    def party(self, party_id: str | None) -> PartyState:
        pid = party_id or "_unknown_"
        with self._lock:
            st = self.parties.get(pid)
            if st is None:
                st = PartyState(party_id=pid)
                self.parties[pid] = st
            return st

    def conversation(self, conversation_id: str) -> Conversation | None:
        return self.conversations.get(conversation_id)

    def ensure_conversation(self, conversation_id: str, **kw) -> Conversation:
        with self._lock:
            conv = self.conversations.get(conversation_id)
            if conv is None:
                conv = Conversation(conversation_id=conversation_id, **kw)
                self.conversations[conversation_id] = conv
                if conv.merchant_id:
                    self.party(conv.merchant_id).conversations.append(conversation_id)
            return conv

    def open_conversation_for(self, merchant_id: str, customer_id: str | None = None) -> Conversation | None:
        for cid in reversed(self.party(merchant_id).conversations):
            conv = self.conversations.get(cid)
            if conv and conv.state != "ended" and conv.customer_id == customer_id:
                return conv
        return None

    # ---------------- snapshot ----------------

    def _maybe_snapshot(self) -> None:
        # an empty path means "never persist" -- used by the console's demo
        # store, whose data is disposable and must not touch the judged snapshot
        if not self.snapshot_path:
            return
        now = time.time()
        if not self._dirty or now - self._last_snapshot < SNAPSHOT_EVERY_SECONDS:
            return
        self._last_snapshot = now
        self._dirty = False
        threading.Thread(target=self._write_snapshot, daemon=True).start()

    def _write_snapshot(self) -> None:
        try:
            blob = {"entries": [{"scope": s, "context_id": c, "version": e.version,
                                 "payload": e.payload, "delivered_at": e.delivered_at}
                                for (s, c), e in list(self.entries.items())]}
            tmp = self.snapshot_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(blob, fh)
            os.replace(tmp, self.snapshot_path)
        except Exception:
            pass

    def load_snapshot(self) -> int:
        try:
            with open(self.snapshot_path) as fh:
                blob = json.load(fh)
        except Exception:
            return 0
        n = 0
        for row in blob.get("entries", []):
            try:
                self.entries[(row["scope"], row["context_id"])] = Entry(
                    version=int(row["version"]), payload=row["payload"],
                    delivered_at=row.get("delivered_at", ""))
                n += 1
            except Exception:
                continue
        return n

    def teardown(self) -> None:
        with self._lock:
            self.entries.clear()
            self.conversations.clear()
            self.parties.clear()
            try:
                os.remove(self.snapshot_path)
            except OSError:
                pass
