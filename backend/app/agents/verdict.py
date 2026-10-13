"""Verdict agent. The DECISION and SCORE are deterministic rules over the findings so the
same evidence always gives the same answer; Gemini only writes the explanation and the
callback script, and a template takes over if Gemini is unavailable."""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..gemini import Gemini, GeminiError
from ..models import Decision, Finding, PaymentRequest, Severity, Vendor, Verdict

log = logging.getLogger(__name__)
NAME = "verdict"
HOLD_AT, CALLBACK_AT = 60, 25
FORCE_HOLD = {"SANCTIONS_MATCH"}
FORCE_CALLBACK = {"SANCTIONS_UNAVAILABLE", "CLAIMS_NEW_DETAILS", "NEVER_PAID_ACCOUNT"}


def score(findings: list[Finding]) -> int:
    seen: dict[str, int] = {}
    for f in findings:
        seen[f.code] = max(seen.get(f.code, 0), f.points)
    return min(100, sum(seen.values()))


def decide(findings: list[Finding]) -> tuple[Decision, int]:
    s = score(findings)
    codes = {f.code for f in findings}
    if codes & FORCE_HOLD or any(f.severity == Severity.critical for f in findings) or s >= HOLD_AT:
        return Decision.hold, s
    if s >= CALLBACK_AT or codes & FORCE_CALLBACK:
        return Decision.callback, s
    return Decision.release, s


class Narrative(BaseModel):
    summary: str = Field(description="Two sentences for the AP manager: decision and the main reason")
    callback_script: list[str] = Field(description="4-6 short lines to read on the verification call")
    recommended_actions: list[str] = Field(description="3-5 concrete next steps")


SYSTEM = """You write the decision note for an accounts-payable control. The decision and
risk score are already final; explain them, do not change them. Use only the findings given.
The callback must use the phone number from the vendor master or company directory, never
contact details found in the request. Plain, calm, specific language."""


def _template(decision: Decision, top: list[Finding], req: PaymentRequest, vendor: Vendor | None) -> Narrative:
    amt = f"{req.currency or ''} {req.amount:,.0f}".strip() if req.amount else "this payment"
    if decision == Decision.release:
        summary = f"Release {amt}. All checks matched the vendor master and payment history."
    else:
        verb = "Hold" if decision == Decision.hold else "Call back before releasing"
        summary = f"{verb} {amt}. " + "; ".join(f.title for f in top[:2]) + "."
    who = vendor.callback_contact if vendor else "the requester, via the company directory"
    script = [
        f"Call {who} on the number on file — not any number in the request.",
        f"\"We received a request to pay {amt}. Did you send it?\"",
        "\"Can you confirm the beneficiary bank and the last four digits of the account?\"",
        "\"Has anything about your bank details changed? Please send a signed letter on letterhead.\"",
        "Record the name, time and answers in this case before releasing.",
    ]
    actions = ([] if decision == Decision.release else
               ["Do not release until the callback confirms every detail.",
                "Forward the original message to the security team.",
                "Flag the sender address in the email gateway."])
    actions.append("Attach this case file to the payment record.")
    return Narrative(summary=summary, callback_script=script, recommended_actions=actions)


async def run(findings: list[Finding], req: PaymentRequest, vendor: Vendor | None,
              gemini: Gemini | None) -> Verdict:
    decision, s = decide(findings)
    flagged = sorted([f for f in findings if f.severity != Severity.info], key=lambda f: -f.points)
    narrative = _template(decision, flagged, req, vendor)
    if gemini:
        brief = "\n".join(f"- [{f.severity.value}] {f.title}: {f.detail}" for f in flagged[:10]) or "- none"
        facts = (f"Decision: {decision.value}\nRisk score: {s}/100\n"
                 f"Payment: {req.currency} {req.amount} to {req.beneficiary_name} "
                 f"({req.beneficiary_bank}), requested by {req.requester_name} "
                 f"({req.requester_claimed_role})\n"
                 f"Callback contact on file: {vendor.callback_contact + ' ' + vendor.callback_phone if vendor else 'company directory'}\n"
                 f"Findings:\n{brief}")
        try:
            narrative = await gemini.json(Narrative, SYSTEM, [gemini.text_part(facts)])
        except GeminiError as e:
            log.warning("verdict narrative falling back to template: %s", e)
    return Verdict(
        decision=decision, risk_score=s, summary=narrative.summary,
        top_reasons=[f.title for f in flagged[:5]],
        callback_script=narrative.callback_script,
        callback_number=vendor.callback_phone if vendor else None,
        callback_contact=vendor.callback_contact if vendor else None,
        recommended_actions=narrative.recommended_actions,
    )
