import json
from datetime import datetime, timedelta, timezone
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
    return Settings(store_backend="memory", seed_file=ROOT / "scripts" / "seed_data.json",
                    demo_dir=ROOT.parent / "demo", company_domains="acme-holdings.com")


@pytest.fixture
def app_client(settings):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.orchestrator import Deps
    seed = json.loads(settings.seed_file.read_text())
    app.state.deps = Deps(settings=settings, store=MemoryStore(seed), sources=FakeSources(), gemini=FakeGemini())
    with TestClient(app) as c:
        yield c
    del app.state.deps
