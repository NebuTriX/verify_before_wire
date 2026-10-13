"""Persistence, tenant-scoped. Backends implement six primitives (get, set, delete, query,
atomic, blobs); every domain method is written once on top of them.

Firestore layout
  tenants/{tid}                         workspace + settings + active policy version
  tenants/{tid}/members/{uid}           role per person
  tenants/{tid}/policies/{version}      immutable policy versions
  tenants/{tid}/vendors/{id}            vendor master (imported by the tenant)
  tenants/{tid}/payments/{id}           payment history (imported by the tenant)
  tenants/{tid}/cases/{cid}             decision records
  tenants/{tid}/cases/{cid}/events/{n}  streamed events
  tenants/{tid}/audit/{n}               hash-chained audit log
  tenants/{tid}/meta/{name}             counters (policy version, audit head)
  users/{uid}/memberships/{tid}         which workspaces a person belongs to
  api_keys/{prefix}                     key lookup -> tenant (hash only)
  invites/{token_hash}                  pending invitations (hash only)
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .audit import GENESIS, AuditEntry, seal
from .config import Settings
from .models import Case, CaseStatus, Event, Payment, Vendor
from .policy import Policy
from .tenancy import Invite, Member, Tenant

log = logging.getLogger(__name__)
Mutator = Callable[[dict | None], tuple[dict | None, Any]]


def _now():
    return datetime.now(timezone.utc)


def _dt(v):
    return datetime.fromisoformat(v) if isinstance(v, str) else v


class Store(ABC):
    # ------------------------------------------------------- primitives ----
    @abstractmethod
    async def get(self, path: str) -> dict | None: ...
    @abstractmethod
    async def set(self, path: str, data: dict) -> None: ...
    @abstractmethod
    async def delete(self, path: str) -> None: ...
    @abstractmethod
    async def query(self, coll: str, where: list[tuple[str, str, Any]] | None = None,
                    order_by: str | None = None, desc: bool = False, limit: int | None = None) -> list[dict]: ...
    @abstractmethod
    async def atomic(self, path: str, fn: Mutator) -> Any:
        """Read-modify-write one document in a transaction. fn returns (new_doc|None, result)."""
    @abstractmethod
    async def put_blob(self, key: str, data: bytes, mime: str) -> str: ...
    @abstractmethod
    async def get_blob(self, uri: str) -> bytes: ...

    async def batch_set(self, items: list[tuple[str, dict]]) -> None:
        for p, d in items:
            await self.set(p, d)

    # ---------------------------------------------------------- tenants ----
    async def create_tenant(self, t: Tenant, owner: Member) -> None:
        await self.set(f"tenants/{t.id}", t.model_dump(mode="json"))
        await self.put_member(t.id, owner, t.name)

    async def get_tenant(self, tid: str) -> Tenant | None:
        d = await self.get(f"tenants/{tid}")
        return Tenant(**d) if d else None

    async def save_tenant(self, t: Tenant) -> None:
        await self.set(f"tenants/{t.id}", t.model_dump(mode="json"))

    async def tenants_for(self, uid: str) -> list[dict]:
        return await self.query(f"users/{uid}/memberships")

    # ---------------------------------------------------------- members ----
    async def put_member(self, tid: str, m: Member, tenant_name: str) -> None:
        await self.set(f"tenants/{tid}/members/{m.uid}", m.model_dump(mode="json"))
        await self.set(f"users/{m.uid}/memberships/{tid}", {"tenant_id": tid, "name": tenant_name,
                                                             "role": m.role.value})

    async def get_member(self, tid: str, uid: str) -> Member | None:
        d = await self.get(f"tenants/{tid}/members/{uid}")
        return Member(**d) if d else None

    async def list_members(self, tid: str) -> list[Member]:
        return [Member(**d) for d in await self.query(f"tenants/{tid}/members")]

    async def remove_member(self, tid: str, uid: str) -> None:
        await self.delete(f"tenants/{tid}/members/{uid}")
        await self.delete(f"users/{uid}/memberships/{tid}")

    # ---------------------------------------------------------- invites ----
    async def put_invite(self, tid: str, inv: Invite) -> None:
        await self.set(f"invites/{inv.token_hash}", {**inv.model_dump(mode="json"), "tenant_id": tid})

    async def accept_invite(self, token_hash: str, uid: str) -> dict | None:
        def fn(d):
            if not d or d.get("accepted_by") or _dt(d["expires_at"]) < _now():
                return None, None
            d = {**d, "accepted_by": uid}
            return d, d
        return await self.atomic(f"invites/{token_hash}", fn)

    async def list_invites(self, tid: str) -> list[dict]:
        return await self.query("invites", [("tenant_id", "==", tid)])

    # --------------------------------------------------------- api keys ----
    async def put_api_key(self, tid: str, key: dict) -> None:
        await self.set(f"api_keys/{key['prefix']}", {**key, "tenant_id": tid})

    async def get_api_key(self, prefix: str) -> dict | None:
        return await self.get(f"api_keys/{prefix}")

    async def list_api_keys(self, tid: str) -> list[dict]:
        return [{k: v for k, v in d.items() if k != "key_hash"}
                for d in await self.query("api_keys", [("tenant_id", "==", tid)])]

    # --------------------------------------------------------- policies ----
    async def next_policy_version(self, tid: str) -> int:
        def fn(d):
            n = (d or {}).get("n", 0) + 1
            return {"n": n}, n
        return await self.atomic(f"tenants/{tid}/meta/policy_counter", fn)

    async def put_policy(self, p: Policy) -> None:
        await self.set(f"tenants/{p.tenant_id}/policies/{p.version}", p.model_dump(mode="json", by_alias=True))

    async def get_policy(self, tid: str, version: int) -> Policy | None:
        d = await self.get(f"tenants/{tid}/policies/{version}")
        return Policy(**d) if d else None

    async def list_policies(self, tid: str) -> list[Policy]:
        return [Policy(**d) for d in await self.query(f"tenants/{tid}/policies", order_by="version", desc=True)]

    async def active_policy(self, tid: str) -> Policy | None:
        t = await self.get_tenant(tid)
        return await self.get_policy(tid, t.active_policy_version) if t and t.active_policy_version else None

    # ------------------------------------------------------- vendor data ----
    async def list_vendors(self, tid: str) -> list[Vendor]:
        return [Vendor(**d) for d in await self.query(f"tenants/{tid}/vendors")]

    async def payments_for_vendor(self, tid: str, vendor_id: str) -> list[Payment]:
        return [Payment(**d) for d in await self.query(f"tenants/{tid}/payments", [("vendor_id", "==", vendor_id)])]

    async def import_vendors(self, tid: str, vendors: list[Vendor]) -> None:
        await self.batch_set([(f"tenants/{tid}/vendors/{v.id}", v.model_dump(mode="json")) for v in vendors])

    async def import_payments(self, tid: str, payments: list[Payment]) -> None:
        await self.batch_set([(f"tenants/{tid}/payments/{p.id}", p.model_dump(mode="json")) for p in payments])

    async def count(self, coll: str) -> int:
        return len(await self.query(coll, limit=10000))

    # ------------------------------------------------------------- cases ----
    def _case(self, tid: str, cid: str) -> str:
        return f"tenants/{tid}/cases/{cid}"

    async def create_case(self, case: Case) -> None:
        await self.set(self._case(case.tenant_id, case.id), case.model_dump(mode="json"))

    async def get_case(self, tid: str, cid: str) -> Case | None:
        d = await self.get(self._case(tid, cid))
        return Case(**d) if d else None

    async def save_case(self, case: Case) -> None:
        case.updated_at = _now()
        await self.set(self._case(case.tenant_id, case.id), case.model_dump(mode="json"))

    async def update_case(self, tid: str, cid: str, fn: Callable[[Case], Any]) -> Any:
        """Transactional case change (workflow actions). fn mutates the Case, returns a result."""
        def mut(d):
            if not d:
                return None, None
            c = Case(**d)
            res = fn(c)
            c.updated_at = _now()
            return c.model_dump(mode="json"), (c, res)
        return await self.atomic(self._case(tid, cid), mut)

    async def list_cases(self, tid: str, limit: int = 50) -> list[Case]:
        return [Case(**d) for d in await self.query(f"tenants/{tid}/cases", order_by="created_at",
                                                     desc=True, limit=limit)]

    async def find_case_by_idempotency(self, tid: str, key: str) -> Case | None:
        rows = await self.query(f"tenants/{tid}/cases", [("idempotency_key", "==", key)], limit=1)
        return Case(**rows[0]) if rows else None

    async def try_claim(self, tid: str, cid: str, lease_seconds: int) -> bool:
        def fn(d):
            if not d:
                return None, False
            lease = _dt(d.get("lease_until"))
            ok = d["status"] == CaseStatus.queued.value or (
                d["status"] == CaseStatus.running.value and (lease is None or lease < _now()))
            if not ok:
                return None, False
            return {**d, "status": CaseStatus.running.value,
                    "lease_until": (_now() + timedelta(seconds=lease_seconds)).isoformat()}, True
        return await self.atomic(self._case(tid, cid), fn)

    async def append_event(self, tid: str, cid: str, ev: Event) -> None:
        await self.set(f"{self._case(tid, cid)}/events/{ev.seq:06d}", ev.model_dump(mode="json"))

    async def events_after(self, tid: str, cid: str, seq: int) -> list[Event]:
        rows = await self.query(f"{self._case(tid, cid)}/events", [("seq", ">", seq)], order_by="seq")
        return [Event(**d) for d in rows]

    # ------------------------------------------------------------- audit ----
    async def audit(self, tid: str, actor: str, action: str, target: str, detail: dict | None = None) -> AuditEntry:
        detail = detail or {}
        head_path = f"tenants/{tid}/meta/audit_head"

        def fn(d):
            seq = (d or {}).get("seq", 0) + 1
            e = seal(seq, (d or {}).get("hash", GENESIS), actor, action, target, detail)
            return {"seq": seq, "hash": e.hash, "pending": e.model_dump(mode="json")}, e
        entry = await self.atomic(head_path, fn)
        await self.set(f"tenants/{tid}/audit/{entry.seq:08d}", entry.model_dump(mode="json"))
        return entry

    async def list_audit(self, tid: str) -> list[AuditEntry]:
        rows = await self.query(f"tenants/{tid}/audit", order_by="seq")
        head = await self.get(f"tenants/{tid}/meta/audit_head")
        # If the process died between sealing and writing, recover the sealed entry.
        if head and head.get("pending") and (not rows or rows[-1]["seq"] < head["seq"]):
            rows.append(head["pending"])
        return [AuditEntry(**r) for r in rows]


# ------------------------------------------------------------------ memory ---
def _match(doc: dict, f: str, op: str, v) -> bool:
    x = doc.get(f)
    return {"==": x == v, ">": x is not None and x > v, "<": x is not None and x < v,
            ">=": x is not None and x >= v, "<=": x is not None and x <= v}[op]


class MemoryStore(Store):
    """Single-process store for local development and tests."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def get(self, path):
        d = self.docs.get(path)
        return copy.deepcopy(d) if d is not None else None

    async def set(self, path, data):
        self.docs[path] = json.loads(json.dumps(data, default=str))

    async def delete(self, path):
        self.docs.pop(path, None)

    async def query(self, coll, where=None, order_by=None, desc=False, limit=None):
        depth = coll.count("/") + 1
        rows = [copy.deepcopy(d) for p, d in self.docs.items()
                if p.startswith(coll + "/") and p.count("/") == depth]
        for f, op, v in where or []:
            rows = [r for r in rows if _match(r, f, op, v)]
        if order_by:
            rows.sort(key=lambda r: (r.get(order_by) is None, r.get(order_by)), reverse=desc)
        return rows[:limit] if limit else rows

    async def atomic(self, path, fn):
        async with self._lock:
            new, res = fn(await self.get(path))
            if new is not None:
                await self.set(path, new)
            return res

    async def put_blob(self, key, data, mime):
        self.blobs[key] = data
        return f"mem://{key}"

    async def get_blob(self, uri):
        return self.blobs[uri.removeprefix("mem://")]


# --------------------------------------------------------------- firestore ---
class FirestoreStore(Store):
    def __init__(self, s: Settings, blobs):
        from google.cloud import firestore
        self.fs = firestore
        self.db = firestore.AsyncClient(project=s.google_cloud_project or None, database=s.firestore_database)
        self.blobs = blobs

    async def get(self, path):
        snap = await self.db.document(path).get()
        return snap.to_dict() if snap.exists else None

    async def set(self, path, data):
        await self.db.document(path).set(json.loads(json.dumps(data, default=str)))

    async def delete(self, path):
        await self.db.document(path).delete()

    async def query(self, coll, where=None, order_by=None, desc=False, limit=None):
        q = self.db.collection(coll)
        for f, op, v in where or []:
            q = q.where(filter=self.fs.FieldFilter(f, op, v))
        if order_by:
            q = q.order_by(order_by, direction=self.fs.Query.DESCENDING if desc else self.fs.Query.ASCENDING)
        if limit:
            q = q.limit(limit)
        return [d.to_dict() async for d in q.stream()]

    async def atomic(self, path, fn):
        ref = self.db.document(path)

        @self.fs.async_transactional
        async def run(tx):
            snap = await ref.get(transaction=tx)
            new, res = fn(snap.to_dict() if snap.exists else None)
            if new is not None:
                tx.set(ref, json.loads(json.dumps(new, default=str)))
            return res
        return await run(self.db.transaction())

    async def batch_set(self, items):
        for i in range(0, len(items), 400):
            b = self.db.batch()
            for p, d in items[i:i + 400]:
                b.set(self.db.document(p), json.loads(json.dumps(d, default=str)))
            await b.commit()

    async def put_blob(self, key, data, mime):
        return await self.blobs.put(key, data, mime)

    async def get_blob(self, uri):
        return await self.blobs.get(uri)


def build_store(s: Settings) -> Store:
    from .blobs import build_blobs
    if s.store_backend == "memory":
        return MemoryStore()
    blobs = build_blobs(s)
    if s.store_backend == "sql":
        from .sqlstore import SqlStore
        if not s.database_url:
            raise RuntimeError("DATABASE_URL is required with STORE_BACKEND=sql")
        return SqlStore(s.database_url, blobs)
    if s.store_backend == "firestore":
        return FirestoreStore(s, blobs)
    raise RuntimeError(f"unknown STORE_BACKEND {s.store_backend!r}")
