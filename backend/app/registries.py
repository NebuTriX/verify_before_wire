"""Official registries used to verify who is being paid and through which bank.

GLEIF  Global Legal Entity Identifier Foundation (G20/FSB-mandated, free, no key). Every
       regulated bank and most large companies have an LEI; GLEIF also maps SWIFT BICs to LEIs,
       so a BIC can be checked against the legal entity that really owns it.
VIES   The European Commission's VAT Information Exchange System: is this EU VAT number valid?
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from rapidfuzz import fuzz

from .config import Settings
from .sources import SourceUnavailable, TTLCache
from .trace import traced


@dataclass
class LegalEntity:
    lei: str
    legal_name: str
    country: str | None
    entity_status: str | None          # ACTIVE / INACTIVE (dissolved, merged...)
    registration_status: str | None    # ISSUED / LAPSED / RETIRED ...
    registered_on: str | None
    bics: list[str]
    match_score: float = 1.0

    @property
    def url(self) -> str:
        return f"https://search.gleif.org/#/record/{self.lei}"


def _entity(rec: dict, score: float = 1.0) -> LegalEntity:
    a = rec.get("attributes", {})
    e, reg = a.get("entity", {}), a.get("registration", {})
    return LegalEntity(rec.get("id", ""), (e.get("legalName") or {}).get("name", ""),
                       (e.get("legalAddress") or {}).get("country"), e.get("status"), reg.get("status"),
                       (reg.get("initialRegistrationDate") or "")[:10] or None, a.get("bic") or [], score)


class GleifClient:
    name = "GLEIF legal entity register"

    def __init__(self, s: Settings, http: httpx.AsyncClient):
        self.base, self.http, self.cache = s.gleif_base.rstrip("/"), http, TTLCache(86400)

    async def _get(self, params: dict) -> list[dict]:
        try:
            r = await self.http.get(f"{self.base}/lei-records", params={**params, "page[size]": 5},
                                    headers={"Accept": "application/vnd.api+json"})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"GLEIF unreachable: {e}") from e
        return r.json().get("data", [])

    @traced("gleif", lambda bic: f"BIC {bic.upper()}",
            lambda e: f"owned by {e.legal_name} ({e.country})" if e else "BIC not linked to an LEI")
    async def by_bic(self, bic: str) -> LegalEntity | None:
        bic = bic.upper().replace(" ", "")
        bic11 = bic + "XXX" if len(bic) == 8 else bic
        if (hit := self.cache.get(("bic", bic11))) is not None:
            return hit or None
        rows = await self._get({"filter[bic]": bic11})
        ent = _entity(rows[0]) if rows else None
        self.cache.put(("bic", bic11), ent or False)
        return ent

    @traced("gleif", lambda name, country=None: name.strip() or None,
            lambda e: f"{e.legal_name}, LEI {e.lei}, {e.entity_status}" if e else "no LEI record")
    async def by_name(self, name: str, country: str | None = None) -> LegalEntity | None:
        """Best LEI record whose legal name closely matches (>= 90/100) — the full-text search
        alone returns related entities, so the match is confirmed here."""
        key = ("name", name.upper().strip(), (country or "").upper())
        if (hit := self.cache.get(key)) is not None:
            return hit or None
        params = {"filter[fulltext]": name}
        if country:
            params["filter[entity.legalAddress.country]"] = country.upper()
        rows = await self._get(params)
        best = None
        for rec in rows:
            ent = _entity(rec)
            score = fuzz.token_sort_ratio(name.upper(), ent.legal_name.upper()) / 100
            if score >= 0.9 and (best is None or score > best.match_score):
                ent.match_score, best = score, ent
        self.cache.put(key, best or False)
        return best


@dataclass
class VatCheck:
    country: str
    number: str
    valid: bool
    name: str | None


class ViesClient:
    name = "EU VIES VAT register"
    EU = {"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR", "HR", "HU", "IE", "IT", "LT",
          "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK", "XI"}

    def __init__(self, s: Settings, http: httpx.AsyncClient):
        self.base, self.http, self.cache = s.vies_base.rstrip("/"), http, TTLCache(86400)

    @staticmethod
    def split(vat: str) -> tuple[str, str] | None:
        v = "".join(ch for ch in vat.upper() if ch.isalnum())
        cc = "EL" if v[:2] == "GR" else v[:2]
        return (cc, v[2:]) if cc in ViesClient.EU and len(v) > 4 else None

    @traced("vies", lambda vat: vat.upper().replace(" ", ""),
            lambda c: ("valid" + (f", registered to {c.name}" if c.name else "")) if c.valid else "NOT valid")
    async def check(self, vat: str) -> VatCheck:
        parts = self.split(vat)
        if not parts:
            raise ValueError(f"{vat} is not an EU VAT number")
        cc, num = parts
        if (hit := self.cache.get((cc, num))) is not None:
            return hit
        try:
            r = await self.http.get(f"{self.base}/ms/{cc}/vat/{num}")
            r.raise_for_status()
            d = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise SourceUnavailable(f"VIES unreachable: {e}") from e
        if d.get("userError") not in (None, "VALID", "INVALID"):       # e.g. MS_UNAVAILABLE: member state offline
            raise SourceUnavailable(f"VIES: {d.get('userError')}")
        name = d.get("name") if d.get("name") not in (None, "", "---") else None
        return self.cache.put((cc, num), VatCheck(cc, num, bool(d.get("isValid")), name))
