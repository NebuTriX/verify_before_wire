"""Verdict. The DECISION comes from the tenant's policy (app/policy.py). Gemini only writes
the explanation and callback script; a template takes over if Gemini is unavailable."""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..gemini import Gemini, GeminiError
from ..models import Decision, Finding, PaymentRequest, Severity, Vendor, Verdict
from ..policy import Outcome

log = logging.getLogger(__name__)
NAME = "verdict"


class Narrative(BaseModel):
    summary: str = Field(description="Two sentences for the AP manager: decision and the main reason")
    callback_script: list[str] = Field(description="4-6 short lines to read on the verification call")
    recommended_actions: list[str] = Field(description="3-5 concrete next steps")


SYSTEM = """You write the decision note for an accounts-payable control. The decision, score and
requirements are final; explain them, never change or soften them. Use only the facts given.
The callback must use the phone number from the vendor master or company directory, never
contact details found in the request. Plain, calm, specific language."""

REQ_TEXT = {"callback": "Confirm by phone using the number on file",
            "second_approver": "A second approver must confirm the release",
            "compliance_review": "Compliance must clear the sanctions alert"}


def template(o: Outcome, top: list[Finding], req: PaymentRequest, vendor: Vendor | None) -> Narrative:
    amt = f"{req.currency or ''} {req.amount:,.0f}".strip() if req.amount else "this payment"
    if o.decision == Decision.release:
        summary = f"Release {amt}. Checks matched the vendor master and payment history."
    else:
        verb = "Hold" if o.decision == Decision.hold else "Call back before releasing"
        why = [f.title for f in top[:2]] or o.reasons[:2]
        summary = f"{verb} {amt}. " + "; ".join(why) + "."
    who = vendor.callback_contact if vendor else "the requester, via the company directory"
    script = [
        f"Call {who} on the number on file, not any number in the request.",
        f"\"We received a request to pay {amt}. Did you send it?\"",
        "\"Please confirm the beneficiary bank and the last four digits of the account.\"",
        "\"Have your bank details changed? If so, we need a signed letter on letterhead.\"",
        "Record who you spoke to and their answers in this case.",
    ]
    actions = [REQ_TEXT[r] for r in o.requirements if r in REQ_TEXT]
    if o.decision != Decision.release:
        actions += ["Forward the original message to the security team."]
    actions.append("Keep this case file with the payment record.")
    return Narrative(summary=summary, callback_script=script, recommended_actions=actions)


async def narrate(o: Outcome, findings: list[Finding], req: PaymentRequest, vendor: Vendor | None,
                  gemini: Gemini | None) -> Verdict:
    flagged = sorted([f for f in findings if f.severity != Severity.info], key=lambda f: -f.points)
    n = template(o, flagged, req, vendor)
    if gemini:
        facts = (f"Decision: {o.decision.value}\nRisk score: {o.score}/100\n"
                 f"Requirements: {', '.join(o.requirements) or 'none'}\n"
                 f"Policy rules fired: {'; '.join(r.description for r in o.fired_rules) or 'none'}\n"
                 f"Payment: {req.currency} {req.amount} to {req.beneficiary_name} ({req.beneficiary_bank}), "
                 f"requested by {req.requester_name} ({req.requester_claimed_role})\n"
                 f"Callback contact on file: "
                 f"{vendor.callback_contact + ' ' + vendor.callback_phone if vendor else 'company directory'}\n"
                 "Findings:\n" + ("\n".join(f"- [{f.severity.value}] {f.title}: {f.detail}" for f in flagged[:10]) or "- none"))
        try:
            n = await gemini.json(Narrative, SYSTEM, [gemini.text_part(facts)])
        except GeminiError as e:
            log.warning("verdict narrative falling back to template: %s", e)
    return Verdict(decision=o.decision, risk_score=o.score, summary=n.summary,
                   top_reasons=[f.title for f in flagged[:5]] or o.reasons[:5],
                   requirements=o.requirements,
                   fired_rules=[r.model_dump(mode="json") for r in o.fired_rules],
                   callback_script=n.callback_script,
                   callback_number=vendor.callback_phone if vendor else None,
                   callback_contact=vendor.callback_contact if vendor else None,
                   recommended_actions=n.recommended_actions)
