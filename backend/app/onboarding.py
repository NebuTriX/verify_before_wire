"""Self-serve onboarding: CSV import with row-level validation, sample data, checklist."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date

from pydantic import ValidationError

from .agents.base import gstin_valid, iban_valid, ifsc_valid, swift_valid
from .models import BankAccount, Payment, Vendor

MAX_ROWS = 50_000
VENDOR_COLUMNS = ["vendor_id", "legal_name", "aliases", "country", "domains", "bank_name", "account_number",
                  "iban", "swift_bic", "bank_country", "verified_on", "callback_contact", "callback_phone",
                  "default_currency", "payment_terms_days"]
OPTIONAL_VENDOR_COLUMNS = ["gstin", "ifsc"]   # India; leave blank or omit elsewhere
PAYMENT_COLUMNS = ["payment_id", "vendor_id", "invoice_number", "amount", "currency", "paid_on", "account_ref"]
CCY = re.compile(r"^[A-Z]{3}$")
CC = re.compile(r"^[A-Z]{2}$")
DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")


def template_csv(kind: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "vendors":
        w.writerow(VENDOR_COLUMNS)
        w.writerow(["v-001", "Lion City Logistics Pte Ltd", "Lion City Logistics|LCL", "SG", "lioncitylogistics.com.sg",
                    "DBS Bank Ltd", "0721234567", "", "DBSSSGSG", "SG", "2024-03-14", "Tan Mei Ling, Finance Manager",
                    "+65 6123 4567", "SGD", "30"])
    else:
        w.writerow(PAYMENT_COLUMNS)
        w.writerow(["p-0001", "v-001", "LCL-INV-2025-1001", "78400.00", "SGD", "2025-09-12", "0721234567"])
    return buf.getvalue()


def _rows(text: str, columns: list[str]) -> tuple[list[dict], list[dict]]:
    try:
        return _rows_unsafe(text, columns)
    except csv.Error as e:
        return [], [{"row": 0, "field": "", "message": f"the file isn't valid CSV ({e}); export it again as CSV UTF-8"}]


def _rows_unsafe(text: str, columns: list[str]) -> tuple[list[dict], list[dict]]:
    text = text.replace("\x00", "")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff"), newline=""))
    missing = [c for c in columns if c not in (reader.fieldnames or [])]
    if missing:
        return [], [{"row": 1, "field": ",".join(missing), "message": "missing column(s); download the template"}]
    rows = []
    for i, r in enumerate(reader, start=2):
        if i - 1 > MAX_ROWS:
            return rows, [{"row": i, "field": "", "message": f"more than {MAX_ROWS} rows; split the file"}]
        rows.append({"_row": i, **{k: (v or "").strip() for k, v in r.items() if k}})
    return rows, []


def _date_ok(v: str) -> bool:
    try:
        return date.fromisoformat(v) <= date.today()
    except ValueError:
        return False


def parse_vendors(text: str) -> tuple[list[Vendor], list[dict]]:
    rows, errors = _rows(text, VENDOR_COLUMNS)
    grouped: dict[str, dict] = {}
    for r in rows:
        n, err = r["_row"], lambda f, m: errors.append({"row": r["_row"], "field": f, "message": m})
        for f in ("vendor_id", "legal_name", "country", "bank_name", "callback_contact", "callback_phone"):
            if not r[f]:
                err(f, "required")
        if r["country"] and not CC.match(r["country"].upper()):
            err("country", "use a 2-letter ISO code, e.g. SG")
        domains = [d.lower() for d in r["domains"].split("|") if d]
        for d in domains:
            if not DOMAIN.match(d):
                err("domains", f"'{d}' is not a domain")
        if not (r["account_number"] or r["iban"]):
            err("account_number", "give an account number or an IBAN")
        if r["iban"] and not iban_valid(r["iban"]):
            err("iban", "IBAN checksum fails")
        if r["swift_bic"] and not swift_valid(r["swift_bic"]):
            err("swift_bic", "not a valid SWIFT/BIC")
        if r["verified_on"] and not _date_ok(r["verified_on"]):
            err("verified_on", "use YYYY-MM-DD, not in the future")
        if r["default_currency"] and not CCY.match(r["default_currency"].upper()):
            err("default_currency", "use a 3-letter code, e.g. SGD")
        if any(e["row"] == n for e in errors):
            continue
        if r.get("gstin") and not gstin_valid(r["gstin"]):
            err("gstin", "GSTIN check digit fails")
            continue
        if r.get("ifsc") and not ifsc_valid(r["ifsc"]):
            err("ifsc", "not a valid IFSC")
            continue
        acct = BankAccount(bank_name=r["bank_name"], account_number=r["account_number"] or None,
                           ifsc=(r.get("ifsc") or "").upper() or None,
                           iban=r["iban"] or None, swift_bic=(r["swift_bic"] or "").upper() or None,
                           country=(r["bank_country"] or r["country"]).upper(),
                           verified_on=r["verified_on"] or date.today().isoformat())
        g = grouped.setdefault(r["vendor_id"], {
            "id": r["vendor_id"], "legal_name": r["legal_name"],
            "aliases": [a for a in r["aliases"].split("|") if a], "country": r["country"].upper(),
            "domains": domains, "gstin": (r.get("gstin") or "").upper() or None,
            "bank_accounts": [], "callback_contact": r["callback_contact"],
            "callback_phone": r["callback_phone"], "default_currency": (r["default_currency"] or "USD").upper(),
            "payment_terms_days": int(r["payment_terms_days"] or 30)})
        g["bank_accounts"].append(acct)
        g["domains"] = sorted(set(g["domains"]) | set(domains))
    vendors = []
    for g in grouped.values():
        try:
            vendors.append(Vendor(**g))
        except ValidationError as e:
            errors.append({"row": 0, "field": g["id"], "message": str(e.errors()[0]["msg"])})
    return vendors, errors


def parse_payments(text: str, vendor_ids: set[str]) -> tuple[list[Payment], list[dict]]:
    rows, errors = _rows(text, PAYMENT_COLUMNS)
    out = []
    for r in rows:
        before = len(errors)
        err = lambda f, m: errors.append({"row": r["_row"], "field": f, "message": m})
        for f in PAYMENT_COLUMNS:
            if not r[f]:
                err(f, "required")
        if r["vendor_id"] and r["vendor_id"] not in vendor_ids:
            err("vendor_id", "unknown vendor; import vendors first")
        try:
            amount = float(r["amount"].replace(",", ""))
            if amount <= 0:
                err("amount", "must be positive")
        except ValueError:
            amount = 0
            err("amount", "not a number")
        if r["currency"] and not CCY.match(r["currency"].upper()):
            err("currency", "use a 3-letter code")
        if r["paid_on"] and not _date_ok(r["paid_on"]):
            err("paid_on", "use YYYY-MM-DD, not in the future")
        if len(errors) == before:
            out.append(Payment(id=r["payment_id"], vendor_id=r["vendor_id"], invoice_number=r["invoice_number"],
                               amount=amount, currency=r["currency"].upper(), paid_on=r["paid_on"],
                               account_ref=r["account_ref"]))
    return out, errors


def sample_data(seed_file) -> tuple[list[Vendor], list[Payment]]:
    d = json.loads(seed_file.read_text())
    return [Vendor(**v) for v in d["vendors"]], [Payment(**p) for p in d["payments"]]


STEPS = [
    ("workspace", "Create your workspace"),
    ("policy", "Choose a policy template"),
    ("vendors", "Import your vendor master"),
    ("payments", "Import payment history"),
    ("team", "Invite an approver"),
    ("integration", "Create an API key for your ERP (optional)"),
    ("first_case", "Run your first check"),
]


async def checklist(store, tid: str) -> dict:
    t = await store.get_tenant(tid)
    done = {
        "workspace": t is not None,
        "policy": bool(t and t.active_policy_version),
        "vendors": await store.count(f"tenants/{tid}/vendors") > 0,
        "payments": await store.count(f"tenants/{tid}/payments") > 0,
        "team": len(await store.list_members(tid)) > 1 or bool(await store.list_invites(tid)),
        "integration": bool(await store.list_api_keys(tid)),
        "first_case": bool(await store.list_cases(tid, limit=1)),
    }
    steps = [{"id": k, "title": title, "done": done[k]} for k, title in STEPS]
    return {"steps": steps, "complete": all(s["done"] for s in steps if s["id"] != "integration")}
