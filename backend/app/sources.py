"""Clients for the live external data sources. Each call has a timeout, a small TTL
cache and raises SourceUnavailable on failure so agents can report it honestly."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import dns.asyncresolver
import dns.exception
import dns.resolver
import httpx
from rapidfuzz import fuzz, process

from .config import Settings
from .sanctions_lists import ListSpec, SanctionsLists, normalise, parse_eu, parse_ofac, parse_uk, parse_un
from .trace import cache_hit, traced

if TYPE_CHECKING:
    from .registries import GleifClient, ViesClient

log = logging.getLogger(__name__)
UA = {"User-Agent": "verify-before-wire/1.0 (payment fraud checkpoint)"}


class SourceUnavailable(Exception):
    pass


class TTLCache:
    def __init__(self, ttl: float):
        self.ttl, self.data = ttl, {}

    def get(self, k):
        v = self.data.get(k)
        if v and time.monotonic() - v[0] < self.ttl:
            cache_hit()
            return v[1]
        return None

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
        self.base, self.http, self.cache = s.rdap_base.rstrip("/"), http, TTLCache(3600)

    @traced("rdap", lambda domain: domain.lower().strip("."),
            lambda r: (f"registered {r.registered_on:%d %b %Y} ({r.age_days} days)" if r.registered_on
                       else "registered, no creation date") if r.found else "not registered")
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
        self.cache = TTLCache(900)

    async def _txt(self, name: str) -> list[str]:
        try:
            ans = await self.resolver.resolve(name, "TXT")
            return [b"".join(r.strings).decode(errors="replace") for r in ans]
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            return []

    @traced("dns", lambda domain: domain.lower().strip("."),
            lambda d: ", ".join([f"{len(d.mx)} MX", "SPF" if d.spf else "no SPF",
                                 f"DMARC p={d.dmarc_policy}" if d.dmarc else "no DMARC"]) if d.exists else "no such domain")
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
        self.base, self.http, self.cache = s.fx_api_base.rstrip("/"), http, TTLCache(3600)

    @traced("fx", lambda f, t: None if f.upper() == t.upper() else f"{f.upper()} → {t.upper()}",
            lambda r: f"rate {r:.4f}")
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
        self.base, self.http, self.cache = s.ifsc_api_base.rstrip("/"), http, TTLCache(86400)

    @traced("ifsc", lambda code: code.upper().strip(),
            lambda i: f"{i.bank}, {i.branch}" if i.found else "code not found")
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
    name: str          # the name we screened
    matched: str       # the listed name it matched
    score: float       # 0..1 similarity
    lists: str         # programme, e.g. "OFAC SDN (IRAN)"
    ref: str           # human-readable reference, e.g. "OFAC SDN ref 25578"
    list_key: str = "" # which list ("ofac", "un", "eu", "uk"); one best hit per list and name


MATCH_MIN_SCORE = 92     # token-sort similarity (0-100) that counts as a possible match for compliance review
MATCHES_PER_LIST = 3     # candidates kept per list and name
MAX_HITS = 5             # possible matches reported per screening
MATCH_CACHE_MAX = 20000  # cached names per process before the cache is cleared


OFAC_WAIT_S = 25   # with no OFAC snapshot yet, a case waits this long for the first download
OTHER_WAIT_S = 4   # ...and this long for the other lists; a list still loading is reported, not silently skipped


class SanctionsClient:
    """OpenSanctions /match when an API key is configured; otherwise the official government
    lists (OFAC SDN, UN SC, EU FSF, UK), refreshed on a schedule and matched with rapidfuzz.
    OFAC is mandatory: if it cannot be loaded the check fails closed (SourceUnavailable)."""

    def __init__(self, s: Settings, http: httpx.AsyncClient):
        self.s, self.http = s, http
        specs = {"ofac": ListSpec("ofac", "OFAC SDN", s.ofac_sdn_url, parse_ofac, timeout_s=60),
                 "un": ListSpec("un", "UN Security Council", s.un_sanctions_url, parse_un, timeout_s=60),
                 "eu": ListSpec("eu", "EU consolidated (FSF)", s.eu_sanctions_url, parse_eu, timeout_s=240),
                 "uk": ListSpec("uk", "UK Sanctions List (FCDO)", s.uk_sanctions_url, parse_uk, timeout_s=120)}
        keys = [k.strip() for k in s.sanctions_lists.split(",") if k.strip() in specs]
        if "ofac" not in keys:
            keys.insert(0, "ofac")
        cache = s.sanctions_cache_dir if str(s.sanctions_cache_dir) not in ("", ".") else None
        self.lists = SanctionsLists([specs[k] for k in keys], http, cache, s.sanctions_refresh_hours)
        # name -> matches, valid only for the exact list snapshots it was computed on (the key holds
        # their SHA-256), so a refreshed list can never serve a stale answer.
        self._match_cache: dict[tuple[str, tuple[str, ...]], list[SanctionsHit]] = {}

    @property
    def name(self) -> str:
        if self.s.opensanctions_api_key:
            return "OpenSanctions"
        loaded = [x.label for x in self.lists.snapshots.values()]
        return "Sanctions lists: " + (", ".join(loaded) if loaded else "OFAC SDN (loading)")

    @traced("sanctions", lambda names, country=None: ", ".join(dict.fromkeys(n.strip() for n in names if n and n.strip())) or None,
            lambda r: (f"{len(r[0])} possible match{'es' if len(r[0]) != 1 else ''}" if r[0] else "no matches")
            + f" across {len(r[1])} list{'s' if len(r[1]) != 1 else ''}")
    async def screen_with_coverage(self, names: list[str], country: str | None = None) -> tuple[list[SanctionsHit], list[dict]]:
        """Matches plus the exact list snapshots screened (label, publication date, SHA-256)."""
        names = [n for n in dict.fromkeys(n.strip() for n in names if n and n.strip())]
        if not names:
            return [], []
        if self.s.opensanctions_api_key:
            return await self._opensanctions(names, country), [{"label": "OpenSanctions (API)", "key": "opensanctions"}]
        return await self._official(names)

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

    async def warm(self) -> None:
        """Load cached snapshots and start any due downloads at startup, so cases don't wait on them."""
        if not self.s.opensanctions_api_key:
            await self.lists.warm()

    def status(self) -> list[dict]:
        return self.lists.status()

    async def _official(self, names: list[str]) -> tuple[list[SanctionsHit], list[dict]]:
        keys = list(self.lists.specs)
        snaps = await asyncio.gather(*(self.lists.ensure(k, OFAC_WAIT_S if k == "ofac" else OTHER_WAIT_S) for k in keys))
        by_key = dict(zip(keys, snaps, strict=True))
        if by_key.get("ofac") is None:
            raise SourceUnavailable("OFAC SDN list is not available yet " + (self.lists.errors.get("ofac") or "(still downloading)"))
        coverage = [{**sn.meta(), "screened": True} for sn in snaps if sn is not None]
        coverage += [{"key": k, "label": self.lists.specs[k].label, "screened": False,
                      "error": self.lists.errors.get(k) or "still downloading"} for k, sn in by_key.items() if sn is None]

        versions = tuple(sn.sha256 for sn in snaps if sn is not None)

        def match_one(n: str) -> list[SanctionsHit]:
            key = (normalise(n), versions)
            if (hit := self._match_cache.get(key)) is not None:
                return hit
            found = []
            q = key[0]
            for sn in snaps:
                if sn is None:
                    continue
                for _, score, idx in process.extract(q, sn.choices, scorer=fuzz.token_sort_ratio,
                                                     score_cutoff=MATCH_MIN_SCORE, limit=MATCHES_PER_LIST):
                    e = sn.entries[idx]
                    found.append(SanctionsHit(n, e.name, score / 100, e.program, f"{sn.label} ref {e.ref}", sn.key))
            if len(self._match_cache) > MATCH_CACHE_MAX:
                self._match_cache.clear()
            self._match_cache[key] = found
            return found

        def match() -> list[SanctionsHit]:
            hits = [h for n in names for h in match_one(n)]
            best: dict[tuple[str, str], SanctionsHit] = {}
            for h in hits:                      # the same person on several lists: keep each list's best
                k = (h.name, h.list_key)
                if k not in best or h.score > best[k].score:
                    best[k] = h
            return sorted(best.values(), key=lambda h: -h.score)[:MAX_HITS]
        return await asyncio.to_thread(match), coverage


@dataclass
class Sources:
    rdap: RdapClient
    dns: DnsClient
    fx: FxClient
    sanctions: SanctionsClient
    http: httpx.AsyncClient
    ifsc: IfscClient | None = None
    gleif: "GleifClient | None" = None
    vies: "ViesClient | None" = None

    @classmethod
    def build(cls, s: Settings) -> "Sources":
        http = httpx.AsyncClient(timeout=s.http_timeout_s, headers=UA)
        from .registries import GleifClient, ViesClient
        return cls(RdapClient(s, http), DnsClient(s), FxClient(s, http), SanctionsClient(s, http), http,
                   IfscClient(s, http), GleifClient(s, http), ViesClient(s, http))

    async def aclose(self):
        await self.http.aclose()
