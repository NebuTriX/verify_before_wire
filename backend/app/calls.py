"""AI verification calls: when a payment is held, an agent calls the vendor on the number in your
vendor master, asks fixed questions built from the case facts, records the call and transcribes it.

Guarantees, so nothing is invented:
  * Who is called comes from the vendor master in the database, never from the payment request.
  * The questions are templates filled with case facts (amount, invoice, account digits): no model
    writes them.
  * Every answer must quote the vendor's own words, and the quote must appear word for word in the
    transcript; otherwise the answer is "unclear". When a second, independent transcript exists (the
    browser's or the phone network's), the answer must agree with it too ("cross_checked").
  * Without Gemini, the browser's own transcript is the only one: answers are then consistent with it
    but not independently verified, and the call says so ("single_transcript").
  * The outcome is a fixed rule over verified answers. The recording is kept with its SHA-256.
  * The call never releases a payment. A person listens, reads, and accepts it as the callback the
    policy requires; until then it is evidence only.
"""
from __future__ import annotations

import re
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from .agents.base import match_vendor, norm_account
from .models import AiCall, CallAnswer, Case, PaymentRequest, Vendor
from .parsing import addr_domain

STRONG_YES = re.compile(r"\b(yes|yeah|yep|yup|correct|confirm(ed)?|absolutely|affirmative|that'?s (right|correct))\b", re.I)
WEAK_YES = re.compile(r"\b(i did|we did|we have|it is|that'?s us|speaking|sure|right|indeed)\b", re.I)
NEGATION = re.compile(r"\b(no|nope|not|never|didn'?t|did not|haven'?t|have not|wasn'?t|isn'?t|don'?t|wrong)\b", re.I)
HEDGE = re.compile(r"\b(not sure|don'?t know|no idea|need to check|have to check|let me check|maybe|i think|probably)\b", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def quote_in(quote: str | None, transcript: str | None) -> bool:
    """The quote appears word for word (ignoring case and punctuation) in the transcript."""
    q, t = _norm(quote or ""), _norm(transcript or "")
    return len(q) >= 2 and f" {q} " in f" {t} "


def polarity(text: str) -> tuple[Literal["yes", "no", "unclear"], str | None]:
    """Rule-based reading of one answer, with the exact words that decided it. Hedging, or a yes and
    a no in the same answer, is "unclear": only a clear answer can count."""
    t = text or ""
    if HEDGE.search(t):
        return "unclear", None
    strong, neg = STRONG_YES.search(t), NEGATION.search(t)
    if strong and neg:
        return "unclear", None
    if neg:
        return "no", neg.group(0)
    if yes := strong or WEAK_YES.search(t):
        return "yes", yes.group(0)
    return "unclear", None


def vendor_for(case: Case, vendors: list[Vendor]) -> Vendor | None:
    """The vendor record this case concerns, resolved again from the database (same rule as the
    investigation), so the number dialled is a fact on file."""
    req = case.request or PaymentRequest()
    v, _ = match_vendor(req, addr_domain(req.requester_email), vendors)
    return v


def questions(case: Case, v: Vendor) -> list[tuple[str, bool]]:
    req = case.request or PaymentRequest()
    amt = f"{req.currency or ''} {req.amount:,.2f}".strip() if req.amount is not None else "a payment"
    inv = f" for invoice {req.invoice_number}" if req.invoice_number else ""
    acct = norm_account(req.iban or req.account_number)
    on_file = {norm_account(a.iban or a.account_number) for a in v.bank_accounts}
    q = [(f"Am I speaking with {v.callback_contact} at {v.legal_name}?", True),
         (f"Did {v.legal_name} ask us to pay {amt}{inv}?", True)]
    if acct and acct not in on_file:
        q.append((f"Did {v.legal_name} ask us to send this payment to a new bank account ending in {acct[-4:]}?", True))
    elif acct:
        q.append((f"Should this payment go to your account ending in {acct[-4:]}?", True))
    q.append(("Is there anything else we should know before we pay?", False))
    return q


def new_call(case: Case, v: Vendor, channel: str, uid: str) -> AiCall:
    return AiCall(id="call_" + uuid.uuid4().hex[:12], vendor_id=v.id, vendor_name=v.legal_name, contact=v.callback_contact,
                  number=v.callback_phone, channel=channel, started_by=uid,
                  answers=[CallAnswer(question=q, decisive=d) for q, d in questions(case, v)])


class _ModelAnswer(BaseModel):
    index: int = Field(description="0-based question index")
    answer: Literal["yes", "no", "unclear"]
    quote: str | None = Field(None, description="The callee's exact words that answer it, copied verbatim from the transcript")


class CallAnalysis(BaseModel):
    transcript: str = Field(description="Verbatim transcript of everything the callee says, in order")
    answers: list[_ModelAnswer] = Field(default_factory=list)


ANALYSIS_PROMPT = """You transcribe and review a recorded phone call made by a bank's payment-verification agent to a
supplier. The agent asked these questions, in order:
{questions}
The recording contains the supplier's spoken answers{marks}.
1. transcript: write exactly what the supplier says, verbatim. Never add words that are not spoken.
2. answers: for each question, 'yes' or 'no' only if the supplier clearly answered it, with `quote` copied
   word for word from your transcript. If an answer is missing, ambiguous or off-topic: 'unclear' and no quote.
Do not infer, summarise or guess intentions."""


def apply_answers(call: AiCall, transcript: str, model: list[_ModelAnswer] | None,
                  browser_answers: list[str] | None, by: str) -> None:
    """Fill the call's answers from a transcript, keeping only what the transcript proves, then derive
    the outcome with a fixed rule."""
    call.transcript, call.transcript_by = transcript, by
    by_idx = {m.index: m for m in (model or [])}
    for i, a in enumerate(call.answers):
        a.answer, a.quote, a.verified = "unclear", None, False
        if not a.decisive:          # open question: record what was said, never read it as yes/no
            said = (m.quote if (m := by_idx.get(i)) is not None else None) or \
                   (browser_answers[i].strip()[:200] if model is None and browser_answers and i < len(browser_answers) else None)
            if said and quote_in(said, transcript):
                a.quote, a.verified = said, True
            continue
        if (m := by_idx.get(i)) is not None and m.answer != "unclear" and quote_in(m.quote, transcript):
            a.answer, a.quote, a.verified = m.answer, m.quote, True
        elif model is None and browser_answers and i < len(browser_answers):
            ans, words = polarity(browser_answers[i])
            if ans != "unclear" and quote_in(words, transcript):
                a.answer, a.quote, a.verified = ans, browser_answers[i].strip()[:200], True
        # A second, independent transcript must not contradict the first.
        if a.verified and model is not None and browser_answers and i < len(browser_answers) and browser_answers[i].strip():
            other, _ = polarity(browser_answers[i])
            if other != "unclear" and other != a.answer:
                a.answer, a.verified = "unclear", False
                a.quote = f"Transcripts disagree: “{a.quote}” vs “{browser_answers[i].strip()[:120]}”"
    if model is not None and browser_answers:
        a_words, b_words = set(_norm(transcript).split()), set(_norm(" ".join(browser_answers)).split())
        call.second_transcript_agreement = round(len(a_words & b_words) / max(1, len(a_words | b_words)), 2)
    has_second = bool(browser_answers and any(x.strip() for x in browser_answers))
    call.verification = (None if not transcript else "single_transcript" if model is None
                         else "cross_checked" if has_second else "ai_transcript")
    decisive = [a for a in call.answers if a.decisive]
    if any(a.answer == "no" and a.verified for a in decisive):
        call.outcome = "denied"
        call.outcome_basis = "The vendor said no to: " + "; ".join(a.question.rstrip("?") for a in decisive if a.answer == "no" and a.verified) + "."
    elif decisive and all(a.answer == "yes" and a.verified for a in decisive):
        call.outcome = "confirmed"
        call.outcome_basis = "The vendor confirmed every key question, each backed by their own words in the transcript."
        if call.verification == "single_transcript":
            call.outcome_basis += " Only one transcript was available, so listen to the recording before accepting."
    else:
        call.outcome = "unclear"
        missing = [a.question for a in decisive if not (a.verified and a.answer == "yes")]
        call.outcome_basis = "Not every key question has a clear, quoted answer: " + "; ".join(q.rstrip("?") for q in missing) + "."
