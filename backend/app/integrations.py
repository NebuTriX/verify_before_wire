"""ERP integration API (built for SAP S/4HANA / ECC, works for any ERP).

The ERP sends structured payment data, so no document reading and no AI is involved: fields map
1:1 from the SAP payment proposal (REGUH/REGUP) and vendor master (LFA1/LFBK or Business
Partner). See docs/sap-integration.md for the end-to-end flow.

  POST /api/t/{tid}/erp/payments                 check one payment         (?wait=seconds)
  POST /api/t/{tid}/erp/payment-runs             check a whole F110 run    (?wait=seconds)
  GET  /api/t/{tid}/erp/payments/{external_ref}  status, for polling instead of webhooks
  PUT  /api/t/{tid}/erp/vendors                  sync vendor master (upsert)
  PUT  /api/t/{tid}/erp/payment-history          sync cleared payments (upsert)
  GET|POST|DELETE /api/t/{tid}/webhooks ...      signed callbacks to the ERP
  GET  /api/t/{tid}/sources/status               freshness of every data source
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from . import ratelimit, trace
from .auth import Access, access
from .models import BankAccount, Case, CaseStatus, Payment, PaymentRequest, SourceDoc, Vendor
from .orchestrator import ERP_LABEL, Deps, start_case
from .tenancy import Perm, new_secret
from .webhooks import EVENTS, case_payload, check_url, deliver_one, new_delivery

router = APIRouter()
_BACKGROUND: set[asyncio.Task] = set()     # investigations that outlive the request that started them
REF_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


def _deps(r: Request) -> Deps:
    return r.app.state.deps


# ------------------------------------------------------------------ models ----
class ErpBank(BaseModel):
    country: str | None = Field(None, description="SAP ZBNKS / BANKS: bank country (ISO alpha-2)")
    bank_key: str | None = Field(None, description="SAP ZBNKL / BANKL: bank key (sort code, routing number...)")
    account: str | None = Field(None, description="SAP ZBNKN / BANKN: account number")
    iban: str | None = Field(None, description="SAP ZIBAN / TIBAN-IBAN")
    swift: str | None = Field(None, description="SAP ZSWIF / BNKA-SWIFT")
    bank_name: str | None = Field(None, description="SAP BNKA-BANKA")
    account_holder: str | None = Field(None, description="SAP KOINH: account holder, if different from the vendor")


class ErpVendorRef(BaseModel):
    id: str = Field(description="SAP LIFNR (vendor) or Business Partner number", max_length=40)
    name: str = Field(description="SAP NAME1", max_length=200)
    country: str | None = Field(None, description="SAP LAND1")
    vat_number: str | None = Field(None, description="SAP STCEG (EU VAT registration number)")


class ErpDocument(BaseModel):
    number: str = Field(description="SAP BELNR", max_length=20)
    fiscal_year: str | None = Field(None, description="SAP GJAHR", max_length=4)
    reference: str | None = Field(None, description="SAP XBLNR: the vendor's invoice number", max_length=40)


class ErpPayment(BaseModel):
    external_ref: str = Field(description="Your unique id for this payment, e.g. F110:<LAUFD>:<LAUFI>:<ZBUKR>:<LIFNR>")
    company_code: str | None = Field(None, description="SAP ZBUKR / BUKRS", max_length=10)
    run_date: str | None = Field(None, description="SAP LAUFD (payment run date)", max_length=10)
    run_id: str | None = Field(None, description="SAP LAUFI (payment run identifier)", max_length=10)
    vendor: ErpVendorRef
    amount: float = Field(gt=0, description="SAP RWBTR (absolute value), in the payment currency")
    currency: str = Field(min_length=3, max_length=3, description="SAP WAERS")
    bank: ErpBank
    documents: list[ErpDocument] = Field(default_factory=list, max_length=500)
    requested_by: str | None = Field(None, description="SAP user who created the proposal", max_length=80)

    @field_validator("external_ref")
    @classmethod
    def _ref(cls, v: str) -> str:
        if not REF_RE.fullmatch(v):
            raise ValueError("use 1-128 letters, digits or . _ : / -")
        return v

    def to_request(self) -> PaymentRequest:
        b, v = self.bank, self.vendor
        refs = [d.reference for d in self.documents if d.reference]
        return PaymentRequest(
            requester_name=self.requested_by, vendor_name=v.name, vendor_id=v.id,
            beneficiary_name=b.account_holder or v.name, beneficiary_bank=b.bank_name,
            beneficiary_country=(b.country or v.country or "").upper() or None,
            account_number=b.account, iban=b.iban, swift_bic=b.swift, amount=self.amount, currency=self.currency.upper(),
            invoice_number=", ".join(refs[:5]) or None, vat_number=v.vat_number,
            reason=f"SAP payment run {self.run_date or ''} {self.run_id or ''}".strip())


class ErpRun(BaseModel):
    run_date: str | None = None
    run_id: str | None = None
    company_code: str | None = None
    items: list[ErpPayment] = Field(min_length=1, max_length=2000)


class ErpBankAccount(BaseModel):
    country: str
    bank_name: str = ""
    bank_key: str | None = None
    account: str | None = None
    iban: str | None = None
    swift: str | None = None
    valid_from: str | None = Field(None, description="When this account was verified/added (ISO date)")


class ErpVendor(BaseModel):
    id: str = Field(max_length=40, description="SAP LIFNR / BP number")
    name: str = Field(max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    country: str
    vat_number: str | None = None
    email_domains: list[str] = Field(default_factory=list, description="From the vendor's e-mail addresses (ADR6)")
    bank_accounts: list[ErpBankAccount] = Field(default_factory=list)
    callback_contact: str = Field("Accounts receivable", description="Who to call to confirm a payment")
    callback_phone: str = Field(description="Phone number on file. Callbacks only ever use this number.")
    currency: str = "USD"
    payment_terms_days: int = 30

    def to_vendor(self) -> Vendor:
        accts = [BankAccount(bank_name=a.bank_name or "", account_number=a.account, iban=a.iban, swift_bic=a.swift,
                             country=a.country.upper(), verified_on=a.valid_from or datetime.now(timezone.utc).date().isoformat())
                 for a in self.bank_accounts]
        return Vendor(id=self.id, legal_name=self.name, aliases=self.aliases, country=self.country.upper(),
                      domains=[d.lower().strip() for d in self.email_domains if d.strip()], gstin=None,
                      bank_accounts=accts, callback_contact=self.callback_contact, callback_phone=self.callback_phone,
                      default_currency=self.currency.upper(), payment_terms_days=self.payment_terms_days,
                      vat_number=self.vat_number)


class VendorSync(BaseModel):
    vendors: list[ErpVendor] = Field(min_length=1, max_length=5000)


class HistorySync(BaseModel):
    payments: list[Payment] = Field(min_length=1, max_length=20000)


class WebhookIn(BaseModel):
    url: str = Field(max_length=500)
    events: list[str] = Field(default_factory=lambda: list(EVENTS))
    description: str = Field("", max_length=120)


# -------------------------------------------------------------- payments ----
async def _create_erp_case(d: Deps, a: Access, p: ErpPayment, idem: str | None,
                           limit: bool = True) -> tuple[Case, bool]:
    tid = a.tenant_id
    key = idem or f"erp:{p.external_ref}"
    if existing := await d.store.find_case_by_idempotency(tid, key):
        return existing, False
    if limit:                                 # a payment run is charged once, not per line
        ratelimit.charge(tid, d.settings.rate_limit_per_minute)
    t = await d.store.get_tenant(tid)
    if not t.active_policy_version:
        raise HTTPException(409, "Choose a policy before running checks.")
    raw = json.dumps(p.model_dump(mode="json"), indent=2).encode()
    case = Case(id="c_" + uuid.uuid4().hex[:12], tenant_id=tid, created_by=a.principal.uid, idempotency_key=key,
                external_ref=p.external_ref, request=p.to_request())
    doc = SourceDoc(label=ERP_LABEL, filename=f"{p.external_ref.replace('/', '_')}.json", mime_type="application/json",
                    sha256=hashlib.sha256(raw).hexdigest(), size=len(raw), text=raw.decode())
    doc.storage_uri = await d.store.put_blob(f"{tid}/cases/{case.id}/{ERP_LABEL}", raw, "application/json")
    case.sources.append(doc)
    await d.store.create_case(case)
    await d.store.audit(tid, a.principal.actor, "case.created", f"case/{case.id}",
                        {"inputs": {ERP_LABEL: doc.sha256}, "external_ref": p.external_ref, "channel": "erp"})
    return case, True


async def _wait(d: Deps, cases: list[Case], seconds: float) -> list[Case]:
    """Wait up to `seconds` for every case to finish. At most worker_concurrency cases run in this
    request; the rest (and any a worker already claimed) are left to the workers and polled."""
    if seconds <= 0:
        return cases
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    sem = asyncio.Semaphore(max(1, d.settings.worker_concurrency))

    async def run_here(c: Case) -> None:
        async with sem:
            if task := await start_case(d, c.tenant_id, c.id, None):
                await task
    local = [asyncio.create_task(run_here(c)) for c in cases[: d.settings.worker_concurrency * 4]
             if c.status == CaseStatus.queued]
    done: dict[str, Case] = {}
    try:
        while True:
            for c in cases:
                if c.id not in done and (cur := await d.store.get_case(c.tenant_id, c.id)) and \
                        cur.status in (CaseStatus.completed, CaseStatus.failed):
                    done[c.id] = cur
            if len(done) == len(cases) or loop.time() >= deadline:
                break
            await asyncio.sleep(0.25)
    finally:
        for t in local:           # still running at the deadline: let them finish in the background
            if not t.done():
                _BACKGROUND.add(t)
                t.add_done_callback(_BACKGROUND.discard)
    return [done.get(c.id) or await d.store.get_case(c.tenant_id, c.id) or c for c in cases]


def _status(c: Case) -> dict:
    return {**case_payload(c), "case_url": f"/#/case/{c.id}"}


@router.post("/api/t/{tid}/erp/payments", tags=["ERP integration"])
async def erp_payment(tid: str, request: Request, body: ErpPayment, wait: float = 0):
    """Check one payment. With ?wait=N (max 60) the response carries the recommendation;
    otherwise it returns at once (status 'queued') and the result arrives by webhook."""
    d = _deps(request)
    a = await access(request, tid, Perm.case_create)
    case, _ = await _create_erp_case(d, a, body, request.headers.get("idempotency-key"))
    case = (await _wait(d, [case], min(max(wait, 0), 60)))[0]
    return _status(case)


@router.post("/api/t/{tid}/erp/payment-runs", tags=["ERP integration"])
async def erp_run(tid: str, request: Request, body: ErpRun, wait: float = 0):
    """Check every payment in a payment run (e.g. an F110 proposal) in one call."""
    d = _deps(request)
    a = await access(request, tid, Perm.case_create)
    ratelimit.charge(tid, d.settings.rate_limit_per_minute)          # a payment run is charged once
    cases = []
    for item in body.items:
        item.run_date, item.run_id = item.run_date or body.run_date, item.run_id or body.run_id
        item.company_code = item.company_code or body.company_code
        cases.append((await _create_erp_case(d, a, item, None, limit=False))[0])
    cases = await _wait(d, cases, min(max(wait, 0), 60))
    out = [_status(c) for c in cases]
    return {"run_id": body.run_id, "run_date": body.run_date, "count": len(out),
            "released_automatically": sum(1 for c in cases if c.workflow.state == "released"),   # policy auto_release_below
            "summary": {k: sum(1 for x in out if x["decision"] == k) for k in ("RELEASE", "CALLBACK", "HOLD")},
            "pending": sum(1 for x in out if x["decision"] is None), "items": out}


@router.get("/api/t/{tid}/erp/payments/{external_ref:path}", tags=["ERP integration"])
async def erp_status(tid: str, external_ref: str, request: Request):
    await access(request, tid, Perm.case_read)
    c = await _deps(request).store.find_case_by_external_ref(tid, external_ref)
    if not c:
        raise HTTPException(404, "No check with that external_ref.")
    return _status(c)


# ----------------------------------------------------------- master data ----
@router.put("/api/t/{tid}/erp/vendors", tags=["ERP integration"])
async def erp_vendors(tid: str, request: Request, body: VendorSync):
    a = await access(request, tid, Perm.data_import)
    d = _deps(request)
    vendors = [v.to_vendor() for v in body.vendors]
    await d.store.import_vendors(tid, vendors)
    await d.store.audit(tid, a.principal.actor, "import.vendors", f"tenant/{tid}",
                        {"count": len(vendors), "channel": "erp",
                         "sha256": hashlib.sha256(body.model_dump_json().encode()).hexdigest()})
    return {"upserted": len(vendors)}


@router.put("/api/t/{tid}/erp/payment-history", tags=["ERP integration"])
async def erp_history(tid: str, request: Request, body: HistorySync):
    a = await access(request, tid, Perm.data_import)
    d = _deps(request)
    await d.store.import_payments(tid, body.payments)
    await d.store.audit(tid, a.principal.actor, "import.payments", f"tenant/{tid}",
                        {"count": len(body.payments), "channel": "erp"})
    return {"upserted": len(body.payments)}


# -------------------------------------------------------------- webhooks ----
@router.get("/api/t/{tid}/webhooks", tags=["Webhooks"])
async def list_hooks(tid: str, request: Request):
    await access(request, tid, Perm.tenant_manage)
    return [{k: v for k, v in h.items() if k != "secret"} for h in await _deps(request).store.list_webhooks(tid)]


@router.post("/api/t/{tid}/webhooks", status_code=201, tags=["Webhooks"])
async def add_hook(tid: str, request: Request, body: WebhookIn):
    a = await access(request, tid, Perm.tenant_manage)
    d = _deps(request)
    bad = [e for e in body.events if e not in EVENTS]
    if bad or not body.events:
        raise HTTPException(422, f"Events must be some of: {', '.join(EVENTS)}.")
    if why := await asyncio.to_thread(check_url, body.url, d.settings.webhook_allow_insecure):
        raise HTTPException(422, why)
    secret, _ = new_secret("whsec")
    hook = {"id": "wh_" + uuid.uuid4().hex[:12], "url": body.url, "events": body.events, "description": body.description,
            "secret": secret, "active": True, "created_at": datetime.now(timezone.utc).isoformat(), "created_by": a.principal.uid}
    await d.store.put_webhook(tid, hook)
    await d.store.audit(tid, a.principal.actor, "webhook.created", f"webhook/{hook['id']}", {"url": body.url, "events": body.events})
    return {**{k: v for k, v in hook.items() if k != "secret"}, "secret": secret,
            "note": "Copy the signing secret now; it is shown only once. Verify VBW-Signature on every request."}


@router.delete("/api/t/{tid}/webhooks/{hook_id}", tags=["Webhooks"])
async def delete_hook(tid: str, hook_id: str, request: Request):
    a = await access(request, tid, Perm.tenant_manage)
    d = _deps(request)
    await d.store.delete_webhook(tid, hook_id)
    await d.store.audit(tid, a.principal.actor, "webhook.deleted", f"webhook/{hook_id}", {})
    return {"ok": True}


@router.post("/api/t/{tid}/webhooks/{hook_id}/test", tags=["Webhooks"])
async def test_hook(tid: str, hook_id: str, request: Request):
    await access(request, tid, Perm.tenant_manage)
    d = _deps(request)
    hook = next((h for h in await d.store.list_webhooks(tid) if h["id"] == hook_id), None)
    if not hook:
        raise HTTPException(404, "Webhook not found.")
    # A test goes to this webhook only, as event "test.ping", so receivers can tell it from real cases.
    sample = Case(id="c_test", tenant_id=tid, external_ref="TEST:0000", status=CaseStatus.completed)
    dlv = new_delivery(tid, hook_id, "test.ping", case_payload(sample))
    await d.store.enqueue_delivery(tid, dlv)
    if await d.store.claim_delivery(tid, dlv["id"]):
        await deliver_one(d.store, d.sources.http, tid, dlv, hook, d.settings.webhook_allow_insecure)
    return next((x for x in await d.store.list_deliveries(tid, 20) if x["id"] == dlv["id"]), dlv)


@router.get("/api/t/{tid}/webhooks/deliveries", tags=["Webhooks"])
async def deliveries(tid: str, request: Request, limit: int = 50):
    await access(request, tid, Perm.tenant_manage)
    return await _deps(request).store.list_deliveries(tid, min(limit, 200))


# ---------------------------------------------------------- data sources ----
LIVE_PURPOSE = {"rdap": "Who registered the sender's domain, and when",
                "dns": "Mail servers, SPF and DMARC of the sender's domain",
                "gleif": "Is the payee a real, active company; who owns the SWIFT code",
                "vies": "Is the invoicing company's EU VAT number valid",
                "fx": "Convert amounts to compare with payment history",
                "ifsc": "Indian bank branch codes"}


@router.get("/api/t/{tid}/sources/status", tags=["Data sources"])
async def sources_status(tid: str, request: Request):
    await access(request, tid, Perm.case_read)
    d = _deps(request)
    vendors = await d.store.list_vendors(tid)
    sanc = d.sources.sanctions
    return {
        "sanctions": {"mode": "opensanctions" if d.settings.opensanctions_api_key else "official_lists",
                      "lists": sanc.status()},
        "live": [{"id": i, "name": trace.CATALOG[i]["name"], "purpose": p, "configured": getattr(d.sources, i, None) is not None}
                 for i, p in LIVE_PURPOSE.items()],
        "workspace": {"vendors": len(vendors), "bank_accounts": sum(len(v.bank_accounts) for v in vendors)},
        "ai": {"enabled": d.gemini is not None, "model": d.settings.gemini_model,
               "unavailable": (st["reason"] if d.gemini is not None and (st := d.gemini.status())["state"] != "ok" else None),
               "role": "Reads unstructured requests and writes summaries. Never makes or changes the decision."},
    }
