from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

import pytest

from app.config import Settings
from app.models import PaymentRequest
from app.sources import DomainDns, DomainRegistration
from app.store import MemoryStore

ROOT = Path(__file__).resolve().parents[1]


class FakeRdap:
    name = "RDAP (fake)"

    async def lookup(self, domain):
        if domain in {"lioncitylogistics.com.sg", "pacrim-components.co.jp"}:
            return DomainRegistration(domain, True, datetime.now(timezone.utc) - timedelta(days=3000), "Reg")
        return DomainRegistration(domain, False)


class FakeDns:
    name = "DNS (fake)"

    async def inspect(self, domain):
        if domain.endswith((".sg", ".jp")):
            return DomainDns(domain, True, ["mx.example"], "v=spf1 -all", "v=DMARC1; p=reject")
        return DomainDns(domain, False, [], None, None)


class FakeFx:
    name = "FX (fake)"

    async def rate(self, a, b):
        return {"SGD": 0.77, "USD": 1.0}[a.upper()]


class FakeSanctions:
    name = "Sanctions (fake)"

    async def screen(self, names, country=None):
        return []


class FakeSources:
    def __init__(self):
        self.rdap, self.dns, self.fx, self.sanctions = FakeRdap(), FakeDns(), FakeFx(), FakeSanctions()

    async def aclose(self):
        pass


class FakeGemini:
    """Deterministic stand-in: delegates intake to the regex fallback plus scenario facts."""

    def text_part(self, text):
        return text

    def bytes_part(self, data, mime):
        return f"<{mime} {len(data)} bytes>"

    async def json(self, schema, system, parts):
        text = "\n".join(p for p in parts if isinstance(p, str))
        name = schema.__name__
        if name == "PaymentRequest":
            if "848-123456-838" in text:
                return PaymentRequest(
                    requester_name="Daniel Koh", requester_claimed_role="CFO", vendor_name="Lion City Logistics",
                    beneficiary_name="Lion City Logistic Services Ltd", beneficiary_bank="HSBC Hong Kong",
                    account_number="848-123456-838", swift_bic="HSBCHKHH", amount=480000, currency="SGD",
                    invoice_number="LCL-INV-2026-1047", deadline="before 5pm today",
                    claims_bank_details_changed=True,
                    conflicts=["Amount: email says SGD 480,000 but transcript says 450,000"])
            if "LCL-INV-2026-0988" in text:
                return PaymentRequest(vendor_name="Lion City Logistics Pte Ltd",
                                      beneficiary_name="Lion City Logistics Pte Ltd", beneficiary_bank="DBS Bank Ltd",
                                      account_number="0721234567", swift_bic="DBSSSGSG", amount=78400,
                                      currency="SGD", invoice_number="LCL-INV-2026-0988", deadline="within 30 days")
            return PaymentRequest(vendor_name="Pacific Rim Components", beneficiary_name="Pacific Rim Components Co., Ltd.",
                                  beneficiary_bank="Bangkok Bank PCL", account_number="101-4-55821-9",
                                  swift_bic="BKKBTHBK", amount=61250, currency="USD", invoice_number="PRC-24117",
                                  claims_bank_details_changed=True)
        if name == "LanguageReport":
            sigs = []
            if "board meetings" in text:
                sigs = [
                    {"tactic": "secrecy", "severity": "high", "source": "email",
                     "quote": "please keep this between us"},
                    {"tactic": "callback_discouraged", "severity": "high", "source": "email",
                     "quote": "I'm in board meetings all day so I can't take calls"},
                    {"tactic": "urgency", "severity": "medium", "source": "email",
                     "quote": "this quote was invented by the model"},  # must be dropped
                ]
            return schema(signals=[{**s, "explanation": "x"} for s in sigs], benign_reasoning="routine")
        return schema(summary="s", callback_script=["a"], recommended_actions=["b"])


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, store_backend="memory", auth_mode="dev",
                    seed_file=ROOT / "scripts" / "seed_data.json",
                    demo_dir=ROOT.parent / "demo", rate_limit_per_minute=1000, demo_enabled=True)


@pytest.fixture
def sources():
    return FakeSources()


@pytest.fixture(params=["memory", "sql"])
def client(request, settings, sources, tmp_path):
    """Every API test runs twice: in-memory store and a real SQL database (SQLite here,
    PostgreSQL in production) through the same code path."""
    from fastapi.testclient import TestClient

    from app.blobs import FileBlobs
    from app.main import app
    from app.orchestrator import Deps
    if request.param == "sql":
        from app.sqlstore import SqlStore
        url = os.getenv("VBW_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path}/vbw.db"
        store = SqlStore(url, FileBlobs(str(tmp_path / "blobs")))
    else:
        store = MemoryStore()
    app.state.deps = Deps(settings=settings, store=store, sources=sources, gemini=FakeGemini())
    with TestClient(app) as c:
        if request.param == "sql" and os.getenv("VBW_TEST_DATABASE_URL"):
            c.portal.call(store.purge_for_tests)   # shared PostgreSQL in CI: start each test clean
        yield c
    del app.state.deps


def H(email):
    return {"X-Dev-User": email}


OWNER, APPROVER, APPROVER2, ADMIN2, CLERK = ("owner@acme.com", "ap1@acme.com", "ap2@acme.com",
                                             "risk@acme.com", "clerk@acme.com")


def make_workspace(client, template="balanced", owner=OWNER, team=True):
    r = client.post("/api/workspaces", headers=H(owner), json={
        "name": "Acme Holdings", "company_domains": ["acme-holdings.com"], "base_currency": "USD",
        "template": template})
    assert r.status_code == 201, r.text
    tid = r.json()["tenant_id"]
    assert client.post(f"/api/t/{tid}/sample-data", headers=H(owner)).status_code == 200
    if team:
        for email, role in ((APPROVER, "approver"), (APPROVER2, "approver"), (ADMIN2, "policy_admin"),
                            (CLERK, "clerk")):
            link = client.post(f"/api/t/{tid}/invites", headers=H(owner),
                               json={"email": email, "role": role}).json()["invite_link"]
            token = link.split("#invite=")[1]
            assert client.post("/api/invites/accept", headers=H(email), json={"token": token}).status_code == 200
    return tid


def run_case(client, tid, scenario, who=CLERK):
    import json as _j
    cid = client.post(f"/api/t/{tid}/demo/{scenario}", headers=H(who)).json()["case_id"]
    events = []
    with client.stream("GET", f"/api/t/{tid}/cases/{cid}/stream", headers=H(who)) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                events.append(_j.loads(line[5:]))
    return cid, events


def verdict_of(events):
    return next(e for e in events if e["type"] == "verdict")["data"]
