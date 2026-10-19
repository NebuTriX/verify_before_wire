"""Store contract tests: every backend (memory, SQL on SQLite, SQL on PostgreSQL when
VBW_TEST_DATABASE_URL is set) must behave the same for the worker, webhook outbox and audit chain."""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.audit import verify_chain
from app.blobs import FileBlobs
from app.models import BankAccount, Case, CaseStatus, Vendor
from app.store import MemoryStore
from app.tenancy import Member, Role, Tenant, TenantSettings

PG_URL = os.getenv("VBW_TEST_DATABASE_URL")
NOW = datetime.now(timezone.utc)


@pytest.fixture(params=["memory", "sql"])
async def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryStore()
        return
    from app.sqlstore import SqlStore
    s = SqlStore(PG_URL or f"sqlite+aiosqlite:///{tmp_path}/vbw.db", FileBlobs(str(tmp_path / "blobs")))
    await s.purge_for_tests()
    yield s
    await s.engine.dispose()


async def _tenant(store, tid="t_a"):
    await store.create_tenant(Tenant(id=tid, name=f"Co {tid}", settings=TenantSettings(), created_by="u1"),
                              Member(uid="u1", email="u1@x.com", role=Role.owner, added_by="u1"))
    return tid


def _case(tid, cid, minutes_ago, **kw):
    return Case(id=cid, tenant_id=tid, created_at=NOW - timedelta(minutes=minutes_ago), **kw)


async def test_claimable_cases_across_tenants(store):
    a, b = await _tenant(store, "t_a"), await _tenant(store, "t_b")
    await store.create_case(_case(a, "c_old", 10))
    await store.create_case(_case(b, "c_stale", 8, status=CaseStatus.running, lease_until=NOW - timedelta(seconds=5)))
    await store.create_case(_case(a, "c_busy", 6, status=CaseStatus.running, lease_until=NOW + timedelta(minutes=5)))
    await store.create_case(_case(b, "c_done", 4, status=CaseStatus.completed))
    await store.create_case(_case(a, "c_new", 2))
    assert await store.claimable_cases() == [(a, "c_old"), (b, "c_stale"), (a, "c_new")]
    assert await store.claimable_cases(limit=1) == [(a, "c_old")]

    assert await store.try_claim(a, "c_old", 60)
    assert not await store.try_claim(a, "c_old", 60)          # leased now
    assert (a, "c_old") not in await store.claimable_cases()

    c = await store.get_case(a, "c_new")
    c.status = CaseStatus.completed
    await store.save_case(c)
    assert await store.claimable_cases() == [(b, "c_stale")]


async def test_find_case_by_external_ref(store):
    tid = await _tenant(store)
    await store.create_case(_case(tid, "c_1", 5, external_ref="PAY-1"))
    await store.create_case(_case(tid, "c_2", 1, external_ref="PAY-1"))
    assert (await store.find_case_by_external_ref(tid, "PAY-1")).id == "c_2"
    assert await store.find_case_by_external_ref(tid, "PAY-404") is None
    assert await store.find_case_by_external_ref("t_other", "PAY-1") is None


async def test_webhooks_and_outbox(store):
    tid = await _tenant(store)
    hook = {"id": "wh_1", "url": "https://erp.example/hook", "secret": "s3cret", "events": ["case.recommended"],
            "active": True, "created_at": NOW}
    await store.put_webhook(tid, hook)
    [got] = await store.list_webhooks(tid)
    assert got["url"] == hook["url"] and got["secret"] == "s3cret" and got["events"] == ["case.recommended"]

    await store.enqueue_delivery(tid, {"id": "d_due", "webhook_id": "wh_1", "event": "case.recommended",
                                       "payload": {"case_id": "c_1", "amount": 1.5}, "status": "pending", "attempts": 0,
                                       "next_attempt_at": NOW - timedelta(seconds=1), "created_at": NOW - timedelta(seconds=2)})
    await store.enqueue_delivery(tid, {"id": "d_later", "webhook_id": "wh_1", "event": "case.released",
                                       "payload": {}, "status": "pending", "attempts": 0,
                                       "next_attempt_at": (NOW + timedelta(hours=1)).isoformat(), "created_at": NOW})
    due = await store.due_deliveries()
    assert [d["id"] for d in due] == ["d_due"] and due[0]["tenant_id"] == tid
    assert due[0]["payload"] == {"case_id": "c_1", "amount": 1.5}

    await store.update_delivery(tid, "d_due", {"status": "delivered", "attempts": 1, "response_status": 200,
                                               "delivered_at": NOW})
    await store.update_delivery("t_other", "d_later", {"status": "failed"})   # wrong tenant: no effect
    assert await store.due_deliveries() == []
    listed = await store.list_deliveries(tid)
    assert [d["id"] for d in listed] == ["d_later", "d_due"]
    assert listed[1]["status"] == "delivered" and listed[1]["response_status"] == 200
    assert listed[0]["status"] == "pending"

    await store.delete_webhook(tid, "wh_1")
    assert await store.list_webhooks(tid) == []


async def test_concurrent_audit_appends_never_fork_the_chain(store):
    tid = await _tenant(store)
    await asyncio.gather(*(store.audit(tid, "u1", "case.created", f"case/c_{i}", {"i": i, "amount": 10.5})
                           for i in range(20)))
    entries = await store.list_audit(tid)
    assert [e.seq for e in entries] == list(range(1, 21))
    assert verify_chain(entries)["ok"]


async def test_reference_data_and_counts(store):
    tid = await _tenant(store)
    acct = BankAccount(bank_name="DBS", account_number="0123-456 789", iban=None, swift_bic="DBSSSGSG",
                       country="SG", verified_on="2026-01-01")
    v = Vendor(id="V1", legal_name="Lion City Pte Ltd", country="SG", domains=["lion.sg"], bank_accounts=[acct],
               callback_contact="Tan", callback_phone="+65 6000 0000", default_currency="SGD")
    await store.import_vendors(tid, [v])
    await store.import_vendors(tid, [v.model_copy(update={"legal_name": "Lion City Logistics Pte Ltd"})])  # upsert
    [got] = await store.list_vendors(tid)
    assert got.legal_name == "Lion City Logistics Pte Ltd" and got.bank_accounts == [acct]
    assert await store.count(f"tenants/{tid}/vendors") == 1
    assert await store.count(f"tenants/{tid}/payments") == 0
    if hasattr(store, "vendors_with_account"):
        assert await store.vendors_with_account(tid, account="0123456789") == ["V1"]
        assert await store.vendors_with_account(tid, iban="GB00NOPE") == []
    await store.ping()


@pytest.mark.skipif(not PG_URL, reason="needs VBW_TEST_DATABASE_URL (PostgreSQL)")
async def test_postgres_audit_log_is_append_only(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.sqlstore import SqlStore
    s = SqlStore(PG_URL, FileBlobs(str(tmp_path)))
    await s.purge_for_tests()
    tid = await _tenant(s)
    await s.audit(tid, "u1", "case.created", "case/c_1", {})
    for stmt in ("UPDATE audit_log SET actor = 'mallory'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        with pytest.raises(DBAPIError, match="append-only"):
            async with s.engine.begin() as c:
                await c.execute(text(stmt))
    assert [e.actor for e in await s.list_audit(tid)] == ["u1"]
    await s.engine.dispose()
