"""Persistence, tenant-scoped. Document backends (memory, Firestore) implement six primitives
(get, set, delete, query, atomic, blobs) and inherit every domain method written on top of them.
The SQL backend (app/sqlstore.py) overrides the domain methods with a relational schema instead.

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
  tenants/{tid}/webhooks/{id}           outbound webhook endpoints
  webhook_deliveries/{id}               webhook outbox (global, carries tenant_id)
  work_queue/{tid}__{cid}               cases a worker may pick up (queued or running)
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .audit import GENESIS, AuditEntry, seal
from .config import Settings
from .models import Case, CaseStatus, Event, Payment, Vendor
from .policy import Policy
from .tenancy import Invite, Member, Tenant

log = logging.getLogger(__name__)
CONTENTION_ROUNDS = 6


class StoreContention(RuntimeError):
    """A transaction could not commit because other writers kept winning (Firestore under heavy
    contention). Claims treat it as "someone else has it"; other writes surface it as an error."""
Mutator = Callable[[dict | None], tuple[dict | None, Any]]


def _now():
    return datetime.now(timezone.utc)


def _dt(v):
    return datetime.fromisoformat(v) if isinstance(v, str) else v


class Store(ABC):
    # ------------------------------------------------------- primitives ----
    # Document backends implement these; the SQL backend overrides the domain methods instead.
    async def get(self, path: str) -> dict | None:
        raise NotImplementedError

    async def set(self, path: str, data: dict) -> None:
        raise NotImplementedError

    async def delete(self, path: str) -> None:
        raise NotImplementedError

    async def query(self, coll: str, where: list[tuple[str, str, Any]] | None = None,
                    order_by: str | None = None, desc: bool = False, limit: int | None = None) -> list[dict]:
        raise NotImplementedError

    async def atomic(self, path: str, fn: Mutator) -> Any:
        """Read-modify-write one document in a transaction. fn returns (new_doc|None, result)."""
        raise NotImplementedError

    @abstractmethod
    async def put_blob(self, key: str, data: bytes, mime: str) -> str: ...
    @abstractmethod
    async def get_blob(self, uri: str) -> bytes: ...

    async def ping(self) -> None:
        """Cheap connectivity check for readiness probes."""
        await self.get("meta/ping")

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

    async def get_invite(self, token_hash: str) -> dict | None:
        return await self.get(f"invites/{token_hash}")

    async def accept_invite(self, token_hash: str, uid: str, email: str) -> tuple[str, dict | None]:
        """Atomically use an invite. ("ok", invite), ("invalid", None) when missing/used/expired, or
        ("wrong_email", invite) when it is for someone else, in which case it is NOT used up."""
        def fn(d):
            if not d or d.get("accepted_by") or _dt(d["expires_at"]) < _now():
                return None, ("invalid", None)
            if d["email"].lower() != email.lower():
                return None, ("wrong_email", d)
            d = {**d, "accepted_by": uid}
            return d, ("ok", d)
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

    def _queue(self, tid: str, cid: str) -> str:
        return f"work_queue/{tid}__{cid}"

    async def _track(self, case: Case) -> None:
        """Keep the cross-tenant work queue in step with the case, so workers never scan all cases."""
        if case.status in (CaseStatus.completed, CaseStatus.failed):
            await self.delete(self._queue(case.tenant_id, case.id))
        else:
            await self.set(self._queue(case.tenant_id, case.id), {
                "tenant_id": case.tenant_id, "case_id": case.id, "status": case.status.value,
                "created_at": case.created_at.isoformat(),
                "lease_until": case.lease_until.isoformat() if case.lease_until else None})

    async def create_case(self, case: Case) -> None:
        await self.set(self._case(case.tenant_id, case.id), case.model_dump(mode="json"))
        await self._track(case)

    async def get_case(self, tid: str, cid: str) -> Case | None:
        d = await self.get(self._case(tid, cid))
        return Case(**d) if d else None

    async def save_case(self, case: Case) -> None:
        case.updated_at = _now()
        await self.set(self._case(case.tenant_id, case.id), case.model_dump(mode="json"))
        await self._track(case)

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
        try:
            ok = await self.atomic(self._case(tid, cid), fn)
        except StoreContention:
            return False                 # another process is claiming it right now
        if ok and (c := await self.get_case(tid, cid)):
            await self._track(c)
        return ok

    async def claimable_cases(self, limit: int = 20) -> list[tuple[str, str]]:
        """(tenant_id, case_id) a worker may run: queued, or running with an expired lease. Oldest first."""
        now = _now()
        rows = [r for r in await self.query("work_queue")
                if r["status"] == CaseStatus.queued.value
                or (r["status"] == CaseStatus.running.value and (not r.get("lease_until") or _dt(r["lease_until"]) < now))]
        rows.sort(key=lambda r: r["created_at"])
        return [(r["tenant_id"], r["case_id"]) for r in rows[:limit]]

    async def find_case_by_external_ref(self, tid: str, ref: str) -> Case | None:
        rows = await self.query(f"tenants/{tid}/cases", [("external_ref", "==", ref)])
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return Case(**rows[0]) if rows else None

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


    # ---------------------------------------------------------- webhooks ----
    async def put_webhook(self, tid: str, hook: dict) -> None:
        await self.set(f"tenants/{tid}/webhooks/{hook['id']}", {**_jsonable(hook), "tenant_id": tid})

    async def list_webhooks(self, tid: str) -> list[dict]:
        return sorted(await self.query(f"tenants/{tid}/webhooks"), key=lambda h: str(h.get("created_at", "")))

    async def delete_webhook(self, tid: str, hook_id: str) -> None:
        await self.delete(f"tenants/{tid}/webhooks/{hook_id}")

    async def enqueue_delivery(self, tid: str, d: dict) -> None:
        await self.set(f"webhook_deliveries/{d['id']}", {**_jsonable(d), "tenant_id": tid})

    async def due_deliveries(self, limit: int = 50) -> list[dict]:
        now = _now()
        rows = [r for r in await self.query("webhook_deliveries", [("status", "==", "pending")])
                if _dt(r["next_attempt_at"]) <= now]
        rows.sort(key=lambda r: r["next_attempt_at"])
        return rows[:limit]

    async def claim_delivery(self, tid: str, delivery_id: str, lease_seconds: int = 60) -> bool:
        """Atomically take a due delivery so that only one process sends it: pushes its next attempt
        out by lease_seconds. False if another process already took it (or it is no longer pending)."""
        def fn(d):
            if not d or d.get("tenant_id") != tid or d.get("status") != "pending" or _dt(d["next_attempt_at"]) > _now():
                return None, False
            return {**d, "next_attempt_at": (_now() + timedelta(seconds=lease_seconds)).isoformat()}, True
        try:
            return bool(await self.atomic(f"webhook_deliveries/{delivery_id}", fn))
        except StoreContention:
            return False                 # another process is sending it

    async def renew_lease(self, tid: str, cid: str, lease_seconds: int) -> bool:
        """Extend the lease of a case this process is still investigating (heartbeat)."""
        def fn(c: Case) -> bool:
            if c.status != CaseStatus.running:
                return False
            c.lease_until = _now() + timedelta(seconds=lease_seconds)
            return True
        res = await self.update_case(tid, cid, fn)
        return bool(res and res[1])

    async def update_delivery(self, tid: str, delivery_id: str, changes: dict) -> None:
        def fn(d):
            if not d or d.get("tenant_id") != tid:
                return None, None
            return {**d, **_jsonable(changes)}, None
        await self.atomic(f"webhook_deliveries/{delivery_id}", fn)

    async def list_deliveries(self, tid: str, limit: int = 50) -> list[dict]:
        rows = await self.query("webhook_deliveries", [("tenant_id", "==", tid)])
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows[:limit]


def _jsonable(d: dict) -> dict:
    """Datetimes as ISO-8601 strings (sortable, and identical across backends)."""
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}


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
        # Many writers on one document (audit head, a contested claim) can exhaust the SDK's own
        # retries; back off with jitter and try again before giving up.
        for attempt in range(CONTENTION_ROUNDS):
            try:
                return await run(self.db.transaction(max_attempts=10))
            except ValueError as e:
                if "Failed to commit transaction" not in str(e):
                    raise
                # jitter only, not security-sensitive
                await asyncio.sleep(random.uniform(0.02, 0.12) * (attempt + 1))  # nosec B311
        raise StoreContention(f"too many concurrent writes to {path}")

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
