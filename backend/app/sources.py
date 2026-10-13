"""Clients for the live external data sources. Each call has a timeout, a small TTL
cache and raises SourceUnavailable on failure so agents can report it honestly."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import dns.asyncresolver
import dns.exception
import dns.resolver
import httpx
from rapidfuzz import fuzz

from .config import Settings

log = logging.getLogger(__name__)
UA = {"User-Agent": "verify-before-wire/1.0 (payment fraud checkpoint)"}


class SourceUnavailable(Exception):
    pass


class _TTLCache:
    def __init__(self, ttl: float):
        self.ttl, self.data = ttl, {}

    def get(self, k):
        v = self.data.get(k)
        return v[1] if v and time.monotonic() - v[0] < self.ttl else None

    def put(self, k, v):
        self.data[k] = (time.monotonic(), v)
        return v


# ------------------------------------------------------------------- RDAP ----
@dataclass
class DomainRegistration:
    domain: str
    found: bool
    registered_on: datetime | None = None
    registrar: str | None = None

    @property
    def age_days(self) -> int | None:
        if not self.registered_on:
            return None
        return (datetime.now(timezone.utc) - self.registered_on).days


class RdapClient:
    """Domain registration data via the IANA RDAP bootstrap redirector (rdap.org)."""
    name = "RDAP (rdap.org)"

    def __init__(self, s: Settings, http: httpx.AsyncClient):
        self.base, self.http, self.cache = s.rdap_base.rstrip("/"), http, _TTLCache(3600)

    async def lookup(self, domain: str) -> DomainRegistration:
        domain = domain.lower().strip(".")
        if (hit := self.cache.get(domain)) is not None:
            return hit
        try:
            r = await self.http.get(f"{self.base}/domain/{domain}", follow_redirects=True)
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"RDAP unreachable: {e}") from e
        if r.status_code == 404:
            return self.cache.put(domain, DomainRegistration(domain, found=False))
        if r.status_code >= 400:
            raise SourceUnavailable(f"RDAP HTTP {r.status_code}")
        body = r.json()
        reg = None
        for ev in body.get("events", []):
            if ev.get("eventAction") == "registration":
                reg = datetime.fromisoformat(ev["eventDate"].replace("Z", "+00:00"))
        registrar = None
        for ent in body.get("entities", []):
            if "registrar" in ent.get("roles", []):
                vcard = ent.get("vcardArray", [None, []])[1]
                registrar = next((v[3] for v in vcard if v[0] == "fn"), None)
        return self.cache.put(domain, DomainRegistration(domain, True, reg, registrar))


# -------------------------------------------------------------------- DNS ----
@dataclass
class DomainDns:
    domain: str
    exists: bool
    mx: list[str]
    spf: str | None
    dmarc: str | None

    @property
    def dmarc_policy(self) -> str | None:
        if not self.dmarc:
            return None
        for part in self.dmarc.split(";"):
            k, _, v = part.strip().partition("=")
            if k.lower() == "p":
                return v.lower()
        return None


class DnsClient:
    name = "Public DNS (MX, SPF, DMARC)"

    def __init__(self, s: Settings):
        self.resolver = dns.asyncresolver.Resolver()
        self.resolver.lifetime = s.http_timeout_s
        self.cache = _TTLCache(900)

    async def _txt(self, name: str) -> list[str]:
        try:
            ans = await self.resolver.resolve(name, "TXT")
            return [b"".join(r.strings).decode(errors="replace") for r in ans]
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            return []

    async def inspect(self, domain: str) -> DomainDns:
        domain = domain.lower().strip(".")
        if (hit := self.cache.get(domain)) is not None:
            return hit
        try:
            exists = True
            try:
                mx_ans = await self.resolver.resolve(domain, "MX")
                mx = sorted(str(r.exchange).rstrip(".") for r in mx_ans)
            except dns.resolver.NXDOMAIN:
                exists, mx = False, []
            except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
                mx = []
            txt = await self._txt(domain) if exists else []
            dmarc = await self._txt(f"_dmarc.{domain}") if exists else []
        except dns.exception.Timeout as e:
            raise SourceUnavailable("DNS timeout") from e
        spf = next((t for t in txt if t.lower().startswith("v=spf1")), None)
        dm = next((t for t in dmarc if t.lower().startswith("v=dmarc1")), None)
        return self.cache.put(domain, DomainDns(domain, exists, mx, spf, dm))


# --------------------------------------------------------------------- FX ----
class FxClient:
    """ECB reference rates via Frankfurter."""
    name = "ECB reference rates (Frankfurter)"

    def __init__(self, s: Settings, http: httpx.AsyncClient):
        self.base, self.http, self.cache = s.fx_api_base.rstrip("/"), http, _TTLCache(3600)

    async def rate(self, from_ccy: str, to_ccy: str) -> float:
        from_ccy, to_ccy = from_ccy.upper(), to_ccy.upper()
        if from_ccy == to_ccy:
            return 1.0
        key = f"{from_ccy}{to_ccy}"
        if (hit := self.cache.get(key)) is not None:
            return hit
        try:
            r = await self.http.get(f"{self.base}/latest", params={"from": from_ccy, "to": to_ccy},
                                    follow_redirects=True)
            r.raise_for_status()
            return self.cache.put(key, float(r.json()["rates"][to_ccy]))
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise SourceUnavailable(f"FX rate {key} unavailable: {e}") from e


# -------------------------------------------------------------------- IFSC ----
@dataclass
class IfscInfo:
    code: str
    found: bool
    bank: str | None = None
    branch: str | None = None
    city: str | None = None


class IfscClient:
    """Indian bank branch directory via Razorpay's open-source IFSC API (data from RBI)."""
    name = "IFSC directory (ifsc.razorpay.com, RBI data)"

    def __init__(self, s: Settings, http: httpx.AsyncClient):
        self.base, self.http, self.cache = s.ifsc_api_base.rstrip("/"), http, _TTLCache(86400)

    async def lookup(self, code: str) -> IfscInfo:
        code = code.upper().strip()
        if (hit := self.cache.get(code)) is not None:
            return hit
        try:
            r = await self.http.get(f"{self.base}/{code}")
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"IFSC directory unreachable: {e}") from e
        if r.status_code == 404:
            return self.cache.put(code, IfscInfo(code, False))
        if r.status_code >= 400:
            raise SourceUnavailable(f"IFSC directory HTTP {r.status_code}")
        d = r.json()
        return self.cache.put(code, IfscInfo(code, True, d.get("BANK"), d.get("BRANCH"), d.get("CITY")))


# -------------------------------------------------------------- sanctions ----
@dataclass
class SanctionsHit:
    name: str
    matched: str
    score: float
    lists: str
    ref: str


class SanctionsClient:
    """OpenSanctions /match when an API key is configured, otherwise the US Treasury
    OFAC SDN list downloaded once per instance and matched with rapidfuzz."""

    def __init__(self, s: Settings, http: httpx.AsyncClient):
        self.s, self.http = s, http
        self._sdn: list[tuple[str, str, str]] | None = None
        self._un: list[tuple[str, str, str]] | None = None
        self._sdn_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "OpenSanctions" if self.s.opensanctions_api_key else "OFAC SDN + UN Security Council lists"

    async def screen(self, names: list[str], country: str | None = None) -> list[SanctionsHit]:
        names = [n for n in dict.fromkeys(n.strip() for n in names if n and n.strip())]
        if not names:
            return []
        if self.s.opensanctions_api_key:
            return await self._opensanctions(names, country)
        return await self._ofac(names)

    async def _opensanctions(self, names: list[str], country: str | None) -> list[SanctionsHit]:
        queries = {}
        for i, n in enumerate(names):
            props = {"name": [n]}
            if country:
                props["country"] = [country.lower()]
            queries[f"q{i}"] = {"schema": "LegalEntity", "properties": props}
        try:
            r = await self.http.post(
                f"{self.s.opensanctions_base}/match/sanctions",
                json={"queries": queries},
                headers={"Authorization": f"ApiKey {self.s.opensanctions_api_key}"},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"OpenSanctions unavailable: {e}") from e
        hits = []
        for i, n in enumerate(names):
            for res in r.json().get("responses", {}).get(f"q{i}", {}).get("results", []):
                if res.get("match"):
                    hits.append(SanctionsHit(n, res.get("caption", ""), float(res.get("score", 0)),
                                             ", ".join(res.get("datasets", [])[:4]),
                                             f"https://www.opensanctions.org/entities/{res.get('id')}/"))
        return hits

    async def _load_sdn(self) -> list[tuple[str, str, str]]:
        async with self._sdn_lock:
            if self._sdn is not None:
                return self._sdn
            try:
                r = await self.http.get(self.s.ofac_sdn_url, follow_redirects=True, timeout=30)
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise SourceUnavailable(f"OFAC SDN list unavailable: {e}") from e
            rows = []
            for row in csv.reader(io.StringIO(r.text)):
                if len(row) >= 4 and row[1] and row[1] != "-0-":
                    rows.append((row[0], row[1].strip(), row[3].strip()))  # ent_num, name, program
            self._sdn = rows
            log.info("Loaded %d OFAC SDN entries", len(rows))
            return rows

    async def _load_un(self) -> list[tuple[str, str, str]]:
        """UN Security Council consolidated list (XML, parsed with defusedxml)."""
        if self._un is not None:
            return self._un
        from defusedxml import ElementTree as DET
        try:
            r = await self.http.get(self.s.un_sanctions_url, follow_redirects=True, timeout=30)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"UN sanctions list unavailable: {e}") from e
        root = DET.fromstring(r.content)
        rows = []
        for kind in ("INDIVIDUAL", "ENTITY"):
            for el in root.iter(kind):
                ref = (el.findtext("REFERENCE_NUMBER") or "").strip()
                parts = [el.findtext(t) for t in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")]
                name = " ".join(p.strip() for p in parts if p and p.strip())
                if name:
                    rows.append((ref, name, "UN SC"))
                for al in el.iter(f"{kind}_ALIAS"):
                    a = (al.findtext("ALIAS_NAME") or "").strip()
                    if a:
                        rows.append((ref, a, "UN SC alias"))
        self._un = rows
        return rows

    async def _ofac(self, names: list[str]) -> list[SanctionsHit]:
        sdn = list(await self._load_sdn())
        try:
            sdn += await self._load_un()
        except SourceUnavailable as e:
            log.warning("%s; screening against OFAC only", e)
        hits = []
        for n in names:
            for ent, sdn_name, program in sdn:
                score = fuzz.token_sort_ratio(n.upper(), sdn_name.upper())
                if score >= 92:
                    src = program if program.startswith("UN") else f"OFAC SDN ({program})"
                    hits.append(SanctionsHit(n, sdn_name, score / 100, src, f"{src} ref {ent}"))
        return sorted(hits, key=lambda h: -h.score)[:5]


@dataclass
class Sources:
    rdap: RdapClient
    dns: DnsClient
    fx: FxClient
    sanctions: SanctionsClient
    http: httpx.AsyncClient
    ifsc: IfscClient | None = None

    @classmethod
    def build(cls, s: Settings) -> "Sources":
        http = httpx.AsyncClient(timeout=s.http_timeout_s, headers=UA)
        return cls(RdapClient(s, http), DnsClient(s), FxClient(s, http), SanctionsClient(s, http), http,
                   IfscClient(s, http))

    async def aclose(self):
        await self.http.aclose()
