"""Runs one investigation for one tenant:
input guard -> intake -> enabled agents in parallel -> tenant policy -> verdict -> decision record.
Every step is published as an Event (persisted + pushed to the live stream)."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from . import guard, intake
from .agents import domain, history, language, sanctions, vendor, verdict
from .agents.base import AgentContext, match_vendor
from .config import Settings
from .gemini import Gemini
from .models import Case, CaseStatus, DecisionRecord, Event, Finding
from .parsing import addr_domain
from .policy import Policy, apply_overrides, build_facts, decide
from .sources import Sources
from .store import Store
from .tenancy import Tenant

log = logging.getLogger(__name__)

AGENTS = [vendor, domain, history, sanctions, language]
AGENT_TIMEOUT_S = 40
TERMINAL = {"case.completed", "case.failed"}


@dataclass
class Deps:
    settings: Settings
    store: Store
    sources: Sources
    gemini: Gemini | None


class Publisher:
    def __init__(self, store: Store, tid: str, cid: str, start_seq: int, queue: asyncio.Queue | None):
        self.store, self.tid, self.cid, self.seq, self.queue = store, tid, cid, start_seq, queue
        self._lock = asyncio.Lock()

    async def __call__(self, type_: str, data: dict | None = None, agent: str | None = None) -> Event:
        async with self._lock:
            self.seq += 1
            ev = Event(seq=self.seq, type=type_, agent=agent, data=data or {})
            await self.store.append_event(self.tid, self.cid, ev)
        if self.queue is not None:
            await self.queue.put(ev)
        return ev


def tenant_settings(base: Settings, t: Tenant, p: Policy) -> Settings:
    """The agents read thresholds from Settings; overlay this tenant's values and policy."""
    th = p.settings.thresholds
    return base.model_copy(update={
        "company_name": t.name,
        "company_domains": ",".join(t.settings.company_domains),
        "base_currency": t.settings.base_currency,
        "business_timezone": t.settings.timezone,
        "new_domain_days_high": th.domain_new_days_high,
        "new_domain_days_medium": th.domain_new_days_medium,
        "amount_ratio_high": th.amount_ratio_high,
        "amount_ratio_medium": th.amount_ratio_medium,
    })


async def investigate(case: Case, tenant: Tenant, policy: Policy, deps: Deps, publish: Publisher) -> Case:
    store, tid = deps.store, tenant.id
    s = tenant_settings(deps.settings, tenant, policy)
    enabled = [a for a in AGENTS if policy.settings.checks_enabled.get(a.NAME, True)]
    t0 = time.perf_counter()
    await publish("case.started", {"case_id": case.id, "policy": {"version": policy.version, "name": policy.name},
                                   "agents": [{"id": a.NAME, "title": a.TITLE} for a in enabled]})
    try:
        raw: list[Finding] = []      # as the agents reported them
        shown: list[Finding] = []    # after this tenant's severity overrides

        async def emit(f: Finding) -> None:
            raw.append(f)
            g = apply_overrides(f, policy.settings)
            shown.append(g)
            await publish("finding", g.model_dump(mode="json"), agent=f.agent)

        # ---- guard + intake ------------------------------------------------
        for f in guard.screen(case.sources):
            await emit(f)
        blobs = {d.label: await store.get_blob(d.storage_uri) for d in case.sources if d.storage_uri}
        await publish("intake.started", {"inputs": [
            {"label": d.label, "filename": d.filename, "type": d.mime_type} for d in case.sources]})
        req, notes = await intake.extract(deps.gemini, case.sources, blobs)
        case.request = req
        read_by = "patterns" if any(intake.PATTERN_NOTE in n for n in notes) else "gemini"
        await publish("intake.completed", {"request": req.model_dump(mode="json"), "notes": notes, "read_by": read_by})

        vendors = await store.list_vendors(tid)
        v, why = match_vendor(req, addr_domain(req.requester_email), vendors)
        hist = await store.payments_for_vendor(tid, v.id) if v else []
        ctx = AgentContext(settings=s, request=req, docs=case.sources, blobs=blobs, vendors=vendors,
                           vendor=v, vendor_match_reason=why, history=hist, sources=deps.sources,
                           gemini=deps.gemini)

        # ---- evidence agents in parallel ----------------------------------
        failed: list[str] = []

        async def run_agent(mod) -> None:
            started, count = time.perf_counter(), 0
            await publish("agent.started", {"title": mod.TITLE}, agent=mod.NAME)

            async def agent_emit(f: Finding) -> None:
                nonlocal count
                count += 1
                await emit(f)
            try:
                await asyncio.wait_for(mod.run(ctx, agent_emit), timeout=AGENT_TIMEOUT_S)
                await publish("agent.completed", {"findings": count,
                                                  "ms": int((time.perf_counter() - started) * 1000)}, agent=mod.NAME)
            except Exception as e:
                log.exception("agent %s failed", mod.NAME)
                failed.append(mod.NAME)
                await publish("agent.failed", {"error": str(e)[:300],
                                               "required": mod.NAME in policy.settings.required_checks},
                              agent=mod.NAME)

        await asyncio.gather(*(run_agent(m) for m in enabled))
        # A check the tenant switched off counts as "not run", never as "passed".
        for a in AGENTS:
            if a not in enabled and a.NAME in policy.settings.required_checks:
                failed.append(a.NAME)
        # Agents that reported their source as unavailable count as failed checks too.
        for f in raw:
            if f.code.endswith("_UNAVAILABLE") and f.agent not in failed:
                failed.append(f.agent)
        case.findings = shown

        # ---- policy decision + narrative -----------------------------------
        await publish("agent.started", {"title": "Decision"}, agent=verdict.NAME)
        facts = build_facts(shown, req.model_dump(mode="json"), v is not None, v.country if v else None,
                            failed, [d.label for d in case.sources], ctx.notes.get("amount_base"))
        outcome = decide(policy, shown, facts)
        case.verdict = await verdict.narrate(outcome, shown, req, v, deps.gemini)
        case.record = DecisionRecord(
            policy_version=policy.version, policy_hash=policy.content_hash(), outcome=outcome.model_dump(mode="json"),
            facts=facts, raw_findings=[f.model_dump(mode="json") for f in raw], model=s.gemini_model,
            prompt_version=s.prompt_version, region=tenant.settings.region,
            input_hashes={d.label: d.sha256 for d in case.sources})
        case.workflow.state = "awaiting_decision"
        case.workflow.requirements = outcome.requirements
        await publish("verdict", case.verdict.model_dump(mode="json"), agent=verdict.NAME)
        case.status, case.lease_until = CaseStatus.completed, None
        await store.save_case(case)
        await store.audit(tid, "system", "case.recommended", f"case/{case.id}",
                          {"decision": outcome.decision.value, "score": outcome.score,
                           "policy_version": policy.version, "policy_hash": case.record.policy_hash})
        await publish("case.completed", {"ms": int((time.perf_counter() - t0) * 1000)})
    except Exception as e:
        log.exception("case %s failed", case.id)
        case.status, case.error = CaseStatus.failed, str(e)[:500]
        await store.save_case(case)
        await store.audit(tid, "system", "case.failed", f"case/{case.id}", {"error": case.error})
        await publish("case.failed", {"error": case.error})
    return case
