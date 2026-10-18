"""Official sanctions lists, downloaded from the publishing governments and refreshed on a
schedule. Every snapshot is cached on disk with its SHA-256 and publication date, so each
decision can say exactly which version of which list it was screened against.

Lists (all free, published by the authorities themselves):
  OFAC SDN   US Treasury, Specially Designated Nationals       (CSV)
  UN SC      UN Security Council Consolidated List              (XML)
  EU FSF     EU Financial Sanctions Files, consolidated list    (CSV)
  UK         FCDO UK Sanctions List (successor to OFSI's list)  (CSV)
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

log = logging.getLogger(__name__)


@dataclass
class Entry:
    ref: str
    name: str
    program: str


@dataclass
class Snapshot:
    key: str
    label: str
    entries: list[Entry]
    fetched_at: str                  # when we downloaded it (ISO, UTC)
    published: str | None            # the publisher's own date, when the file states one
    sha256: str
    url: str
    choices: list[str] = field(default_factory=list, repr=False)   # normalised names, same order as entries

    def meta(self) -> dict:
        return {"key": self.key, "label": self.label, "entries": len(self.entries), "fetched_at": self.fetched_at,
                "published": self.published, "sha256": self.sha256, "url": self.url}


def normalise(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", name.upper())).strip()


# ---------------------------------------------------------------- parsers ----
def parse_ofac(raw: bytes) -> tuple[list[Entry], str | None]:
    rows = []
    for r in csv.reader(io.StringIO(raw.decode("utf-8", errors="replace"))):
        if len(r) >= 4 and r[1] and r[1] != "-0-":
            rows.append(Entry(r[0], r[1].strip(), f"OFAC SDN ({r[3].strip()})"))
    return rows, None


def parse_un(raw: bytes) -> tuple[list[Entry], str | None]:
    from defusedxml import ElementTree as DET
    root = DET.fromstring(raw)
    rows = []
    for kind in ("INDIVIDUAL", "ENTITY"):
        for el in root.iter(kind):
            ref = (el.findtext("REFERENCE_NUMBER") or "").strip()
            parts = [el.findtext(t) for t in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")]
            name = " ".join(p.strip() for p in parts if p and p.strip())
            if name:
                rows.append(Entry(ref, name, "UN SC"))
            for al in el.iter(f"{kind}_ALIAS"):
                if a := (al.findtext("ALIAS_NAME") or "").strip():
                    rows.append(Entry(ref, a, "UN SC (alias)"))
    return rows, (root.get("dateGenerated") or "")[:10] or None


def parse_eu(raw: bytes) -> tuple[list[Entry], str | None]:
    text = raw.decode("utf-8-sig", errors="replace")
    rows, seen, published = [], set(), None
    for r in csv.DictReader(io.StringIO(text), delimiter=";"):
        if not published and (g := (r.get("fileGenerationDate") or "").strip()):
            m = re.match(r"(\d{2})/(\d{2})/(\d{4})", g)
            published = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else g[:10]
        name = (r.get("NameAlias_WholeName") or "").strip()
        ref = (r.get("Entity_EU_ReferenceNumber") or r.get("Entity_LogicalId") or "").strip()
        if name and (ref, name) not in seen:
            seen.add((ref, name))
            rows.append(Entry(ref, name, f"EU FSF ({(r.get('Entity_Regulation_Programme') or 'consolidated').strip()})"))
    return rows, published


def parse_uk(raw: bytes) -> tuple[list[Entry], str | None]:
    text = raw.decode("utf-8-sig", errors="replace")
    first, _, rest = text.partition("\n")
    published = None
    if m := re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", first):
        published = datetime.strptime(m.group(1), "%d-%b-%Y").date().isoformat()
    rows, seen = [], set()
    for r in csv.DictReader(io.StringIO(rest)):
        name = " ".join(x.strip() for x in (r.get(f"Name {i}") or "" for i in (1, 2, 3, 4, 5, 6)) if x and x.strip())
        ref = (r.get("Unique ID") or r.get("OFSI Group ID") or "").strip()
        if name and (ref, name) not in seen:
            seen.add((ref, name))
            rows.append(Entry(ref, name, f"UK ({(r.get('Regime Name') or 'sanctions').strip()})"))
    return rows, published


@dataclass
class ListSpec:
    key: str
    label: str
    url: str
    parse: Callable[[bytes], tuple[list[Entry], str | None]]
    timeout_s: float


class SanctionsLists:
    """Holds the latest snapshot of each list. A snapshot older than `max_age_h` is refreshed in
    the background while the old one keeps serving, so screening never waits on a slow server."""

    def __init__(self, specs: list[ListSpec], http: httpx.AsyncClient, cache_dir: Path | None, max_age_h: float):
        self.specs = {s.key: s for s in specs}
        self.http, self.cache_dir, self.max_age_s = http, cache_dir, max_age_h * 3600
        self.snapshots: dict[str, Snapshot] = {}
        self.errors: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._loaded_at: dict[str, float] = {}
        self._disk_lock = asyncio.Lock()

    # ---- disk cache -------------------------------------------------------
    def _paths(self, key: str) -> tuple[Path, Path] | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / f"{key}.raw", self.cache_dir / f"{key}.json"

    def _from_disk(self, key: str) -> bool:
        p = self._paths(key)
        if not p or not p[0].exists() or not p[1].exists():
            return False
        try:
            meta = json.loads(p[1].read_text())
            raw = p[0].read_bytes()
            if hashlib.sha256(raw).hexdigest() != meta["sha256"]:
                return False
            self._install(key, raw, meta["fetched_at"], age_s=time.time() - meta["fetched_epoch"])
            return True
        except Exception as e:  # a corrupt cache just means downloading again
            log.warning("Ignoring cached %s list: %s", key, e)
            return False

    def _install(self, key: str, raw: bytes, fetched_at: str, age_s: float = 0.0) -> Snapshot:
        spec = self.specs[key]
        entries, published = spec.parse(raw)
        if not entries:
            raise ValueError(f"{spec.label}: the file parsed to 0 names, refusing to replace the list")
        snap = Snapshot(key, spec.label, entries, fetched_at, published, hashlib.sha256(raw).hexdigest(), spec.url,
                        [normalise(e.name) for e in entries])
        self.snapshots[key] = snap
        self._loaded_at[key] = time.time() - age_s
        self.errors.pop(key, None)
        log.info("Sanctions list %s: %d names (published %s)", spec.label, len(entries), published or "n/a")
        return snap

    async def _download(self, key: str) -> Snapshot:
        spec = self.specs[key]
        try:
            r = await self.http.get(spec.url, follow_redirects=True, timeout=spec.timeout_s)
            r.raise_for_status()
        except httpx.HTTPError as e:
            self.errors[key] = f"download failed: {e}"
            raise
        raw, now = r.content, datetime.now(timezone.utc)
        try:
            snap = await asyncio.to_thread(self._install, key, raw, now.isoformat(timespec="seconds"))
        except Exception as e:             # malformed file: keep the previous snapshot, report the error
            self.errors[key] = f"could not read the file: {type(e).__name__}: {e}"[:300]
            raise
        if p := self._paths(key):
            try:
                p[0].parent.mkdir(parents=True, exist_ok=True)
                p[0].write_bytes(raw)
                p[1].write_text(json.dumps({"sha256": snap.sha256, "fetched_at": snap.fetched_at,
                                            "fetched_epoch": now.timestamp()}))
            except OSError as e:
                log.warning("Could not cache %s list on disk: %s", key, e)
        return snap

    # ---- public -----------------------------------------------------------
    def task(self, key: str) -> asyncio.Task | None:
        """The shared download for one list, started when the list is missing or stale."""
        fresh = key in self.snapshots and time.time() - self._loaded_at.get(key, 0) < self.max_age_s
        if fresh:
            return None
        t = self._tasks.get(key)
        if t is None or t.done():
            t = self._tasks[key] = asyncio.create_task(self._download(key))
            t.add_done_callback(lambda x: x.cancelled() or x.exception())   # never "exception was never retrieved"
        return t

    async def ensure(self, key: str, wait_s: float) -> Snapshot | None:
        """The list to screen against: the current snapshot (refreshing in the background if
        stale), or, when there is none yet, wait up to wait_s for the first download."""
        if key not in self.snapshots:
            await self._load_cached(key)
        t = self.task(key)
        if key in self.snapshots:
            return self.snapshots[key]
        if t is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(t), wait_s)
        except Exception:                  # timeout, network or a malformed file: screen without this list
            return self.snapshots.get(key)

    async def _load_cached(self, key: str) -> None:
        async with self._disk_lock:            # parse a cached file once, off the event loop
            if key not in self.snapshots:
                await asyncio.to_thread(self._from_disk, key)

    async def warm(self) -> None:
        for key in self.specs:
            await self._load_cached(key)
        await asyncio.gather(*(t for k in self.specs if (t := self.task(k))), return_exceptions=True)

    def status(self) -> list[dict]:
        out = []
        for key, spec in self.specs.items():
            snap = self.snapshots.get(key)
            loading = bool((t := self._tasks.get(key)) and not t.done())
            out.append({**(snap.meta() if snap else {"key": key, "label": spec.label, "entries": 0, "url": spec.url}),
                        "loaded": snap is not None, "refreshing": loading, "error": self.errors.get(key)})
        return out

