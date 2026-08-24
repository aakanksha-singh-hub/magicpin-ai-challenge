"""Category voice adapters + deterministic phrasing variation.

Two jobs:
  1. make a dentist sound like a dentist and a restaurateur like an operator
  2. vary phrasing across 50 merchants without ever becoming non-deterministic
     (variants are chosen by a hash of the merchant+trigger, so the same input
     always yields the same output)
"""
from __future__ import annotations

import hashlib
import re
from typing import Sequence

# ------------------------------------------------------------------ variation

def seed_index(seed: str, n: int) -> int:
    if n <= 1:
        return 0
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % n


def pick(seed: str, options: Sequence[str]) -> str:
    opts = [o for o in options if o]
    if not opts:
        return ""
    return opts[seed_index(seed, len(opts))]


# ------------------------------------------------------------------ profiles

CATEGORY_VOICE = {
    "dentists": {
        "salute": "Dr. {owner}",
        "salute_fallback": "Doc",
        "audience": "patients",
        "audience_singular": "patient",
        "asset": "chair time",
        "channel_artifact": "patient-education WhatsApp",
        "post_word": "Google post",
        "emoji": "",
        "cx_emoji": "🦷",
        "hindi_ok": True,
    },
    "salons": {
        "salute": "Hi {owner}",
        "salute_fallback": "Hi there",
        "audience": "clients",
        "audience_singular": "client",
        "asset": "chair slots",
        "channel_artifact": "WhatsApp broadcast",
        "post_word": "Google post",
        "emoji": "",
        "cx_emoji": "✨",
        "hindi_ok": True,
    },
    "restaurants": {
        "salute": "{owner}",
        "salute_fallback": "Quick one",
        "audience": "covers",
        "audience_singular": "cover",
        "asset": "kitchen capacity",
        "channel_artifact": "Swiggy/Zomato banner copy",
        "post_word": "Google post",
        "emoji": "",
        "cx_emoji": "🍽️",
        "hindi_ok": True,
    },
    "gyms": {
        "salute": "Hi {owner}",
        "salute_fallback": "Coach",
        "audience": "members",
        "audience_singular": "member",
        "asset": "class slots",
        "channel_artifact": "member WhatsApp",
        "post_word": "Google post",
        "emoji": "",
        "cx_emoji": "💪",
        "hindi_ok": False,
    },
    "pharmacies": {
        "salute": "{owner}",
        "salute_fallback": "Namaste",
        "audience": "customers",
        "audience_singular": "customer",
        "asset": "counter time",
        "channel_artifact": "customer WhatsApp",
        "post_word": "Google post",
        "emoji": "",
        "cx_emoji": "",
        "hindi_ok": True,
    },
}

DEFAULT_VOICE = {
    "salute": "Hi {owner}", "salute_fallback": "Hi there", "audience": "customers",
    "audience_singular": "customer", "asset": "capacity",
    "channel_artifact": "customer WhatsApp", "post_word": "Google post",
    "emoji": "", "cx_emoji": "", "hindi_ok": True,
}

# regional greeting keyed off the customer's stated language preference
LANG_GREETING = {
    "hi": "Namaste", "hi-en mix": "Namaste",
    "ta": "Vanakkam", "ta-en mix": "Vanakkam",
    "te": "Namaskaram", "te-en mix": "Namaskaram",
    "kn": "Namaskara", "kn-en mix": "Namaskara",
    "mr": "Namaskar", "mr-en mix": "Namaskar",
}

# short, natural code-mix connectors -- used at most once per message
HINDI_CONNECTORS = ["chalega", "theek hai"]


class Voice:
    def __init__(self, category: dict | None, merchant: dict | None,
                 customer: dict | None = None, seed: str = ""):
        self.category = category or {}
        self.merchant = merchant or {}
        self.customer = customer
        self.seed = seed
        self.slug = str(self.category.get("slug") or "")
        self.profile = CATEGORY_VOICE.get(self.slug, DEFAULT_VOICE)
        vprofile = self.category.get("voice") or {}
        self.taboos = [t for t in (vprofile.get("vocab_taboo") or []) if t]
        self.vocab = [v for v in (vprofile.get("vocab_allowed") or []) if v]
        self.tone = str(vprofile.get("tone") or "")
        self.code_mix = str(vprofile.get("code_mix") or "")
        langs = ((self.merchant.get("identity") or {}).get("languages")) or []
        self.langs = [str(l).lower() for l in langs]

    # -- addressing ---------------------------------------------------

    def salute(self) -> str:
        owner = ((self.merchant.get("identity") or {}).get("owner_first_name") or "").strip()
        if not owner:
            name = ((self.merchant.get("identity") or {}).get("name") or "").strip()
            if name:
                return f"{name} team"
            return self.profile["salute_fallback"]
        owner = owner.replace("Dr.", "").replace("Dr ", "").strip()
        return self.profile["salute"].format(owner=owner)

    def cx_addressing(self) -> dict:
        """Resolve who we are actually talking to.

        The dataset plants minors ("Aanya (parent: Sneha)") and a senior whose
        channel is `whatsapp_via_son` -- writing to the patient in those cases
        is both wrong and, for a minor, inappropriate.
        """
        ident = (self.customer or {}).get("identity") or {}
        prefs = (self.customer or {}).get("preferences") or {}
        raw = str(ident.get("name") or "").strip()
        pref = str(ident.get("language_pref") or "").strip().lower()
        channel = str(prefs.get("channel") or "").lower()
        age = str(ident.get("age_band") or "").lower()

        subject, guardian = raw, ""
        m = re.match(r"^(.*?)\s*\((?:parent|guardian|son|daughter)\s*:\s*(.*?)\)\s*$",
                     raw, re.IGNORECASE)
        if m:
            subject, guardian = m.group(1).strip(), m.group(2).strip()
        if raw.startswith("(") or "no profile" in raw.lower():
            subject = ""

        via_guardian = bool(guardian) or channel.startswith("whatsapp_via") \
            or age.startswith("child")

        greet_word = LANG_GREETING.get(pref, "Hi")
        if guardian:
            greeting = f"{greet_word} {guardian}"
            addressee = guardian
        elif via_guardian or not subject:
            greeting = greet_word
            addressee = ""
        else:
            greeting = f"{greet_word} {subject}"
            addressee = subject

        # respectful third-person reference for a senior spoken about, not to
        honorific = subject
        if subject and via_guardian and not age.startswith("child"):
            base = subject.replace("Mr.", "").replace("Mrs.", "").replace("Ms.", "").strip()
            honorific = f"{base} ji" if base else subject

        return {"greeting": greeting, "addressee": addressee, "subject": subject,
                "honorific": honorific, "via_guardian": via_guardian,
                "has_name": bool(subject or guardian)}

    def customer_greeting(self) -> str:
        return self.cx_addressing()["greeting"]

    def customer_uses_hindi(self) -> bool:
        pref = str(((self.customer or {}).get("identity") or {}).get("language_pref") or "").lower()
        return pref.startswith("hi")

    # -- code-mix -----------------------------------------------------

    @property
    def hindi_ok(self) -> bool:
        if self.customer is not None:
            return self.customer_uses_hindi()
        return ("hi" in self.langs
                and self.profile["hindi_ok"]
                and self.code_mix == "hindi_english_natural")

    def connector(self) -> str:
        """One short Hindi tag question, or empty."""
        if not self.hindi_ok:
            return ""
        return pick(self.seed + "|connector", HINDI_CONNECTORS)

    # -- vocabulary ---------------------------------------------------

    def term(self, *candidates: str) -> str:
        """Prefer a term the category explicitly allows."""
        allowed = {v.lower(): v for v in self.vocab}
        for cand in candidates:
            if cand.lower() in allowed:
                return allowed[cand.lower()]
        return candidates[0] if candidates else ""

    @property
    def audience(self) -> str:
        return self.profile["audience"]

    @property
    def artifact(self) -> str:
        return self.profile["channel_artifact"]

    def emoji(self) -> str:
        return self.profile["cx_emoji"] if self.customer is not None else self.profile["emoji"]

    # -- safety -------------------------------------------------------

    def taboo_hits(self, text: str) -> list[str]:
        low = text.lower()
        hits = []
        for t in self.taboos:
            base = t.split("(")[0].strip().lower()
            if base and base in low:
                hits.append(base)
        return hits


def cap(text: str) -> str:
    """Upper-case the first letter only -- str.capitalize() lower-cases the rest
    and would turn '1 Apr' into '1 apr'."""
    text = (text or "").strip()
    if not text:
        return text
    return text[0].upper() + text[1:] if text[0].islower() else text


def clean(text: str) -> str:
    """Collapse whitespace, tidy punctuation, keep it WhatsApp-shaped."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\s+([,.;:?!])", r"\1", text)
    text = re.sub(r"([,.;:?!])([A-Za-z₹])", r"\1 \2", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r" +", " ", text)
    return text.strip()
