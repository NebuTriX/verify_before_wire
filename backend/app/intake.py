"""Intake: Gemini reads every input (email text, PDF/image invoices, voice notes natively)
and merges them into one structured PaymentRequest. Header facts override model output."""
from __future__ import annotations

import logging
import re
from email.utils import parseaddr

from .gemini import Gemini, GeminiError
from .models import PaymentRequest, SourceDoc
from .guard import UNTRUSTED_NOTE
from .parsing import AUDIO, DOCS

log = logging.getLogger(__name__)

SYSTEM = """You are the intake clerk of an accounts-payable fraud checkpoint.
You receive one payment request delivered through several inputs, each wrapped with its label.
Extract ONE merged payment request.
- Copy identifiers exactly (account numbers, IBAN, SWIFT/BIC, invoice numbers, amounts).
- ifsc / gstin: Indian bank branch code and GST number if they appear; copy exactly.
- currency: ISO-4217 code. beneficiary_country: ISO-3166 alpha-2 (infer from IBAN/SWIFT if absent).
- transcript: verbatim transcript of any audio input, otherwise null.
- citations: up to 8 exact quotes supporting the key fields, each with the input label.
- conflicts: every fact that differs between inputs (amount, account, payee, deadline, who is asking),
  naming both labels. Empty list if they agree.
- claims_bank_details_changed: true only if an input says the payee's bank details changed.
Never invent values. Use null when a field is not present.
""" + UNTRUSTED_NOTE

AMOUNT_RE = re.compile(r"\b(USD|SGD|EUR|GBP|INR|JPY|AUD|HKD|MYR)\s?\$?\s?([\d,]+(?:\.\d{1,2})?)", re.I)
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}\s?[A-Z0-9]{1,4}\b")
SWIFT_RE = re.compile(r"\b(?:SWIFT|BIC)[:\s]*([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b", re.I)
BENE_RE = re.compile(r"\b(?:beneficiary|account name|payee)\s*:\s*([^\n]+)", re.I)
BANK_RE = re.compile(r"\bbank\s*:\s*([^\n]+)", re.I)
IFSC_FIND = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
GSTIN_FIND = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
ACCT_RE = re.compile(r"\b(?:account|a/c|acct)(?:\s*(?:no\.?|number|#))?[:\s]*([0-9][0-9\- ]{6,20}[0-9])", re.I)


def _parts(gemini: Gemini, docs: list[SourceDoc], blobs: dict[str, bytes], with_audio: bool):
    parts = []
    for d in docs:
        parts.append(gemini.text_part(f'<input label="{d.label}" file="{d.filename}" type="{d.mime_type}">'))
        if d.mime_type in AUDIO:
            if with_audio:
                parts.append(gemini.bytes_part(blobs[d.label], d.mime_type))
            else:
                parts.append(gemini.text_part("[audio unavailable; rely on any transcript input]"))
        elif d.mime_type in DOCS:  # PDFs and images go to Gemini natively
            parts.append(gemini.bytes_part(blobs[d.label], d.mime_type))
        else:
            parts.append(gemini.text_part((d.text or "")[:20000]))
        parts.append(gemini.text_part("</input>"))
    return parts


def regex_fallback(docs: list[SourceDoc]) -> PaymentRequest:
    text = "\n".join(d.text or "" for d in docs)
    req = PaymentRequest()
    if m := AMOUNT_RE.search(text):
        req.currency, req.amount = m.group(1).upper(), float(m.group(2).replace(",", ""))
    if m := IBAN_RE.search(text):
        req.iban = m.group(0).replace(" ", "")
    if m := SWIFT_RE.search(text):
        req.swift_bic = m.group(1).upper()
    if m := IFSC_FIND.search(text):
        req.ifsc = m.group(0)
    if m := GSTIN_FIND.search(text):
        req.gstin = m.group(0)
    if m := BENE_RE.search(text):
        req.beneficiary_name = req.vendor_name = m.group(1).strip()
    if m := BANK_RE.search(text):
        req.beneficiary_bank = m.group(1).strip()
    if re.search(r"\b(new|changed|updated) (bank|banking|account|remittance)|moved our banking", text, re.I):
        req.claims_bank_details_changed = True
    if m := ACCT_RE.search(text):
        req.account_number = re.sub(r"\D", "", m.group(1))
    return req


def apply_header_facts(req: PaymentRequest, docs: list[SourceDoc]) -> PaymentRequest:
    email = next((d for d in docs if d.label == "email"), None)
    if email and email.headers.get("From"):
        name, addr = parseaddr(email.headers["From"])
        req.requester_email = addr or req.requester_email
        req.requester_name = req.requester_name or name or None
    if req.currency:
        req.currency = req.currency.upper()[:3]
    if req.beneficiary_country:
        req.beneficiary_country = req.beneficiary_country.upper()[:2]
    if not req.beneficiary_country and req.iban:
        req.beneficiary_country = req.iban[:2].upper()
    if not req.beneficiary_country and req.swift_bic and len(req.swift_bic) >= 6:
        req.beneficiary_country = req.swift_bic[4:6].upper()
    return req


async def extract(gemini: Gemini | None, docs: list[SourceDoc], blobs: dict[str, bytes]
                  ) -> tuple[PaymentRequest, list[str]]:
    """Returns (request, notes). Notes describe any fallback taken, for the audit log."""
    notes: list[str] = []
    if gemini is None:
        notes.append("Gemini disabled: used pattern extraction")
        return apply_header_facts(regex_fallback(docs), docs), notes
    has_audio = any(d.mime_type in AUDIO for d in docs)
    try:
        req = await gemini.json(PaymentRequest, SYSTEM, _parts(gemini, docs, blobs, True))
    except GeminiError as e:
        if not has_audio:
            notes.append(f"Gemini intake failed ({e}); used pattern extraction")
            return apply_header_facts(regex_fallback(docs), docs), notes
        notes.append(f"Audio processing failed ({e}); retried with transcript text")
        try:
            req = await gemini.json(PaymentRequest, SYSTEM, _parts(gemini, docs, blobs, False))
        except GeminiError as e2:
            notes.append(f"Gemini intake failed ({e2}); used pattern extraction")
            req = regex_fallback(docs)
    if has_audio and not req.transcript:
        t = next((d.text for d in docs if d.label.startswith("transcript") and d.text), None)
        req.transcript = t
    return apply_header_facts(req, docs), notes
