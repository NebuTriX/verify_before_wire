"""Sanctions agent: screens the beneficiary, vendor and bank names against the official lists."""
from __future__ import annotations

from ..models import Evidence, Finding, Severity
from ..sources import SourceUnavailable
from .base import AgentContext, Emit

NAME = "sanctions"
TITLE = "Sanctions screen"


def _lists_text(coverage: list[dict]) -> str:
    used = [c for c in coverage if c.get("screened", True)]
    parts = [c["label"] + (f" (published {c['published']})" if c.get("published") else "") for c in used]
    return ", ".join(parts) or "no list"


async def run(ctx: AgentContext, emit: Emit) -> None:
    req, client = ctx.request, ctx.sources.sanctions
    names = [n for n in (req.beneficiary_name, req.vendor_name, req.beneficiary_bank) if n]
    if not names:
        await emit(Finding(agent=NAME, code="NOTHING_TO_SCREEN", severity=Severity.low,
                           title="No beneficiary name to screen",
                           detail="The request does not name an account holder.", data_source=client.name))
        return
    try:
        hits, coverage = await client.screen_with_coverage(names, req.beneficiary_country)
    except SourceUnavailable as e:
        await emit(Finding(agent=NAME, code="SANCTIONS_UNAVAILABLE", source_unavailable=True, severity=Severity.medium,
                           title="Sanctions screening could not run",
                           detail=f"{e}. Payment must not be released until screening completes.",
                           data_source=client.name))
        return
    # Provenance: which list versions were used goes into the decision record with every finding.
    meta = {"lists": coverage}
    skipped = [c for c in coverage if not c.get("screened", True)]
    if skipped:
        await emit(Finding(agent=NAME, code="SANCTIONS_PARTIAL", severity=Severity.info,
                           title=f"{len(skipped)} sanctions list{'s' if len(skipped) > 1 else ''} not yet loaded",
                           detail="Screened without " + ", ".join(f"{c['label']} ({c.get('error')})" for c in skipped)
                                  + ". The check will include them once their download finishes.",
                           data_source=client.name, meta=meta))
    if not hits:
        await emit(Finding(agent=NAME, code="SANCTIONS_CLEAR", severity=Severity.info,
                           title="No sanctions match",
                           detail=f"Screened {', '.join(names)} against {_lists_text(coverage)}.",
                           evidence=[Evidence(source=client.name, excerpt="0 matches")],
                           data_source=client.name, meta=meta))
        return
    for h in hits[:3]:
        await emit(Finding(
            agent=NAME, code="SANCTIONS_MATCH", severity=Severity.critical,
            title=f"Possible sanctions match: {h.matched}",
            detail=f"'{h.name}' matches '{h.matched}' ({h.score:.0%}) on {h.lists}. "
                   "Compliance must clear this before any payment.",
            evidence=[Evidence(source=client.name, excerpt=h.ref)], data_source=client.name, meta=meta))
