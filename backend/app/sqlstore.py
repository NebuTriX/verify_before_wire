"""SQL backend: a relational schema (app/db_schema.py) on PostgreSQL in production and SQLite in tests.
Runs on any cloud or on-premises: AWS RDS/Aurora, Azure Database for PostgreSQL, Cloud SQL, or a VM.

Schema changes are Alembic migrations (backend/migrations). On PostgreSQL the store applies pending
migrations at startup when DB_AUTO_MIGRATE is true (the default, guarded by an advisory lock so many
instances can start at once). Production deploys that run `alembic upgrade head` as a release step
set DB_AUTO_MIGRATE=false; the store then refuses to start against an out-of-date schema.
SQLite (tests, quick local runs) is created directly from the schema.

Concurrency: every read-modify-write runs in one transaction with SELECT ... FOR UPDATE on PostgreSQL
(case workflow changes, worker leases, invite acceptance, counters). Audit entries are numbered under
a per-workspace row lock, so concurrent writers can never fork the hash chain; the (tenant_id, seq)
primary key and the unique hash are the backstop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .audit import GENESIS, AuditEntry, seal
from .blobs import Blobs
from .db_schema import (api_keys, audit_log, case_events, cases, counters, invites, members, metadata, payments,
                        policies, tenants, vendor_bank_accounts, vendors, webhook_deliveries, webhooks)
from .models import Case, CaseStatus, Event, Payment, Vendor
from .policy import Policy
from .store import Store, _jsonable
from .tenancy import Invite, Member, Tenant

log = logging.getLogger(__name__)
BACKEND_DIR = Path(__file__).resolve().parent.parent
CHUNK = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm(v: str | None) -> str | None:
    s = re.sub(r"[^A-Z0-9]", "", (v or "").upper())
    return s or None


def normalise_url(url: str) -> str:
    if url.startswith(("postgresql://", "postgres://")):
        return "postgresql+asyncpg://" + url.split("://", 1)[1]
    return url


def run_migrations(url: str) -> None:
    """`alembic upgrade head`, callable from code (run it in a thread: env.py starts its own event loop)."""
    from alembic import command
    from alembic.config import Config
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


def head_revision() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _row_dict(row) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row._mapping.items()}


class SqlStore(Store):
    def __init__(self, url: str, blobs: Blobs, engine: AsyncEngine | None = None):
        self.url = normalise_url(url)
        self.sqlite = self.url.startswith("sqlite")
        if engine is not None:
            self.engine = engine
        elif self.sqlite:
            self.engine = create_async_engine(self.url)
        else:
            self.engine = create_async_engine(
                self.url, pool_pre_ping=True,
                pool_size=int(os.getenv("DATABASE_POOL_SIZE", "10")),
                max_overflow=int(os.getenv("DATABASE_MAX_OVERFLOW", "20")),
                pool_recycle=int(os.getenv("DATABASE_POOL_RECYCLE_S", "1800")))
        self.blobs = blobs
        self._ready = False
        self._init_lock = asyncio.Lock()
        # SQLite allows one writer; serialise write transactions in-process instead of failing with
        # "database is locked". PostgreSQL relies on row locks.
        self._wlock = asyncio.Lock() if self.sqlite else None

    # ------------------------------------------------------------ plumbing ----
    async def _init(self) -> None:
        if self._ready:
            return
        async with self._init_lock:
            if self._ready:
                return
            if self.sqlite:
                async with self.engine.begin() as c:
                    await c.run_sync(metadata.create_all)
            elif os.getenv("DB_AUTO_MIGRATE", "true").lower() in ("1", "true", "yes"):
                await asyncio.to_thread(run_migrations, self.url)
            else:
                async with self.engine.connect() as c:
                    try:
                        current = (await c.execute(text("SELECT version_num FROM alembic_version"))).scalar()
                    except Exception:
                        current = None
                if current != (head := head_revision()):
                    raise RuntimeError(f"Database schema is at {current or 'nothing'}, the app needs {head}. "
                                       "Run `alembic upgrade head` (backend/) or set DB_AUTO_MIGRATE=true.")
            self._ready = True

    @asynccontextmanager
    async def _tx(self):
        await self._init()
        if self._wlock is not None:
            async with self._wlock, self.engine.begin() as c:
                yield c
        else:
            async with self.engine.begin() as c:
                yield c

    @asynccontextmanager
    async def _ro(self):
        await self._init()
        async with self.engine.connect() as c:
            yield c

    def _insert(self, table):
        if self.sqlite:
            from sqlalchemy.dialects.sqlite import insert
        else:
            from sqlalchemy.dialects.postgresql import insert
        return insert(table)

    async def _upsert(self, c, table, rows: dict | list[dict], keys: list[str]) -> None:
        rows = rows if isinstance(rows, list) else [rows]
        if not rows:
            return
        stmt = self._insert(table).values(rows)
        cols = [k for k in rows[0] if k not in keys]
        stmt = stmt.on_conflict_do_update(index_elements=keys, set_={k: stmt.excluded[k] for k in cols}) if cols \
            else stmt.on_conflict_do_nothing(index_elements=keys)
        await c.execute(stmt)

    async def _bump(self, c, tid: str, name: str) -> tuple[int, str | None]:
        """Lock this workspace's counter row and return (value, last_hash) before incrementing."""
        await c.execute(self._insert(counters).values(tenant_id=tid, name=name, value=0)
                        .on_conflict_do_nothing(index_elements=["tenant_id", "name"]))
        row = (await c.execute(select(counters.c.value, counters.c.last_hash)
                               .where(counters.c.tenant_id == tid, counters.c.name == name)
                               .with_for_update())).one()
        return int(row.value), row.last_hash

    async def ping(self) -> None:
        async with self._ro() as c:
            await c.execute(text("SELECT 1"))

    async def purge_for_tests(self) -> None:
        """Test-only: empty every table. Bypasses the audit_log append-only trigger (superuser only)."""
        async with self._tx() as c:
            if self.sqlite:
                for t in reversed(metadata.sorted_tables):
                    await c.execute(delete(t))
            else:
                await c.execute(text("SET LOCAL session_replication_role = replica"))
                names = ", ".join(t.name for t in metadata.sorted_tables)
                await c.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))  # table names come from the schema, not input

    async def put_blob(self, key, data, mime):
        return await self.blobs.put(key, data, mime)

    async def get_blob(self, uri):
        return await self.blobs.get(uri)

    # ---------------------------------------------------------- tenants ----
    @staticmethod
    def _tenant_row(t: Tenant) -> dict:
        return {"id": t.id, "name": t.name, "status": t.status, "active_policy_version": t.active_policy_version,
                "created_by": t.created_by, "created_at": t.created_at, "doc": t.model_dump(mode="json")}

    async def create_tenant(self, t: Tenant, owner: Member) -> None:
        async with self._tx() as c:
            await self._upsert(c, tenants, self._tenant_row(t), ["id"])
            await self._upsert(c, members, self._member_row(t.id, owner), ["tenant_id", "uid"])

    async def get_tenant(self, tid: str) -> Tenant | None:
        async with self._ro() as c:
            doc = (await c.execute(select(tenants.c.doc).where(tenants.c.id == tid))).scalar()
        return Tenant(**doc) if doc else None

    async def save_tenant(self, t: Tenant) -> None:
        async with self._tx() as c:
            await self._upsert(c, tenants, self._tenant_row(t), ["id"])

    async def tenants_for(self, uid: str) -> list[dict]:
        q = (select(members.c.tenant_id, tenants.c.name, members.c.role)
             .join(tenants, tenants.c.id == members.c.tenant_id)
             .where(members.c.uid == uid).order_by(members.c.added_at))
        async with self._ro() as c:
            return [dict(r._mapping) for r in (await c.execute(q)).all()]

    # ---------------------------------------------------------- members ----
    @staticmethod
    def _member_row(tid: str, m: Member) -> dict:
        return {"tenant_id": tid, "uid": m.uid, "email": m.email, "role": m.role.value, "added_by": m.added_by,
                "added_at": m.added_at}

    async def put_member(self, tid: str, m: Member, tenant_name: str) -> None:
        async with self._tx() as c:
            await self._upsert(c, members, self._member_row(tid, m), ["tenant_id", "uid"])

    async def get_member(self, tid: str, uid: str) -> Member | None:
        async with self._ro() as c:
            r = (await c.execute(select(members).where(members.c.tenant_id == tid, members.c.uid == uid))).first()
        return self._member(r) if r else None

    @staticmethod
    def _member(r) -> Member:
        return Member(uid=r.uid, email=r.email, role=r.role, added_by=r.added_by, added_at=r.added_at)

    async def list_members(self, tid: str) -> list[Member]:
        async with self._ro() as c:
            rows = (await c.execute(select(members).where(members.c.tenant_id == tid)
                                    .order_by(members.c.added_at))).all()
        return [self._member(r) for r in rows]

    async def remove_member(self, tid: str, uid: str) -> None:
        async with self._tx() as c:
            await c.execute(delete(members).where(members.c.tenant_id == tid, members.c.uid == uid))

    # ---------------------------------------------------------- invites ----
    async def put_invite(self, tid: str, inv: Invite) -> None:
        doc = {**inv.model_dump(mode="json"), "tenant_id": tid}
        async with self._tx() as c:
            await self._upsert(c, invites, {"token_hash": inv.token_hash, "tenant_id": tid, "email": inv.email,
                                            "role": inv.role.value, "expires_at": inv.expires_at,
                                            "accepted_by": inv.accepted_by, "doc": doc}, ["token_hash"])

    async def get_invite(self, token_hash: str) -> dict | None:
        async with self._ro() as c:
            return (await c.execute(select(invites.c.doc).where(invites.c.token_hash == token_hash))).scalar()

    async def accept_invite(self, token_hash: str, uid: str, email: str) -> tuple[str, dict | None]:
        async with self._tx() as c:
            r = (await c.execute(select(invites).where(invites.c.token_hash == token_hash).with_for_update())).first()
            if not r or r.accepted_by or r.expires_at < _now():
                return "invalid", None
            if r.email.lower() != email.lower():
                return "wrong_email", r.doc              # someone else's invite: leave it unused
            doc = {**r.doc, "accepted_by": uid}
            await c.execute(update(invites).where(invites.c.token_hash == token_hash)
                            .values(accepted_by=uid, doc=doc))
            return "ok", doc

    async def list_invites(self, tid: str) -> list[dict]:
        async with self._ro() as c:
            return [r for (r,) in (await c.execute(select(invites.c.doc).where(invites.c.tenant_id == tid))).all()]

    # --------------------------------------------------------- api keys ----
    async def put_api_key(self, tid: str, key: dict) -> None:
        doc = {**key, "tenant_id": tid}
        async with self._tx() as c:
            await self._upsert(c, api_keys, {"prefix": key["prefix"], "tenant_id": tid, "name": key["name"],
                                             "role": key["role"], "revoked": bool(key.get("revoked")), "doc": doc},
                               ["prefix"])

    async def get_api_key(self, prefix: str) -> dict | None:
        async with self._ro() as c:
            return (await c.execute(select(api_keys.c.doc).where(api_keys.c.prefix == prefix))).scalar()

    async def list_api_keys(self, tid: str) -> list[dict]:
        async with self._ro() as c:
            rows = (await c.execute(select(api_keys.c.doc).where(api_keys.c.tenant_id == tid))).all()
        return [{k: v for k, v in d.items() if k != "key_hash"} for (d,) in rows]

    # --------------------------------------------------------- policies ----
    async def next_policy_version(self, tid: str) -> int:
        async with self._tx() as c:
            n, _ = await self._bump(c, tid, "policy_version")
            await c.execute(update(counters).where(counters.c.tenant_id == tid, counters.c.name == "policy_version")
                            .values(value=n + 1))
            return n + 1

    async def put_policy(self, p: Policy) -> None:
        async with self._tx() as c:
            await self._upsert(c, policies, {"tenant_id": p.tenant_id, "version": p.version, "name": p.name,
                                             "status": p.status, "doc": p.model_dump(mode="json", by_alias=True)},
                               ["tenant_id", "version"])

    async def get_policy(self, tid: str, version: int) -> Policy | None:
        async with self._ro() as c:
            doc = (await c.execute(select(policies.c.doc).where(policies.c.tenant_id == tid,
                                                                policies.c.version == version))).scalar()
        return Policy(**doc) if doc else None

    async def list_policies(self, tid: str) -> list[Policy]:
        async with self._ro() as c:
            rows = (await c.execute(select(policies.c.doc).where(policies.c.tenant_id == tid)
                                    .order_by(policies.c.version.desc()))).all()
        return [Policy(**d) for (d,) in rows]

    # ------------------------------------------------------- vendor data ----
    async def list_vendors(self, tid: str) -> list[Vendor]:
        async with self._ro() as c:
            rows = (await c.execute(select(vendors.c.doc).where(vendors.c.tenant_id == tid)
                                    .order_by(vendors.c.id))).all()
        return [Vendor(**d) for (d,) in rows]

    async def import_vendors(self, tid: str, items: list[Vendor]) -> None:
        """Upsert by vendor id; a vendor's approved bank accounts are replaced by the imported set."""
        now = _now()
        async with self._tx() as c:
            for i in range(0, len(items), CHUNK):
                part = items[i:i + CHUNK]
                await self._upsert(c, vendors, [{"tenant_id": tid, "id": v.id, "legal_name": v.legal_name,
                                                 "country": v.country, "updated_at": now,
                                                 "doc": v.model_dump(mode="json")} for v in part], ["tenant_id", "id"])
                await c.execute(delete(vendor_bank_accounts).where(
                    vendor_bank_accounts.c.tenant_id == tid, vendor_bank_accounts.c.vendor_id.in_([v.id for v in part])))
                accts = [{"tenant_id": tid, "vendor_id": v.id, "iban_norm": _norm(b.iban),
                          "account_norm": _norm(b.account_number), "swift_bic": _norm(b.swift_bic), "country": b.country}
                         for v in part for b in v.bank_accounts]
                if accts:
                    await c.execute(vendor_bank_accounts.insert(), accts)

    async def vendors_with_account(self, tid: str, iban: str | None = None, account: str | None = None) -> list[str]:
        """Vendor ids that have this IBAN or account number on file (indexed lookup)."""
        conds = []
        if _norm(iban):
            conds.append(vendor_bank_accounts.c.iban_norm == _norm(iban))
        if _norm(account):
            conds.append(vendor_bank_accounts.c.account_norm == _norm(account))
        if not conds:
            return []
        async with self._ro() as c:
            rows = (await c.execute(select(vendor_bank_accounts.c.vendor_id).distinct()
                                    .where(vendor_bank_accounts.c.tenant_id == tid, or_(*conds)))).all()
        return [r[0] for r in rows]

    async def payments_for_vendor(self, tid: str, vendor_id: str) -> list[Payment]:
        async with self._ro() as c:
            rows = (await c.execute(select(payments.c.doc).where(payments.c.tenant_id == tid,
                                                                 payments.c.vendor_id == vendor_id)
                                    .order_by(payments.c.paid_on))).all()
        return [Payment(**d) for (d,) in rows]

    async def import_payments(self, tid: str, items: list[Payment]) -> None:
        async with self._tx() as c:
            for i in range(0, len(items), CHUNK):
                await self._upsert(c, payments, [
                    {"tenant_id": tid, "id": p.id, "vendor_id": p.vendor_id, "invoice_number": p.invoice_number,
                     "amount": p.amount, "currency": p.currency, "paid_on": p.paid_on, "account_ref": p.account_ref,
                     "doc": p.model_dump(mode="json")} for p in items[i:i + CHUNK]], ["tenant_id", "id"])

    _COUNTABLE = {"vendors": vendors, "payments": payments, "cases": cases, "members": members, "policies": policies}

    async def count(self, coll: str) -> int:
        """Accepts the document-store path used elsewhere, e.g. tenants/{tid}/vendors."""
        m = re.fullmatch(r"tenants/([^/]+)/(\w+)", coll)
        if not m or m.group(2) not in self._COUNTABLE:
            raise ValueError(f"cannot count {coll!r}")
        t = self._COUNTABLE[m.group(2)]
        async with self._ro() as c:
            return int((await c.execute(select(func.count()).select_from(t).where(t.c.tenant_id == m.group(1)))).scalar())

    # ------------------------------------------------------------- cases ----
    @staticmethod
    def _case_row(case: Case) -> dict:
        r, v = case.request, case.verdict
        return {"tenant_id": case.tenant_id, "id": case.id, "status": case.status.value,
                "created_at": case.created_at, "updated_at": case.updated_at, "lease_until": case.lease_until,
                "idempotency_key": case.idempotency_key, "external_ref": case.external_ref,
                "decision": v.decision.value if v else None, "risk_score": v.risk_score if v else None,
                "workflow_state": case.workflow.state,
                "amount": r.amount if r and r.amount is not None and abs(r.amount) < 1e17 else None,
                "currency": (r.currency or "")[:3] or None if r else None,
                "payee": (r.beneficiary_name or "")[:300] or None if r else None,
                "doc": case.model_dump(mode="json")}

    async def create_case(self, case: Case) -> None:
        async with self._tx() as c:
            await c.execute(cases.insert().values(**self._case_row(case)))

    async def get_case(self, tid: str, cid: str) -> Case | None:
        async with self._ro() as c:
            doc = (await c.execute(select(cases.c.doc).where(cases.c.tenant_id == tid, cases.c.id == cid))).scalar()
        return Case(**doc) if doc else None

    async def save_case(self, case: Case) -> None:
        case.updated_at = _now()
        async with self._tx() as c:
            await self._upsert(c, cases, self._case_row(case), ["tenant_id", "id"])

    async def _locked_case(self, c, tid: str, cid: str) -> Case | None:
        doc = (await c.execute(select(cases.c.doc).where(cases.c.tenant_id == tid, cases.c.id == cid)
                               .with_for_update())).scalar()
        return Case(**doc) if doc else None

    async def _write_case(self, c, case: Case) -> None:
        row = self._case_row(case)
        await c.execute(update(cases).where(cases.c.tenant_id == case.tenant_id, cases.c.id == case.id)
                        .values(**{k: v for k, v in row.items() if k not in ("tenant_id", "id")}))

    async def update_case(self, tid: str, cid: str, fn: Callable[[Case], Any]) -> Any:
        async with self._tx() as c:
            case = await self._locked_case(c, tid, cid)
            if case is None:
                return None
            res = fn(case)
            case.updated_at = _now()
            await self._write_case(c, case)
            return case, res

    async def try_claim(self, tid: str, cid: str, lease_seconds: int) -> bool:
        async with self._tx() as c:
            case = await self._locked_case(c, tid, cid)
            if case is None:
                return False
            ok = case.status == CaseStatus.queued or (
                case.status == CaseStatus.running and (case.lease_until is None or case.lease_until < _now()))
            if not ok:
                return False
            case.status, case.lease_until = CaseStatus.running, _now() + timedelta(seconds=lease_seconds)
            await self._write_case(c, case)
            return True

    async def claimable_cases(self, limit: int = 20) -> list[tuple[str, str]]:
        q = (select(cases.c.tenant_id, cases.c.id)
             .where(or_(cases.c.status == CaseStatus.queued.value,
                        and_(cases.c.status == CaseStatus.running.value,
                             or_(cases.c.lease_until.is_(None), cases.c.lease_until < _now()))))
             .order_by(cases.c.created_at).limit(limit))
        async with self._ro() as c:
            return [(r.tenant_id, r.id) for r in (await c.execute(q)).all()]

    async def list_cases(self, tid: str, limit: int = 50) -> list[Case]:
        async with self._ro() as c:
            rows = (await c.execute(select(cases.c.doc).where(cases.c.tenant_id == tid)
                                    .order_by(cases.c.created_at.desc()).limit(limit))).all()
        return [Case(**d) for (d,) in rows]

    async def find_case_by_idempotency(self, tid: str, key: str) -> Case | None:
        async with self._ro() as c:
            doc = (await c.execute(select(cases.c.doc).where(cases.c.tenant_id == tid,
                                                             cases.c.idempotency_key == key))).scalar()
        return Case(**doc) if doc else None

    async def find_case_by_external_ref(self, tid: str, ref: str) -> Case | None:
        async with self._ro() as c:
            doc = (await c.execute(select(cases.c.doc).where(cases.c.tenant_id == tid, cases.c.external_ref == ref)
                                   .order_by(cases.c.created_at.desc()).limit(1))).scalar()
        return Case(**doc) if doc else None

    async def append_event(self, tid: str, cid: str, ev: Event) -> None:
        async with self._tx() as c:
            await self._upsert(c, case_events, {"tenant_id": tid, "case_id": cid, "seq": ev.seq, "type": ev.type,
                                                "agent": ev.agent, "ts": ev.ts, "data": ev.model_dump(mode="json")["data"]},
                               ["tenant_id", "case_id", "seq"])

    async def events_after(self, tid: str, cid: str, seq: int) -> list[Event]:
        async with self._ro() as c:
            rows = (await c.execute(select(case_events).where(case_events.c.tenant_id == tid,
                                                              case_events.c.case_id == cid, case_events.c.seq > seq)
                                    .order_by(case_events.c.seq))).all()
        return [Event(seq=r.seq, type=r.type, agent=r.agent, data=r.data, ts=r.ts) for r in rows]

    # ------------------------------------------------------------- audit ----
    async def audit(self, tid: str, actor: str, action: str, target: str, detail: dict | None = None) -> AuditEntry:
        async with self._tx() as c:
            n, last = await self._bump(c, tid, "audit")
            e = seal(n + 1, last or GENESIS, actor, action, target, detail or {})
            body = e.model_dump(mode="json")
            await c.execute(audit_log.insert().values(tenant_id=tid, seq=e.seq, at=e.at, actor=e.actor, action=e.action,
                                                      target=e.target, detail=body["detail"],
                                                      prev_hash=e.prev_hash, hash=e.hash))
            await c.execute(update(counters).where(counters.c.tenant_id == tid, counters.c.name == "audit")
                            .values(value=e.seq, last_hash=e.hash))
        return e

    async def list_audit(self, tid: str) -> list[AuditEntry]:
        async with self._ro() as c:
            rows = (await c.execute(select(audit_log).where(audit_log.c.tenant_id == tid)
                                    .order_by(audit_log.c.seq))).all()
        return [AuditEntry(seq=r.seq, at=r.at, actor=r.actor, action=r.action, target=r.target, detail=r.detail,
                           prev_hash=r.prev_hash, hash=r.hash) for r in rows]

    # ---------------------------------------------------------- webhooks ----
    async def put_webhook(self, tid: str, hook: dict) -> None:
        doc = {**_jsonable(hook), "tenant_id": tid}
        async with self._tx() as c:
            await self._upsert(c, webhooks, {"tenant_id": tid, "id": hook["id"], "url": hook["url"],
                                             "active": bool(hook.get("active", True)), "events": list(hook.get("events", [])),
                                             "secret": hook["secret"], "created_at": hook.get("created_at") or _now(),
                                             "doc": doc}, ["tenant_id", "id"])

    async def list_webhooks(self, tid: str) -> list[dict]:
        async with self._ro() as c:
            rows = (await c.execute(select(webhooks.c.doc).where(webhooks.c.tenant_id == tid)
                                    .order_by(webhooks.c.created_at))).all()
        return [d for (d,) in rows]

    async def delete_webhook(self, tid: str, hook_id: str) -> None:
        async with self._tx() as c:
            await c.execute(delete(webhooks).where(webhooks.c.tenant_id == tid, webhooks.c.id == hook_id))

    _DELIVERY_COLS = {"webhook_id", "event", "payload", "status", "attempts", "next_attempt_at", "created_at",
                      "delivered_at", "response_status", "last_error"}

    async def enqueue_delivery(self, tid: str, d: dict) -> None:
        row = {k: v for k, v in d.items() if k in self._DELIVERY_COLS}
        row = {"status": "pending", "attempts": 0, "created_at": _now(), "next_attempt_at": _now(), **row}
        async with self._tx() as c:
            await c.execute(webhook_deliveries.insert().values(id=d["id"], tenant_id=tid, **row))

    async def due_deliveries(self, limit: int = 50) -> list[dict]:
        async with self._ro() as c:
            rows = (await c.execute(select(webhook_deliveries)
                                    .where(webhook_deliveries.c.status == "pending",
                                           webhook_deliveries.c.next_attempt_at <= _now())
                                    .order_by(webhook_deliveries.c.next_attempt_at).limit(limit))).all()
        return [_row_dict(r) for r in rows]

    async def claim_delivery(self, tid: str, delivery_id: str, lease_seconds: int = 60) -> bool:
        async with self._tx() as c:
            r = await c.execute(update(webhook_deliveries)
                                .where(webhook_deliveries.c.tenant_id == tid, webhook_deliveries.c.id == delivery_id,
                                       webhook_deliveries.c.status == "pending",
                                       webhook_deliveries.c.next_attempt_at <= _now())
                                .values(next_attempt_at=_now() + timedelta(seconds=lease_seconds)))
            return r.rowcount == 1

    async def update_delivery(self, tid: str, delivery_id: str, changes: dict) -> None:
        bad = set(changes) - self._DELIVERY_COLS
        if bad:
            raise ValueError(f"unknown delivery fields: {sorted(bad)}")
        async with self._tx() as c:
            await c.execute(update(webhook_deliveries).where(webhook_deliveries.c.tenant_id == tid,
                                                             webhook_deliveries.c.id == delivery_id).values(**changes))

    async def list_deliveries(self, tid: str, limit: int = 50) -> list[dict]:
        async with self._ro() as c:
            rows = (await c.execute(select(webhook_deliveries).where(webhook_deliveries.c.tenant_id == tid)
                                    .order_by(webhook_deliveries.c.created_at.desc()).limit(limit))).all()
        return [_row_dict(r) for r in rows]
