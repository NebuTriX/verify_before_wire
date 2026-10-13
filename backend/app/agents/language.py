"""Language & consistency agent. Gemini reads the words for social-engineering tactics;
every quote it returns is verified against the source text before it is shown."""
from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from ..gemini import GeminiError
from ..guard import UNTRUSTED_NOTE
from ..models import Evidence, Finding, Severity
from .base import AgentContext, Emit

log = logging.getLogger(__name__)
NAME = "language"
TITLE = "Language & consistency"

Tactic = Literal["urgency", "secrecy", "authority_pressure", "callback_discouraged",
                 "channel_change", "unusual_process", "emotional_pressure", "inconsistency"]


class Signal(BaseModel):
    tactic: Tactic
    severity: Literal["low", "medium", "high"]
    source: str = Field(description="Label of the input the quote comes from")
    quote: str = Field(description="Exact words copied from that input, max 25 words")
    explanation: str = Field(description="One sentence: why this is a red flag in a payment request")


class LanguageReport(BaseModel):
    signals: list[Signal] = Field(default_factory=list)
    benign_reasoning: str = Field(description="If no signals, why the language looks routine")


SYSTEM = """You are a payments-fraud investigator reviewing the words of a payment request.
Find social-engineering tactics used in business email compromise and deepfake CEO fraud:
urgency, secrecy, authority pressure, discouraging a callback, moving to a new channel,
bypassing normal process, emotional pressure, and inconsistencies between sources.
Rules:
- Quote words EXACTLY as they appear in the named source. Never paraphrase inside a quote.
- Only report what is present. Routine invoices often contain polite deadlines; do not flag
  ordinary payment terms ("due in 30 days") as urgency.
- Do not judge whether a voice is synthetic; judge only the words.
- Return at most 6 signals, the strongest first.
""" + UNTRUSTED_NOTE

FLAG = {"high": Severity.high, "medium": Severity.medium, "low": Severity.low}
TITLES = {
    "urgency": "Manufactured urgency", "secrecy": "Asks for secrecy",
    "authority_pressure": "Leans on authority to skip checks",
    "callback_discouraged": "Discourages a callback", "channel_change": "Moves to a new channel",
    "unusual_process": "Bypasses the normal payment process",
    "emotional_pressure": "Emotional pressure", "inconsistency": "Sources contradict each other",
}
# Fallback patterns if Gemini is unavailable. Deliberately narrow.
PATTERNS = {
    "urgency": r"\b(urgent(ly)?|immediately|asap|before \d{1,2}\s?(pm|am)|today|right away)\b",
    "secrecy": r"\b(confidential|keep this (between us|quiet)|don'?t (tell|mention)|discreet)\b",
    "callback_discouraged": r"\b(don'?t|do not|no need to) (call|phone|ring)|can'?t take calls|in (a )?(board )?meeting\b",
    "channel_change": r"\b(whatsapp|telegram|signal|personal (email|number))\b",
}


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t.replace("’", "'").replace("“", '"').replace("”", '"')).strip().lower()


def verify_quote(quote: str, text: str) -> bool:
    q, t = _norm(quote), _norm(text)
    if not q:
        return False
    return q in t or fuzz.partial_ratio(q, t) >= 92


def _texts(ctx: AgentContext) -> dict[str, str]:
    texts = {d.label: d.text for d in ctx.docs if d.text}
    if ctx.request.transcript:
        for d in ctx.docs:
            if d.label.startswith("voice_note") and d.label not in texts:
                texts[d.label] = ctx.request.transcript
    return texts


async def run(ctx: AgentContext, emit: Emit) -> None:
    texts = _texts(ctx)
    for c in ctx.request.conflicts:
        await emit(Finding(agent=NAME, code="SOURCE_CONFLICT", severity=Severity.high,
                           title=TITLES["inconsistency"], detail=c,
                           data_source="Cross-check of all inputs (Gemini)"))
    if not texts:
        await emit(Finding(agent=NAME, code="NO_TEXT", severity=Severity.info,
                           title="No written or spoken words to analyse",
                           detail="Only documents were provided.", data_source="Payment request"))
        return

    signals: list[Signal] = []
    used_model = False
    if ctx.gemini:
        corpus = "\n\n".join(f"<source label=\"{k}\">\n{v[:12000]}\n</source>" for k, v in texts.items())
        try:
            report = await ctx.gemini.json(LanguageReport, SYSTEM, [ctx.gemini.text_part(corpus)])
            signals, used_model = report.signals, True
        except GeminiError as e:
            log.warning("language agent falling back to patterns: %s", e)
    if not used_model:
        seen: set[str] = set()
        for label, text in texts.items():
            for tactic, pat in PATTERNS.items():
                if tactic in seen:
                    continue
                if m := re.search(pat, text, re.I):
                    seen.add(tactic)
                    start = max(0, m.start() - 40)
                    signals.append(Signal(tactic=tactic, severity="medium", source=label,
                                          quote=text[start:m.end() + 40].strip(),
                                          explanation="Matched a known pressure phrase."))

    emitted = 0
    for sig in signals:
        src_text = texts.get(sig.source) or next((t for t in texts.values() if verify_quote(sig.quote, t)), "")
        if not verify_quote(sig.quote, src_text):
            log.info("dropped unverifiable quote from %s: %r", sig.source, sig.quote)
            continue
        emitted += 1
        await emit(Finding(
            agent=NAME, code=f"LANG_{sig.tactic.upper()}", severity=FLAG[sig.severity],
            title=TITLES[sig.tactic], detail=sig.explanation,
            evidence=[Evidence(source=sig.source, excerpt=sig.quote)],
            data_source=f"{'Gemini' if used_model else 'Pattern'} language analysis, quote verified"))
    if not emitted:
        await emit(Finding(agent=NAME, code="LANGUAGE_ROUTINE", severity=Severity.info,
                           title="Language reads as routine",
                           detail="No pressure, secrecy or callback-avoidance found in the words.",
                           data_source="Gemini language analysis" if used_model else "Pattern analysis"))
