"""Human decision workflow (maker-checker). The system recommends; people decide.
Every rule here exists to stop one person - or one compromised account - from moving money alone."""
from __future__ import annotations

from fastapi import HTTPException

from .auth import Access
from .models import Approval, CallbackRecord, Case, Decision
from .tenancy import Perm

MIN_OVERRIDE_REASON = 20


def _who(a: Access) -> dict:
    return {"uid": a.principal.uid, "email": a.principal.email}


def _need(cond: bool, msg: str, code: int = 409):
    if not cond:
        raise HTTPException(code, msg)


def record_callback(case: Case, a: Access, body: dict) -> CallbackRecord:
    _need(case.workflow.state in ("awaiting_decision", "awaiting_second_approval"),
          "Callbacks can be recorded only while a decision is pending.")
    cb = CallbackRecord(**_who(a), **body)
    case.workflow.callbacks.append(cb)
    return cb


def callback_satisfied(case: Case) -> bool:
    return any(cb.outcome == "confirmed" and cb.number_source in ("vendor_master", "company_directory")
               for cb in case.workflow.callbacks)


def act(case: Case, a: Access, action: str, reason: str = "") -> str:
    wf, v = case.workflow, case.verdict
    _need(v is not None, "The investigation hasn't finished.")
    _need(wf.state in ("awaiting_decision", "awaiting_second_approval"), f"Case is already {wf.state}.")
    uid = a.principal.uid

    if action == "clear_compliance":
        _need(a.can(Perm.policy_approve), "Only a policy admin or owner can clear a compliance review.", 403)
        _need(len(reason) >= MIN_OVERRIDE_REASON, f"Give a reason of at least {MIN_OVERRIDE_REASON} characters.", 422)
        wf.approvals.append(Approval(**_who(a), action="clear_compliance", reason=reason))
        return wf.state

    _need(a.can(Perm.case_decide), "Your role can't release or hold payments.", 403)
    _need(uid != case.created_by, "You submitted this payment, so someone else must decide it.", 403)

    if action == "hold":
        wf.approvals.append(Approval(**_who(a), action="hold", reason=reason))
        wf.state = "held"
        return wf.state
    if action == "reject":
        _need(bool(reason), "Say why this is fraud; it trains future policy simulations.", 422)
        wf.approvals.append(Approval(**_who(a), action="reject", reason=reason))
        wf.state, wf.label = "rejected", "fraud"
        return wf.state
    _need(action == "release", f"Unknown action {action!r}.", 422)

    # ---- release ----
    reqs = set(wf.requirements)
    if "callback" in reqs:
        _need(callback_satisfied(case), "Record a confirmed callback to a number from the vendor master "
                                        "or company directory before releasing.")
    if "compliance_review" in reqs:
        _need(any(x.action == "clear_compliance" for x in wf.approvals), "Compliance must clear this case first.")
    override = v.decision == Decision.hold
    if override:
        _need(len(reason) >= MIN_OVERRIDE_REASON,
              f"Releasing a held payment is an override: give a reason of at least {MIN_OVERRIDE_REASON} characters.", 422)
        reqs.add("second_approver")
    releases = [x for x in wf.approvals if x.action == "release"]
    _need(all(x.uid != uid for x in releases), "You already approved this. A different approver must confirm.", 403)
    wf.approvals.append(Approval(**_who(a), action="release", reason=reason))
    if "second_approver" in reqs and len(releases) == 0:
        wf.state = "awaiting_second_approval"
    else:
        wf.state, wf.label = "released", "legit"
    return wf.state
