"""Policy engine. A company's rules are data (JSON), not code.

A condition is one of:
  {"all": [cond, ...]}   {"any": [cond, ...]}   {"not": cond}
  {"fact": "<path>", "op": "<op>", "value": <json>}
Ops: eq ne gt gte lt lte in not_in exists contains.
Facts (see build_facts): signal.<CODE>, severity.<CODE>, request.*, vendor.*, check_failed.<agent>,
input.*, score. Nothing in a policy can execute code."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .models import Decision, Finding, Severity, utcnow

OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "exists", "contains"}
AGENT_IDS = ["vendor", "domain", "history", "sanctions", "language", "registry", "media"]
DECISION_RANK = {Decision.release: 0, Decision.callback: 1, Decision.hold: 2}
Requirement = Literal["callback", "second_approver", "compliance_review"]


class Condition(BaseModel):
    all: list["Condition"] | None = None
    any: list["Condition"] | None = None
    not_: "Condition | None" = Field(None, alias="not")
    fact: str | None = None
    op: str | None = None
    value: Any = None

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _one_kind(self):
        kinds = [self.all is not None, self.any is not None, self.not_ is not None, self.fact is not None]
        if sum(kinds) != 1:
            raise ValueError("a condition must be exactly one of all / any / not / fact")
        if self.fact is not None and self.op not in OPS:
            raise ValueError(f"unknown op {self.op!r}; use one of {sorted(OPS)}")
        return self


class Rule(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    description: str
    when: Condition
    add_points: int = Field(0, ge=0, le=100)
    min_decision: Decision | None = None
    require: list[Requirement] = Field(default_factory=list)
    enabled: bool = True


class Thresholds(BaseModel):
    domain_new_days_high: int = Field(90, ge=1)
    domain_new_days_medium: int = Field(365, ge=1)
    amount_ratio_high: float = Field(3.0, gt=1)
    amount_ratio_medium: float = Field(1.8, gt=1)
    sanctions_min_score: float = Field(0.92, ge=0.5, le=1)


class PolicySettings(BaseModel):
    thresholds: Thresholds = Field(default_factory=Thresholds)
    severity_points: dict[Severity, int] = Field(default_factory=lambda: {
        Severity.info: 0, Severity.low: 5, Severity.medium: 15, Severity.high: 30, Severity.critical: 60})
    severity_overrides: dict[str, Severity] = Field(default_factory=dict)   # finding code -> severity
    hold_at: int = Field(60, ge=1, le=100)
    callback_at: int = Field(25, ge=0, le=100)
    critical_forces_hold: bool = True
    checks_enabled: dict[str, bool] = Field(default_factory=lambda: {a: True for a in AGENT_IDS})
    required_checks: list[str] = Field(default_factory=lambda: ["vendor", "sanctions"])
    fail_mode: Literal["hold", "callback"] = "hold"   # what a failed required check forces
    # Straight-through processing for ERP payment runs: a RELEASE with no red flags, no requirements,
    # no failed check and a score below this is released by the policy itself (audited). Off when None.
    auto_release_below: int | None = Field(None, ge=1, le=100)

    @model_validator(mode="after")
    def _sane(self):
        if self.callback_at >= self.hold_at:
            raise ValueError("callback_at must be lower than hold_at")
        if self.auto_release_below is not None and self.auto_release_below > self.callback_at:
            raise ValueError("auto_release_below must not exceed callback_at")
        bad = set(self.required_checks) - set(AGENT_IDS)
        if bad:
            raise ValueError(f"unknown checks in required_checks: {sorted(bad)}")
        return self


class Policy(BaseModel):
    tenant_id: str
    version: int
    name: str
    based_on: str | None = None
    settings: PolicySettings = Field(default_factory=PolicySettings)
    rules: list[Rule] = Field(default_factory=list)
    status: Literal["draft", "pending_approval", "active", "retired", "rejected"] = "draft"
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    submitted_by: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    change_note: str = ""

    @model_validator(mode="after")
    def _unique_ids(self):
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        return self

    def content_hash(self) -> str:
        # exclude_none: settings added later as optional (None = off) leave older policies' hashes unchanged
        body = json.dumps({"settings": self.settings.model_dump(mode="json", exclude_none=True),
                           "rules": [r.model_dump(mode="json", by_alias=True) for r in self.rules]},
                          sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()


# ------------------------------------------------------------- evaluation ----
def _get(facts: dict, path: str):
    cur: Any = facts
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def evaluate(c: Condition, facts: dict) -> bool:
    if c.all is not None:
        return all(evaluate(x, facts) for x in c.all)
    if c.any is not None:
        return any(evaluate(x, facts) for x in c.any)
    if c.not_ is not None:
        return not evaluate(c.not_, facts)
    v, want = _get(facts, c.fact), c.value
    try:
        match c.op:
            case "exists": return (v is not None) == bool(want if want is not None else True)
            case "eq": return v == want
            case "ne": return v != want
            case "gt": return v is not None and v > want
            case "gte": return v is not None and v >= want
            case "lt": return v is not None and v < want
            case "lte": return v is not None and v <= want
            case "in": return v in (want or [])
            case "not_in": return v not in (want or [])
            case "contains": return want in (v or [])
    except TypeError:
        return False  # comparing incompatible types is "not matched", never an error
    return False


def build_facts(findings: list[Finding], request: dict, vendor_known: bool, vendor_country: str | None,
                failed_agents: list[str], inputs: list[str], amount_base: float | None) -> dict:
    signals, sev = {}, {}
    for f in findings:
        signals[f.code] = True
        cur = sev.get(f.code)
        if cur is None or _SEV_ORDER[f.severity] > _SEV_ORDER[Severity(cur)]:
            sev[f.code] = f.severity.value
    return {
        "signal": signals, "severity": sev,
        "request": {**{k: request.get(k) for k in ("amount", "currency", "beneficiary_country",
                                                   "claims_bank_details_changed", "invoice_number")},
                    "amount_base": amount_base},
        "vendor": {"known": vendor_known, "country": vendor_country},
        "check_failed": {a: (a in failed_agents) for a in AGENT_IDS},
        "input": {"labels": inputs, "has_voice_note": any(i.startswith("voice_note") for i in inputs),
                  "has_email": "email" in inputs},
    }


_SEV_ORDER = {Severity.info: 0, Severity.low: 1, Severity.medium: 2, Severity.high: 3, Severity.critical: 4}


class FiredRule(BaseModel):
    id: str
    description: str
    add_points: int = 0
    min_decision: Decision | None = None
    require: list[str] = Field(default_factory=list)


class Outcome(BaseModel):
    decision: Decision
    score: int
    base_score: int
    fired_rules: list[FiredRule]
    requirements: list[str]
    failed_required_checks: list[str]
    reasons: list[str]
    # How the decision was reached, for display: what each finding added, the thresholds,
    # and every step that set or raised the decision. Purely descriptive; decide() is the logic.
    contributions: list[dict[str, Any]] = Field(default_factory=list)
    thresholds: dict[str, int] = Field(default_factory=dict)
    path: list[dict[str, Any]] = Field(default_factory=list)


def apply_overrides(f: Finding, s: PolicySettings) -> Finding:
    sev = s.severity_overrides.get(f.code, f.severity)
    return f.model_copy(update={"severity": sev, "points": s.severity_points[sev]})


def decide(policy: Policy, findings: list[Finding], facts: dict) -> Outcome:
    s = policy.settings
    per_code: dict[str, int] = {}
    top: dict[str, Finding] = {}
    for f in findings:
        pts = s.severity_points[f.severity]
        if f.code not in per_code or pts > per_code[f.code]:
            top[f.code] = f
        per_code[f.code] = max(per_code.get(f.code, 0), pts)
    base = min(100, sum(per_code.values()))
    contributions = sorted(({"code": c, "agent": top[c].agent, "title": top[c].title,
                             "severity": top[c].severity.value, "points": p}
                            for c, p in per_code.items() if p > 0), key=lambda x: -x["points"])
    facts = {**facts, "score": base}

    fired, floor, reqs, reasons, extra = [], Decision.release, [], [], 0
    for r in policy.rules:
        if r.enabled and evaluate(r.when, facts):
            fired.append(FiredRule(id=r.id, description=r.description, add_points=r.add_points,
                                   min_decision=r.min_decision, require=list(r.require)))
            extra += r.add_points
            reqs += r.require
            if r.min_decision and DECISION_RANK[r.min_decision] > DECISION_RANK[floor]:
                floor = r.min_decision
            reasons.append(r.description)
    score = min(100, base + extra)

    path: list[dict[str, Any]] = []
    decision = Decision.release
    if score >= s.hold_at:
        decision = Decision.hold
    elif score >= s.callback_at:
        decision = Decision.callback
    path.append({"kind": "score", "label": f"Risk score {score}", "decision": decision.value, "raised": False})
    critical = [f.title for f in findings if f.severity == Severity.critical]
    if s.critical_forces_hold and critical and decision != Decision.hold:
        decision = Decision.hold
        path.append({"kind": "critical", "label": f"Critical finding: {critical[0]}", "decision": decision.value, "raised": True})
    for r in fired:
        if r.min_decision:
            raised = DECISION_RANK[r.min_decision] > DECISION_RANK[decision]
            path.append({"kind": "rule", "label": r.description, "decision": r.min_decision.value, "raised": raised})
    if DECISION_RANK[floor] > DECISION_RANK[decision]:
        decision = floor

    failed_required = [a for a in s.required_checks if facts["check_failed"].get(a)]
    if failed_required:  # fail closed
        forced = Decision.hold if s.fail_mode == "hold" else Decision.callback
        path.append({"kind": "failed", "label": f"Required check didn't run: {', '.join(failed_required)}",
                     "decision": forced.value, "raised": DECISION_RANK[forced] > DECISION_RANK[decision]})
        if DECISION_RANK[forced] > DECISION_RANK[decision]:
            decision = forced
        reasons.append(f"Required check did not complete: {', '.join(failed_required)}")
    if decision == Decision.callback and "callback" not in reqs:
        reqs.append("callback")
    return Outcome(decision=decision, score=score, base_score=base, fired_rules=fired,
                   requirements=sorted(set(reqs)), failed_required_checks=failed_required, reasons=reasons,
                   contributions=contributions, thresholds={"callback_at": s.callback_at, "hold_at": s.hold_at},
                   path=path)


# -------------------------------------------------------------- templates ----
def _f(fact, op, value=None):
    return {"fact": fact, "op": op, "value": value}


TEMPLATES: dict[str, dict] = {
    "balanced": {
        "name": "Balanced (mid-size corporate AP)",
        "summary": "Holds clear fraud signals, asks for a callback on anything unusual.",
        "settings": {},
        "rules": [
            {"id": "new-account-needs-callback", "description": "Any new or changed bank account needs a callback",
             "when": {"any": [_f("signal.BANK_ACCOUNT_CHANGED", "eq", True), _f("signal.NEVER_PAID_ACCOUNT", "eq", True),
                              _f("request.claims_bank_details_changed", "eq", True)]},
             "min_decision": "CALLBACK", "require": ["callback"]},
            {"id": "large-payment-two-approvers", "description": "Payments above USD 250,000 need a second approver",
             "when": _f("request.amount_base", "gte", 250000), "require": ["second_approver"]},
            {"id": "sanctions-to-compliance", "description": "Possible sanctions match goes to compliance",
             "when": _f("signal.SANCTIONS_MATCH", "eq", True), "min_decision": "HOLD",
             "require": ["compliance_review"]},
        ],
    },
    "strict-bank": {
        "name": "Strict (bank treasury / regulated)",
        "summary": "Lower thresholds, every check required, two approvers above USD 50,000.",
        "settings": {"hold_at": 45, "callback_at": 15, "fail_mode": "hold",
                     "required_checks": ["vendor", "domain", "history", "sanctions"],
                     "thresholds": {"domain_new_days_high": 180, "domain_new_days_medium": 730,
                                    "amount_ratio_high": 2.0, "amount_ratio_medium": 1.4},
                     "severity_overrides": {"NO_DMARC": "medium", "OFF_CYCLE": "medium"}},
        "rules": [
            {"id": "account-change-hold", "description": "Changed bank account is always held",
             "when": {"any": [_f("signal.BANK_ACCOUNT_CHANGED", "eq", True), _f("signal.NEVER_PAID_ACCOUNT", "eq", True)]},
             "min_decision": "HOLD", "require": ["callback", "second_approver"]},
            {"id": "two-approvers-50k", "description": "Payments above USD 50,000 need a second approver",
             "when": _f("request.amount_base", "gte", 50000), "require": ["second_approver"]},
            {"id": "voice-only-instruction", "description": "Voice-note instructions are never enough on their own",
             "when": {"all": [_f("input.has_voice_note", "eq", True), _f("vendor.known", "eq", False)]},
             "add_points": 20, "min_decision": "HOLD"},
            {"id": "sanctions-to-compliance", "description": "Possible sanctions match goes to compliance",
             "when": _f("signal.SANCTIONS_MATCH", "eq", True), "min_decision": "HOLD",
             "require": ["compliance_review"]},
        ],
    },
    "lean": {
        "name": "Lean (startup / low volume)",
        "summary": "Fewer interruptions: small account changes get a callback instead of a hold.",
        "settings": {"hold_at": 75, "callback_at": 30, "critical_forces_hold": False, "fail_mode": "callback",
                     "required_checks": ["sanctions"],
                     "severity_overrides": {"NO_HISTORY": "low", "OFF_CYCLE": "info"}},
        "rules": [
            {"id": "small-account-change-callback", "description": "Account change under USD 20,000: callback is enough",
             "when": {"all": [_f("signal.BANK_ACCOUNT_CHANGED", "eq", True), _f("request.amount_base", "lt", 20000)]},
             "min_decision": "CALLBACK", "require": ["callback"]},
            {"id": "large-account-change-hold", "description": "Account change of USD 20,000 or more is held",
             "when": {"all": [_f("signal.BANK_ACCOUNT_CHANGED", "eq", True), _f("request.amount_base", "gte", 20000)]},
             "min_decision": "HOLD", "require": ["callback"]},
            {"id": "impersonation-hold", "description": "Lookalike domain or failed email authentication is held",
             "when": {"any": [_f("signal.LOOKALIKE_DOMAIN", "eq", True), _f("signal.EMAIL_AUTH_FAILED", "eq", True)]},
             "min_decision": "HOLD"},
            {"id": "sanctions-to-compliance", "description": "Possible sanctions match goes to compliance",
             "when": _f("signal.SANCTIONS_MATCH", "eq", True), "min_decision": "HOLD",
             "require": ["compliance_review"]},
        ],
    },
}


def policy_from_template(tenant_id: str, template: str, version: int, created_by: str) -> Policy:
    t = TEMPLATES[template]
    return Policy(tenant_id=tenant_id, version=version, name=t["name"], based_on=template,
                  settings=PolicySettings(**t["settings"]), rules=[Rule(**r) for r in t["rules"]],
                  created_by=created_by)


# ------------------------------------------------------------- simulation ----
class SimRow(BaseModel):
    case_id: str
    current: Decision
    proposed: Decision
    current_score: int
    proposed_score: int
    label: str | None = None    # confirmed outcome if known: "fraud" | "legit"


def simulate(proposed: Policy, current: Policy | None, cases: list[dict]) -> dict:
    """cases: [{"id", "findings": [...], "facts": {...}, "label"?}] taken from stored decision records."""
    rows = []
    for c in cases:
        raw = [Finding(**f) for f in c["findings"]]
        p = decide(proposed, [apply_overrides(f, proposed.settings) for f in raw], c["facts"])
        cur = decide(current, [apply_overrides(f, current.settings) for f in raw], c["facts"]) if current else p
        rows.append(SimRow(case_id=c["id"], current=cur.decision, proposed=p.decision,
                           current_score=cur.score, proposed_score=p.score, label=c.get("label")))
    changed = [r for r in rows if r.current != r.proposed]
    labelled = [r for r in rows if r.label in ("fraud", "legit")]

    def metrics(attr):
        fraud = [r for r in labelled if r.label == "fraud"]
        legit = [r for r in labelled if r.label == "legit"]
        caught = sum(getattr(r, attr) != Decision.release for r in fraud)
        alarms = sum(getattr(r, attr) != Decision.release for r in legit)
        return {"fraud_cases": len(fraud), "fraud_stopped": caught, "legit_cases": len(legit),
                "legit_interrupted": alarms}

    return {"cases": len(rows), "changed": len(changed),
            "newly_released": [r.case_id for r in changed if r.proposed == Decision.release],
            "rows": [r.model_dump(mode="json") for r in changed],
            "metrics": {"current": metrics("current"), "proposed": metrics("proposed")}}
