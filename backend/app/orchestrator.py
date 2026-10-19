"""Runs one investigation for one tenant:
input guard -> intake -> enabled agents in parallel -> tenant policy -> verdict -> decision record.
Every step is published as an Event (persisted + pushed to the live stream)."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from . import guard, intake, metrics, trace, webhooks
from .agents import domain, history, language, media, registry, sanctions, vendor, verdict
from .agents.base import AgentContext, match_vendor
from .config import Settings
from .gemini import Gemini
from .models import Approval, Case, CaseStatus, Decision, DecisionRecord, Event, Finding, PaymentRequest
from .parsing import addr_domain
from .policy import Policy, apply_overrides, build_facts, decide
from .sources import Sources
from .store import Store
from .tenancy import Tenant

log = logging.getLogger(__name__)

AGENTS = [vendor, domain, history, sanctions, registry, language, media]
AGENT_TIMEOUT_S = 40
TERMINAL = {"case.completed", "case.failed"}
ERP_LABEL = "erp_payment"     # the source label of a payment submitted as structured ERP data


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
    """Run one investigation end to end; the case comes back completed (or failed, with the reason)."""
    return await _Investigation(case, tenant, policy, deps, publish).run()


class _Investigation:
    """One investigation's state and steps, in pipeline order (see run())."""

    def __init__(self, case: Case, tenant: Tenant, policy: Policy, deps: Deps, publish: Publisher):
        self.case, self.tenant, self.policy, self.deps, self.publish = case, tenant, policy, deps, publish
        self.store, self.tid = deps.store, tenant.id
        self.settings = tenant_settings(deps.settings, tenant, policy)
        self.enabled = [a for a in AGENTS if policy.settings.checks_enabled.get(a.NAME, True)]
        self.raw: list[Finding] = []     # as the checks reported them (kept in the decision record)
        self.shown: list[Finding] = []   # after this tenant's severity overrides (what the policy sees)
        self.failed: list[str] = []      # checks that did not really run
        self.t0 = time.perf_counter()

    async def run(self) -> Case:
        await self.publish("case.started", {
            "case_id": self.case.id, "policy": {"version": self.policy.version, "name": self.policy.name},
            "agents": [{"id": a.NAME, "title": a.TITLE} for a in self.enabled],
            "sources": trace.CATALOG, "gemini": self.deps.gemini is not None})
        sink_token = trace.set_sink(self.publish)
        try:
            for f in guard.screen(self.case.sources):
                await self.emit(f)
            blobs = {d.label: await self.store.get_blob(d.storage_uri) for d in self.case.sources if d.storage_uri}
            req, read_by = await self.read_request(blobs)
            ctx = await self.reference_data(req, blobs)
            await self.run_checks(ctx)
            self.count_failed_checks()
            outcome, auto_released = await self.decide(ctx)
            await self.finish(outcome, auto_released, read_by)
        except Exception as e:
            await self.fail(e)
        finally:
            trace.reset_sink(sink_token)
        return self.case

    async def emit(self, f: Finding) -> None:
        self.raw.append(f)
        g = apply_overrides(f, self.policy.settings)
        self.shown.append(g)
        await self.publish("finding", g.model_dump(mode="json"), agent=f.agent)

    async def read_request(self, blobs: dict[str, bytes]) -> tuple[PaymentRequest, str]:
        """What is being asked for. ERP submissions are already structured; documents are read by
        Gemini, or by pattern matching when AI is unavailable."""
        sources = self.case.sources
        await self.publish("intake.started", {"inputs": [
            {"label": d.label, "filename": d.filename, "type": d.mime_type} for d in sources]})
        with trace.agent_scope("intake"):
            if self.case.request is not None and any(d.label == ERP_LABEL for d in sources):
                req, notes, read_by = self.case.request, ["Structured payment data from your ERP; no AI reading needed."], "erp"
            else:
                req, notes, read_by = await intake.extract(self.deps.gemini, sources, blobs)
                req.vendor_id = None    # trusted only from the ERP: text in an email must never pick the vendor record
        self.case.request = req
        await self.publish("intake.completed", {"request": req.model_dump(mode="json"), "notes": notes, "read_by": read_by})
        return req, read_by

    async def reference_data(self, req: PaymentRequest, blobs: dict[str, bytes]) -> AgentContext:
        """The workspace's own records the checks compare against: the vendor and its payment history."""
        with trace.agent_scope("vendor"):
            ts = time.perf_counter()
            vendors = await self.store.list_vendors(self.tid)
            v, why = match_vendor(req, addr_domain(req.requester_email), vendors)
            await trace.record("vendor_master", req.beneficiary_name or req.requester_email or "the request",
                               "ok", f"matched {v.legal_name}" if v else f"no match among {len(vendors)} vendors",
                               int((time.perf_counter() - ts) * 1000))
        hist = await self.store.payments_for_vendor(self.tid, v.id) if v else []
        if v:
            with trace.agent_scope("history"):
                await trace.record("payment_history", v.legal_name, "ok", f"{len(hist)} past payment{'s' if len(hist) != 1 else ''}")
        return AgentContext(settings=self.settings, request=req, docs=self.case.sources, blobs=blobs, vendors=vendors,
                            vendor=v, vendor_match_reason=why, history=hist, sources=self.deps.sources,
                            gemini=self.deps.gemini)

    async def run_checks(self, ctx: AgentContext) -> None:
        """Every enabled check in parallel, each with its own time limit."""
        async def run_one(mod) -> None:
            started, count = time.perf_counter(), 0

            async def check_emit(f: Finding) -> None:
                nonlocal count
                count += 1
                await self.emit(f)
            with trace.agent_scope(mod.NAME):
                await self.publish("agent.started", {"title": mod.TITLE}, agent=mod.NAME)
                try:
                    await asyncio.wait_for(mod.run(ctx, check_emit), timeout=AGENT_TIMEOUT_S)
                    await self.publish("agent.completed", {"findings": count,
                                                           "ms": int((time.perf_counter() - started) * 1000)}, agent=mod.NAME)
                except Exception as e:
                    log.exception("agent %s failed", mod.NAME)
                    self.failed.append(mod.NAME)
                    await self.publish("agent.failed", {"error": str(e)[:300],
                                                        "required": mod.NAME in self.policy.settings.required_checks},
                                       agent=mod.NAME)
        await asyncio.gather(*(run_one(m) for m in self.enabled))

    def count_failed_checks(self) -> None:
        # A required check the tenant switched off counts as "not run", never as "passed".
        for a in AGENTS:
            if a not in self.enabled and a.NAME in self.policy.settings.required_checks:
                self.failed.append(a.NAME)
        # A check whose data source could not answer did not really run either.
        for f in self.raw:
            if f.source_unavailable and f.agent not in self.failed:
                self.failed.append(f.agent)
        self.case.findings = self.shown

    async def decide(self, ctx: AgentContext):
        """The policy decides from the findings (no AI); Gemini may only word the summary."""
        case, policy, req, v = self.case, self.policy, ctx.request, ctx.vendor
        await self.publish("agent.started", {"title": "Decision"}, agent=verdict.NAME)
        facts = build_facts(self.shown, req.model_dump(mode="json"), v is not None, v.country if v else None,
                            self.failed, [d.label for d in case.sources], ctx.notes.get("amount_base"))
        outcome = decide(policy, self.shown, facts)
        with trace.agent_scope(verdict.NAME):
            case.verdict = await verdict.narrate(outcome, self.shown, req, v, self.deps.gemini)
        case.record = DecisionRecord(
            policy_version=policy.version, policy_hash=policy.content_hash(), outcome=outcome.model_dump(mode="json"),
            facts=facts, raw_findings=[f.model_dump(mode="json") for f in self.raw], model=self.settings.gemini_model,
            prompt_version=self.settings.prompt_version, region=self.tenant.settings.region,
            input_hashes={d.label: d.sha256 for d in case.sources})
        case.workflow.state = "awaiting_decision"
        case.workflow.requirements = outcome.requirements
        auto = policy.settings.auto_release_below
        auto_released = (auto is not None and outcome.decision == Decision.release and not outcome.requirements
                         and not self.failed and outcome.score < auto)
        if auto_released:
            case.workflow.state = "released"
            case.workflow.approvals.append(Approval(
                uid="system", email=f"policy v{policy.version}", action="release",
                reason=f"Released automatically: RELEASE with no requirements, risk {outcome.score} below {auto} (policy v{policy.version})."))
        return outcome, auto_released

    async def finish(self, outcome, auto_released: bool, read_by: str) -> None:
        case, policy = self.case, self.policy
        await self.publish("verdict", case.verdict.model_dump(mode="json"), agent=verdict.NAME)
        case.status, case.lease_until = CaseStatus.completed, None
        await self.store.save_case(case)
        await self.store.audit(self.tid, "system", "case.recommended", f"case/{case.id}",
                               {"decision": outcome.decision.value, "score": outcome.score,
                                "policy_version": policy.version, "policy_hash": case.record.policy_hash})
        channel = "erp" if read_by == "erp" else "documents"
        metrics.CASES.labels(outcome.decision.value, channel).inc()
        metrics.INVESTIGATION.labels(channel).observe(time.perf_counter() - self.t0)
        if auto_released:
            metrics.AUTO_RELEASED.inc()
            await self.store.audit(self.tid, "system", "case.auto_released", f"case/{case.id}",
                                   {"score": outcome.score, "policy_version": policy.version,
                                    "below": policy.settings.auto_release_below})
        await self.publish("case.completed", {"ms": int((time.perf_counter() - self.t0) * 1000)})
        await webhooks.emit(self.store, self.tid, "case.recommended", case)

    async def fail(self, e: Exception) -> None:
        log.exception("case %s failed", self.case.id)
        self.case.status, self.case.error = CaseStatus.failed, str(e)[:500]
        await self.store.save_case(self.case)
        await self.store.audit(self.tid, "system", "case.failed", f"case/{self.case.id}", {"error": self.case.error})
        metrics.CASE_FAILED.inc()
        await self.publish("case.failed", {"error": self.case.error})
        await webhooks.emit(self.store, self.tid, "case.failed", self.case)


async def start_case(d: Deps, tid: str, cid: str, queue: asyncio.Queue | None) -> asyncio.Task | None:
    """Claim the case (a lease in the store, so only one web or worker process runs it) and
    start the investigation. None if someone else holds the lease or the case is finished."""
    if not await d.store.try_claim(tid, cid, d.settings.lease_seconds):
        return None
    case = await d.store.get_case(tid, cid)
    t = await d.store.get_tenant(tid)
    pol = await d.store.active_policy(tid)
    if case is None or t is None or pol is None:
        return None
    prior = await d.store.events_after(tid, cid, 0)
    pub = Publisher(d.store, tid, cid, prior[-1].seq if prior else 0, queue)

    async def run() -> Case:
        # Keep the lease alive while working, so a slow investigation is never picked up a second time.
        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(max(5, d.settings.lease_seconds / 3))
                await d.store.renew_lease(tid, cid, d.settings.lease_seconds)
        hb = asyncio.create_task(heartbeat())
        try:
            return await investigate(case, t, pol, d, pub)
        finally:
            hb.cancel()
    return asyncio.create_task(run())
