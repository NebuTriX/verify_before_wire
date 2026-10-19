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
    vendor_id: str | None = Field(None, description="Vendor id in the customer's ERP (e.g. SAP LIFNR); only set by ERP submissions")
    beneficiary_name: str | None = Field(None, description="Account holder name on the bank details")
    beneficiary_bank: str | None = None
    beneficiary_country: str | None = Field(None, description="ISO-3166 alpha-2 if known")
    account_number: str | None = None
    iban: str | None = None
    swift_bic: str | None = None
    ifsc: str | None = Field(None, description="Indian bank branch code (11 chars), if present")
    gstin: str | None = Field(None, description="Indian GST number of the invoicing entity (15 chars), if present")
    vat_number: str | None = Field(None, description="EU VAT number of the invoicing entity incl. country prefix, if present")
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
    meta: dict[str, Any] = Field(default_factory=dict)   # structured detail for display, e.g. both domains
    # The check's own data source could not answer, so the check did not really run: the policy treats
    # it as a failed check (fail-closed for required checks). Optional AI reviews never set this.
    source_unavailable: bool = False

    def model_post_init(self, _: Any) -> None:
        if not self.points:
            self.points = SEVERITY_POINTS[self.severity]


class Verdict(BaseModel):
    decision: Decision
    requirements: list[str] = Field(default_factory=list)
    fired_rules: list[dict[str, Any]] = Field(default_factory=list)
    risk_score: int
    summary: str
    top_reasons: list[str]
    callback_script: list[str]
    callback_number: str | None = None
    callback_contact: str | None = None
    recommended_actions: list[str]
    narrative_by: Literal["gemini", "template"] = "template"   # who wrote summary + script (never the decision)
    explain: dict[str, Any] = Field(default_factory=dict)    # score build-up, thresholds, decision path (for display)


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


class Approval(BaseModel):
    uid: str
    email: str
    action: Literal["release", "hold", "reject", "clear_compliance"]
    reason: str = ""
    at: datetime = Field(default_factory=utcnow)


class CallbackRecord(BaseModel):
    uid: str
    email: str
    contact: str
    number: str
    number_source: Literal["vendor_master", "company_directory", "other"]
    outcome: Literal["confirmed", "denied", "no_answer"]
    notes: str = ""
    at: datetime = Field(default_factory=utcnow)


class CallAnswer(BaseModel):
    """One question of an AI verification call and what the vendor said. An answer only counts when
    `verified`: its quote was found word for word in the transcript of the recording."""
    question: str
    decisive: bool = True              # identity / request / bank-account questions decide the outcome
    answer: Literal["yes", "no", "unclear"] = "unclear"
    quote: str | None = None
    verified: bool = False


class AiCall(BaseModel):
    """An outbound verification call placed by the AI agent to the number in the vendor master."""
    id: str
    vendor_id: str
    vendor_name: str
    contact: str
    number: str
    number_source: Literal["vendor_master"] = "vendor_master"
    channel: Literal["browser", "twilio"] = "browser"
    status: Literal["dialing", "in_progress", "analysing", "completed", "failed"] = "dialing"
    answers: list[CallAnswer] = Field(default_factory=list)
    recording_uri: str | None = None
    recording_sha256: str | None = None
    recording_mime: str | None = None
    duration_s: float | None = None
    transcript: str | None = None
    transcript_by: Literal["gemini", "browser", "twilio"] | None = None
    second_transcript: list[str] = Field(default_factory=list)   # per-question text from the browser / phone network
    second_transcript_agreement: float | None = None   # word overlap between the two transcripts, 0..1
    # How the answers were checked: "cross_checked" = Gemini transcript + an independent second transcript
    # that must agree; "ai_transcript" = Gemini transcript only; "single_transcript" = only the browser's or
    # phone network's own transcript (consistent with itself, not independently verified).
    verification: Literal["cross_checked", "ai_transcript", "single_transcript"] | None = None
    outcome: Literal["confirmed", "denied", "unclear"] | None = None
    outcome_basis: str | None = None
    provider_ref: str | None = None
    error: str | None = None
    started_by: str = ""
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    accepted_by: str | None = None
    accepted_at: datetime | None = None


class Workflow(BaseModel):
    state: Literal["investigating", "awaiting_decision", "awaiting_second_approval",
                   "released", "held", "rejected"] = "investigating"
    requirements: list[str] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)
    callbacks: list[CallbackRecord] = Field(default_factory=list)
    ai_calls: list[AiCall] = Field(default_factory=list)
    label: Literal["fraud", "legit"] | None = None   # confirmed outcome, feeds policy simulation


class DecisionRecord(BaseModel):
    """Everything needed to reproduce and defend the decision later."""
    policy_version: int
    policy_hash: str
    outcome: dict[str, Any]
    facts: dict[str, Any]
    raw_findings: list[dict[str, Any]]          # before tenant severity overrides
    model: str
    prompt_version: str
    region: str
    input_hashes: dict[str, str]
    decided_at: datetime = Field(default_factory=utcnow)


class Case(BaseModel):
    id: str
    tenant_id: str
    created_by: str = "system"
    idempotency_key: str | None = None
    external_ref: str | None = None              # payment id in the customer's ERP
    status: CaseStatus = CaseStatus.queued
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    scenario: str | None = None
    sources: list[SourceDoc] = Field(default_factory=list)
    request: PaymentRequest | None = None
    findings: list[Finding] = Field(default_factory=list)
    verdict: Verdict | None = None
    record: DecisionRecord | None = None
    workflow: Workflow = Field(default_factory=Workflow)
    lease_until: datetime | None = None
    error: str | None = None


# ---------------------------------------------------------- reference data ---
class BankAccount(BaseModel):
    bank_name: str
    account_number: str | None = None
    iban: str | None = None
    swift_bic: str | None = None
    ifsc: str | None = None
    country: str
    verified_on: str


class Vendor(BaseModel):
    id: str
    legal_name: str
    aliases: list[str] = Field(default_factory=list)
    country: str
    domains: list[str]
    gstin: str | None = None
    vat_number: str | None = None      # EU VAT registration (SAP STCEG)
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
