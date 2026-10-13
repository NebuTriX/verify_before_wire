"""Domain models shared by the API, the agents and the report."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Severity(str, Enum):
    info = "info"
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


SEVERITY_POINTS = {
    Severity.info: 0,
    Severity.low: 5,
    Severity.medium: 15,
    Severity.high: 30,
    Severity.critical: 60,
}


class Decision(str, Enum):
    release = "RELEASE"
    callback = "CALLBACK"
    hold = "HOLD"


# ---------------------------------------------------------------- intake -----
class Citation(BaseModel):
    source: str = Field(description="Source label, e.g. 'email', 'voice_note', 'invoice'.")
    quote: str = Field(description="Exact words copied from that source.")


class PaymentRequest(BaseModel):
    """What the payment request asks for, merged across every input."""
    requester_name: str | None = None
    requester_email: str | None = None
    requester_claimed_role: str | None = Field(None, description="e.g. CFO, vendor billing")
    vendor_name: str | None = Field(None, description="Company that should receive the money")
    beneficiary_name: str | None = Field(None, description="Account holder name on the bank details")
    beneficiary_bank: str | None = None
    beneficiary_country: str | None = Field(None, description="ISO-3166 alpha-2 if known")
    account_number: str | None = None
    iban: str | None = None
    swift_bic: str | None = None
    amount: float | None = None
    currency: str | None = Field(None, description="ISO-4217 code")
    invoice_number: str | None = None
    deadline: str | None = Field(None, description="Deadline as written, e.g. 'today 5pm'")
    reason: str | None = None
    claims_bank_details_changed: bool = False
    transcript: str | None = Field(None, description="Verbatim transcript of any audio input")
    citations: list[Citation] = Field(default_factory=list)
    conflicts: list[str] = Field(
        default_factory=list,
        description="Facts that differ between sources, each naming both sources",
    )


class SourceDoc(BaseModel):
    """One uploaded input after parsing."""
    label: str                       # email, voice_note, invoice, email_attachment_1 ...
    filename: str
    mime_type: str
    sha256: str
    size: int
    text: str | None = None          # parsed text (email body, transcript)
    headers: dict[str, str] = Field(default_factory=dict)
    storage_uri: str | None = None


# --------------------------------------------------------------- findings ----
class Evidence(BaseModel):
    source: str                      # input label or external source name
    excerpt: str


class Finding(BaseModel):
    agent: str
    code: str                        # stable machine code, e.g. BANK_ACCOUNT_CHANGED
    severity: Severity
    title: str
    detail: str
    evidence: list[Evidence] = Field(default_factory=list)
    data_source: str                 # where the fact came from, e.g. "RDAP (rdap.org)"
    points: int = 0

    def model_post_init(self, _: Any) -> None:
        if not self.points:
            self.points = SEVERITY_POINTS[self.severity]


class Verdict(BaseModel):
    decision: Decision
    risk_score: int
    summary: str
    top_reasons: list[str]
    callback_script: list[str]
    callback_number: str | None = None
    callback_contact: str | None = None
    recommended_actions: list[str]


# ------------------------------------------------------------------ events ---
EventType = Literal[
    "case.started", "intake.started", "intake.completed",
    "agent.started", "finding", "agent.completed", "agent.failed",
    "verdict", "case.completed", "case.failed", "heartbeat",
]


class Event(BaseModel):
    seq: int
    type: str
    agent: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=utcnow)


class CaseStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class Case(BaseModel):
    id: str
    status: CaseStatus = CaseStatus.queued
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    scenario: str | None = None
    sources: list[SourceDoc] = Field(default_factory=list)
    request: PaymentRequest | None = None
    findings: list[Finding] = Field(default_factory=list)
    verdict: Verdict | None = None
    lease_until: datetime | None = None
    error: str | None = None


# ---------------------------------------------------------- reference data ---
class BankAccount(BaseModel):
    bank_name: str
    account_number: str | None = None
    iban: str | None = None
    swift_bic: str | None = None
    country: str
    verified_on: str


class Vendor(BaseModel):
    id: str
    legal_name: str
    aliases: list[str] = Field(default_factory=list)
    country: str
    domains: list[str]
    bank_accounts: list[BankAccount]
    callback_contact: str
    callback_phone: str                # from the vendor master, never from the request
    default_currency: str
    payment_terms_days: int = 30


class Payment(BaseModel):
    id: str
    vendor_id: str
    invoice_number: str
    amount: float
    currency: str
    paid_on: str                       # ISO date
    account_ref: str                   # account_number or iban paid to
