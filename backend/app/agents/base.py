"""Shared context passed to every agent, plus small deterministic helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from rapidfuzz import fuzz
from rapidfuzz.distance import DamerauLevenshtein

from ..config import Settings
from ..gemini import Gemini
from ..models import Finding, Payment, PaymentRequest, SourceDoc, Vendor
from ..sources import Sources

Emit = Callable[[Finding], Awaitable[None]]


@dataclass
class AgentContext:
    settings: Settings
    request: PaymentRequest
    docs: list[SourceDoc]
    blobs: dict[str, bytes]               # label -> raw bytes
    vendors: list[Vendor]
    vendor: Vendor | None
    vendor_match_reason: str | None
    history: list[Payment]
    sources: Sources
    gemini: Gemini | None
    notes: dict = field(default_factory=dict)  # agents may share facts (e.g. base amount)

    @property
    def email(self) -> SourceDoc | None:
        return next((d for d in self.docs if d.label == "email"), None)

    def sender_address(self) -> str | None:
        if self.email and self.email.headers.get("From"):
            return self.email.headers["From"]
        return self.request.requester_email


# -------------------------------------------------------------- helpers ----
def norm_account(v: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (v or "").upper())


def iban_valid(iban: str) -> bool:
    s = norm_account(iban)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


SWIFT_RE = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")


def swift_valid(bic: str) -> bool:
    return bool(SWIFT_RE.fullmatch(norm_account(bic)))


HOMOGLYPHS = str.maketrans({"0": "o", "1": "l", "3": "e", "5": "s", "7": "t", "@": "a", "$": "s"})


def skeleton(domain: str) -> str:
    d = domain.lower().translate(HOMOGLYPHS)
    return d.replace("rn", "m").replace("vv", "w").replace("cl", "d").replace("-", "")


def sld(domain: str) -> str:
    parts = domain.lower().split(".")
    # crude registrable-label extraction good enough for .com, .com.sg, .co.jp
    if len(parts) >= 3 and parts[-2] in {"com", "co", "net", "org", "gov", "ac"}:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else parts[0]


def lookalike_of(domain: str, trusted: list[str]) -> str | None:
    """Return the trusted domain this one imitates, or None."""
    d = domain.lower()
    if d in trusted:
        return None
    for t in trusted:
        if DamerauLevenshtein.distance(d, t) <= 2:
            return t
        if skeleton(d) == skeleton(t):
            return t
        ts, ds = sld(t), sld(d)
        if len(ts) >= 5 and (DamerauLevenshtein.distance(ds, ts) <= 1 or skeleton(ds) == skeleton(ts)
                             or (ts in ds.replace("-", ""))):
            return t
    return None


def match_vendor(req: PaymentRequest, sender_domain: str | None,
                 vendors: list[Vendor]) -> tuple[Vendor | None, str | None]:
    if sender_domain:
        for v in vendors:
            if sender_domain in v.domains:
                return v, f"sender domain {sender_domain} is on file for {v.legal_name}"
    best, best_score, name_used = None, 0.0, None
    for name in filter(None, [req.vendor_name, req.beneficiary_name]):
        for v in vendors:
            for cand in [v.legal_name, *v.aliases]:
                score = fuzz.token_set_ratio(name.lower(), cand.lower())
                if score > best_score:
                    best, best_score, name_used = v, score, name
    if best and best_score >= 85:
        return best, f"name '{name_used}' matches vendor master ({best_score:.0f}% similarity)"
    return None, None


FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com",
             "proton.me", "protonmail.com", "icloud.com", "aol.com", "gmx.com", "mail.com",
             "yandex.com", "zoho.com", "qq.com", "163.com"}


GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def gstin_valid(g: str) -> bool:
    """Format + check digit of an Indian GSTIN (GSTN's published mod-36 algorithm)."""
    g = norm_account(g)
    if not GSTIN_RE.match(g):
        return False
    total = 0
    for i, ch in enumerate(g[:14]):
        prod = GSTIN_CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += prod // 36 + prod % 36
    return GSTIN_CHARS[(36 - total % 36) % 36] == g[14]


def ifsc_valid(code: str) -> bool:
    return bool(IFSC_RE.match(norm_account(code)))


def domain_valid(d: str) -> bool:
    return bool(DOMAIN_RE.match((d or "").lower()))
