"""Payment-history agent: amount, duplicates and timing against this vendor's history.
Amounts are normalised to the base currency with live ECB rates."""
from __future__ import annotations

import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

from ..models import Evidence, Finding, Severity
from ..sources import SourceUnavailable
from .base import AgentContext, Emit, norm_account

NAME = "history"
TITLE = "Payment history"
SRC = "Payment history (Firestore)"


async def run(ctx: AgentContext, emit: Emit, now: datetime | None = None) -> None:
    s, req, v = ctx.settings, ctx.request, ctx.vendor
    base = s.base_currency
    tz = ZoneInfo(s.business_timezone)
    now = (now or datetime.now(tz)).astimezone(tz)

    amount_base = None
    if req.amount and req.currency:
        try:
            rate = await ctx.sources.fx.rate(req.currency, base)
            amount_base = req.amount * rate
            ctx.notes["amount_base"] = amount_base
            await emit(Finding(
                agent=NAME, code="FX_NORMALISED", severity=Severity.info,
                title=f"{req.currency.upper()} {req.amount:,.0f} ≈ {base} {amount_base:,.0f}",
                detail=f"Converted at today's ECB reference rate {rate:.4f}.",
                evidence=[Evidence(source=ctx.sources.fx.name, excerpt=f"1 {req.currency.upper()} = {rate:.4f} {base}")],
                data_source=ctx.sources.fx.name))
        except SourceUnavailable as e:
            await emit(Finding(agent=NAME, code="FX_UNAVAILABLE", severity=Severity.low,
                               title="Live FX rate unavailable",
                               detail=f"{e}. Amount compared in original currency only.",
                               data_source=ctx.sources.fx.name))

    if v is None or not ctx.history:
        if v is not None:
            await emit(Finding(agent=NAME, code="NO_HISTORY", severity=Severity.medium,
                               title="No payment history with this vendor",
                               detail="First-ever payment: there is no baseline to compare against.",
                               data_source=SRC))
        else:
            await emit(Finding(agent=NAME, code="NO_HISTORY", severity=Severity.medium,
                               title="No payment history for this payee",
                               detail="The payee is unknown, so this would be a first payment.",
                               data_source=SRC))
    else:
        hist = sorted(ctx.history, key=lambda p: p.paid_on)
        same_ccy = [p.amount for p in hist if p.currency.upper() == (req.currency or "").upper()]
        if req.amount and same_ccy:
            med = statistics.median(same_ccy)
            mx = max(same_ccy)
            ratio = req.amount / med if med else 0
            if ratio >= s.amount_ratio_high or req.amount > mx * 1.5:
                sev = Severity.high
            elif ratio >= s.amount_ratio_medium:
                sev = Severity.medium
            else:
                sev = Severity.info
            await emit(Finding(
                agent=NAME, code="AMOUNT_ANOMALY" if sev != Severity.info else "AMOUNT_NORMAL",
                severity=sev,
                title=(f"Amount is {ratio:.1f}× this vendor's median invoice" if sev != Severity.info
                       else "Amount is within this vendor's normal range"),
                detail=f"{len(same_ccy)} past payments: median {req.currency.upper()} {med:,.0f}, "
                       f"largest {mx:,.0f}. This request: {req.amount:,.0f}.",
                evidence=[Evidence(source=SRC, excerpt=f"median {med:,.0f}; max {mx:,.0f}; n={len(same_ccy)}")],
                data_source=SRC))
        if req.invoice_number:
            dup = next((p for p in hist if p.invoice_number.strip().lower()
                        == req.invoice_number.strip().lower()), None)
            if dup:
                await emit(Finding(
                    agent=NAME, code="DUPLICATE_INVOICE", severity=Severity.high,
                    title=f"Invoice {req.invoice_number} was already paid",
                    detail=f"Paid {dup.currency} {dup.amount:,.0f} on {dup.paid_on}.",
                    evidence=[Evidence(source=SRC, excerpt=f"{dup.id}: {dup.invoice_number} {dup.paid_on}")],
                    data_source=SRC))
        acct = norm_account(req.iban or req.account_number)
        if acct and acct not in {norm_account(p.account_ref) for p in hist}:
            await emit(Finding(
                agent=NAME, code="NEVER_PAID_ACCOUNT", severity=Severity.medium,
                title="We have never paid this account before",
                detail=f"All {len(hist)} past payments to {v.legal_name} went to other accounts.",
                evidence=[Evidence(source=SRC, excerpt=f"account …{acct[-4:]} not in history")],
                data_source=SRC))
        last = hist[-1]
        cadence = _median_gap_days(hist)
        if cadence:
            days_since = (now.date() - datetime.fromisoformat(last.paid_on).date()).days
            if days_since < cadence * 0.4:
                await emit(Finding(
                    agent=NAME, code="OFF_CYCLE", severity=Severity.low,
                    title="Request arrives off the vendor's usual billing cycle",
                    detail=f"Usually paid every ~{cadence:.0f} days; last paid {days_since} days ago.",
                    evidence=[Evidence(source=SRC, excerpt=f"last payment {last.paid_on}")],
                    data_source=SRC))

    if req.deadline and any(w in req.deadline.lower() for w in ("today", "tonight", "now", "asap",
                                                                  "within the hour", "eod", "5pm", "5 pm")):
        late = now.hour >= 15 or now.weekday() >= 4
        await emit(Finding(
            agent=NAME, code="SAME_DAY_DEADLINE", severity=Severity.medium if late else Severity.low,
            title="Same-day deadline" + (" late in the week/day" if late else ""),
            detail="Fraudsters set deadlines near cut-off so there is no time for a callback. "
                   f"Received {now:%a %H:%M} {s.business_timezone}.",
            evidence=[Evidence(source="request", excerpt=req.deadline)], data_source="Payment request"))


def _median_gap_days(hist) -> float | None:
    dates = [datetime.fromisoformat(p.paid_on) for p in hist]
    gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
    return statistics.median(gaps) if len(gaps) >= 3 else None
