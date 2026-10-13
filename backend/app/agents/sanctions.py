"""Sanctions agent: screens the beneficiary and vendor names against a live list."""
from __future__ import annotations

from ..models import Evidence, Finding, Severity
from ..sources import SourceUnavailable
from .base import AgentContext, Emit

NAME = "sanctions"
TITLE = "Sanctions screen"


async def run(ctx: AgentContext, emit: Emit) -> None:
    req, client = ctx.request, ctx.sources.sanctions
    names = [n for n in (req.beneficiary_name, req.vendor_name, req.beneficiary_bank) if n]
    if not names:
        await emit(Finding(agent=NAME, code="NOTHING_TO_SCREEN", severity=Severity.low,
                           title="No beneficiary name to screen",
                           detail="The request does not name an account holder.", data_source=client.name))
        return
    try:
        hits = await client.screen(names, req.beneficiary_country)
    except SourceUnavailable as e:
        await emit(Finding(agent=NAME, code="SANCTIONS_UNAVAILABLE", severity=Severity.medium,
                           title="Sanctions screening could not run",
                           detail=f"{e}. Payment must not be released until screening completes.",
                           data_source=client.name))
        return
    if not hits:
        await emit(Finding(agent=NAME, code="SANCTIONS_CLEAR", severity=Severity.info,
                           title="No sanctions match",
                           detail=f"Screened {', '.join(names)}.",
                           evidence=[Evidence(source=client.name, excerpt="0 matches")],
                           data_source=client.name))
        return
    for h in hits[:3]:
        await emit(Finding(
            agent=NAME, code="SANCTIONS_MATCH", severity=Severity.critical,
            title=f"Possible sanctions match: {h.matched}",
            detail=f"'{h.name}' matches '{h.matched}' ({h.score:.0%}) on {h.lists}. "
                   "Compliance must clear this before any payment.",
            evidence=[Evidence(source=client.name, excerpt=h.ref)], data_source=client.name))
