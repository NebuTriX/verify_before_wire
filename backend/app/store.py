"""Persistence. FirestoreStore + Cloud Storage in production, MemoryStore locally.

Firestore layout
  cases/{case_id}                    case document (status, request, findings, verdict)
  cases/{case_id}/events/{seq:06d}   every streamed event, so any instance can replay/tail
  vendors/{vendor_id}                vendor master (seeded by scripts/seed_firestore.py)
  payments/{payment_id}              payment history, queried by vendor_id
"""
from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from .config import Settings
from .models import Case, CaseStatus, Event, Payment, Vendor

log = logging.getLogger(__name__)


class Store(ABC):
    @abstractmethod
    async def create_case(self, case: Case) -> None: ...
    @abstractmethod
    async def get_case(self, case_id: str) -> Case | None: ...
    @abstractmethod
    async def save_case(self, case: Case) -> None: ...
    @abstractmethod
    async def try_claim(self, case_id: str, lease_seconds: int) -> bool:
        """Atomically mark a queued (or stale running) case as running. True if we own it."""
    @abstractmethod
    async def append_event(self, case_id: str, event: Event) -> None: ...
    @abstractmethod
    async def events_after(self, case_id: str, seq: int) -> list[Event]: ...
    @abstractmethod
    async def list_vendors(self) -> list[Vendor]: ...
    @abstractmethod
    async def payments_for_vendor(self, vendor_id: str) -> list[Payment]: ...
    @abstractmethod
    async def put_blob(self, key: str, data: bytes, mime: str) -> str: ...
    @abstractmethod
    async def get_blob(self, uri: str) -> bytes: ...


def _claimable(status: str, lease_until: datetime | None, now: datetime) -> bool:
    if status == CaseStatus.queued.value:
        return True
    return status == CaseStatus.running.value and (lease_until is None or lease_until < now)


# ------------------------------------------------------------------ memory ---
class MemoryStore(Store):
    def __init__(self, seed: dict | None = None):
        self.cases: dict[str, Case] = {}
        self.events: dict[str, list[Event]] = {}
        self.blobs: dict[str, bytes] = {}
        self.vendors = [Vendor(**v) for v in (seed or {}).get("vendors", [])]
        self.payments = [Payment(**p) for p in (seed or {}).get("payments", [])]
        self._lock = asyncio.Lock()

    async def create_case(self, case: Case) -> None:
        self.cases[case.id] = case.model_copy(deep=True)
        self.events[case.id] = []

    async def get_case(self, case_id: str) -> Case | None:
        c = self.cases.get(case_id)
        return c.model_copy(deep=True) if c else None

    async def save_case(self, case: Case) -> None:
        case.updated_at = datetime.now(timezone.utc)
        self.cases[case.id] = case.model_copy(deep=True)

    async def try_claim(self, case_id: str, lease_seconds: int) -> bool:
        async with self._lock:
            c = self.cases.get(case_id)
            now = datetime.now(timezone.utc)
            if not c or not _claimable(c.status.value, c.lease_until, now):
                return False
            c.status = CaseStatus.running
            c.lease_until = now + timedelta(seconds=lease_seconds)
            return True

    async def append_event(self, case_id: str, event: Event) -> None:
        self.events.setdefault(case_id, []).append(event)

    async def events_after(self, case_id: str, seq: int) -> list[Event]:
        return [e for e in self.events.get(case_id, []) if e.seq > seq]

    async def list_vendors(self) -> list[Vendor]:
        return list(self.vendors)

    async def payments_for_vendor(self, vendor_id: str) -> list[Payment]:
        return [p for p in self.payments if p.vendor_id == vendor_id]

    async def put_blob(self, key: str, data: bytes, mime: str) -> str:
        self.blobs[key] = data
        return f"mem://{key}"

    async def get_blob(self, uri: str) -> bytes:
        return self.blobs[uri.removeprefix("mem://")]


# --------------------------------------------------------------- firestore ---
class FirestoreStore(Store):
    def __init__(self, settings: Settings):
        from google.cloud import firestore  # imported lazily so tests need no credentials
        self._fs = firestore
        self.db = firestore.AsyncClient(
            project=settings.google_cloud_project or None, database=settings.firestore_database
        )
        self.bucket_name = settings.uploads_bucket
        self._gcs = None
        self._mem_blobs: dict[str, bytes] = {}
        if not self.bucket_name:
            log.warning("UPLOADS_BUCKET not set: uploads live in memory; run Cloud Run with max 1 instance")

    def _case(self, case_id: str):
        return self.db.collection("cases").document(case_id)

    async def create_case(self, case: Case) -> None:
        await self._case(case.id).set(case.model_dump(mode="json"))

    async def get_case(self, case_id: str) -> Case | None:
        snap = await self._case(case_id).get()
        return Case(**snap.to_dict()) if snap.exists else None

    async def save_case(self, case: Case) -> None:
        case.updated_at = datetime.now(timezone.utc)
        await self._case(case.id).set(case.model_dump(mode="json"))

    async def try_claim(self, case_id: str, lease_seconds: int) -> bool:
        ref = self._case(case_id)
        fs = self._fs

        @fs.async_transactional
        async def _claim(tx) -> bool:
            snap = await ref.get(transaction=tx)
            if not snap.exists:
                return False
            d = snap.to_dict()
            lease = d.get("lease_until")
            lease_dt = datetime.fromisoformat(lease) if isinstance(lease, str) else lease
            now = datetime.now(timezone.utc)
            if not _claimable(d.get("status"), lease_dt, now):
                return False
            tx.update(ref, {
                "status": CaseStatus.running.value,
                "lease_until": (now + timedelta(seconds=lease_seconds)).isoformat(),
            })
            return True

        return await _claim(self.db.transaction())

    async def append_event(self, case_id: str, event: Event) -> None:
        await self._case(case_id).collection("events").document(f"{event.seq:06d}").set(
            event.model_dump(mode="json")
        )

    async def events_after(self, case_id: str, seq: int) -> list[Event]:
        q = (self._case(case_id).collection("events")
             .where(filter=self._fs.FieldFilter("seq", ">", seq)).order_by("seq"))
        return [Event(**d.to_dict()) async for d in q.stream()]

    async def list_vendors(self) -> list[Vendor]:
        return [Vendor(**d.to_dict()) async for d in self.db.collection("vendors").stream()]

    async def payments_for_vendor(self, vendor_id: str) -> list[Payment]:
        q = self.db.collection("payments").where(
            filter=self._fs.FieldFilter("vendor_id", "==", vendor_id))
        return [Payment(**d.to_dict()) async for d in q.stream()]

    def _bucket(self):
        if self._gcs is None:
            from google.cloud import storage
            self._gcs = storage.Client().bucket(self.bucket_name)
        return self._gcs

    async def put_blob(self, key: str, data: bytes, mime: str) -> str:
        if not self.bucket_name:
            self._mem_blobs[key] = data
            return f"mem://{key}"
        blob = self._bucket().blob(key)
        await asyncio.to_thread(blob.upload_from_string, data, content_type=mime)
        return f"gs://{self.bucket_name}/{key}"

    async def get_blob(self, uri: str) -> bytes:
        if uri.startswith("mem://"):
            return self._mem_blobs[uri.removeprefix("mem://")]
        key = uri.split("/", 3)[3]
        return await asyncio.to_thread(self._bucket().blob(key).download_as_bytes)


def build_store(settings: Settings) -> Store:
    if settings.store_backend == "memory":
        seed = json.loads(settings.seed_file.read_text()) if settings.seed_file.exists() else {}
        return MemoryStore(seed)
    return FirestoreStore(settings)
