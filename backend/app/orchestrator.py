"""Runs one investigation: intake -> five agents in parallel -> verdict.
Every step is published as an Event (persisted + pushed to the live stream)."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from . import intake
from .agents import domain, history, language, sanctions, vendor, verdict
from .agents.base import AgentContext, match_vendor
from .config import Settings
from .gemini import Gemini
from .models import Case, CaseStatus, Event, Finding
from .parsing import addr_domain
from .sources import Sources
from .store import Store

log = logging.getLogger(__name__)

AGENTS = [vendor, domain, history, sanctions, language]
AGENT_TIMEOUT_S = 40


@dataclass
class Deps:
    settings: Settings
    store: Store
    sources: Sources
    gemini: Gemini | None


class Publisher:
    """Assigns sequence numbers, persists each event, and fans it out to a local queue."""

    def __init__(self, store: Store, case_id: str, start_seq: int, queue: asyncio.Queue | None):
        self.store, self.case_id, self.seq, self.queue = store, case_id, start_seq, queue
        self._lock = asyncio.Lock()

    async def __call__(self, type_: str, data: dict | None = None, agent: str | None = None) -> Event:
        async with self._lock:
            self.seq += 1
            ev = Event(seq=self.seq, type=type_, agent=agent, data=data or {})
            await self.store.append_event(self.case_id, ev)
        if self.queue is not None:
            await self.queue.put(ev)
        return ev


async def investigate(case: Case, deps: Deps, publish: Publisher) -> Case:
    store, s = deps.store, deps.settings
    t0 = time.perf_counter()
    await publish("case.started", {"case_id": case.id, "agents": [
        {"id": a.NAME, "title": a.TITLE} for a in AGENTS]})
    try:
        # ---- intake --------------------------------------------------------
        blobs = {d.label: await store.get_blob(d.storage_uri) for d in case.sources if d.storage_uri}
        await publish("intake.started", {"inputs": [
            {"label": d.label, "filename": d.filename, "type": d.mime_type} for d in case.sources]})
        req, notes = await intake.extract(deps.gemini, case.sources, blobs)
        case.request = req
        await publish("intake.completed", {"request": req.model_dump(mode="json"), "notes": notes})

        vendors = await store.list_vendors()
        sender_domain = addr_domain(req.requester_email)
        v, why = match_vendor(req, sender_domain, vendors)
        hist = await store.payments_for_vendor(v.id) if v else []
        ctx = AgentContext(settings=s, request=req, docs=case.sources, blobs=blobs, vendors=vendors,
                           vendor=v, vendor_match_reason=why, history=hist, sources=deps.sources,
                           gemini=deps.gemini)

        # ---- evidence agents in parallel ----------------------------------
        findings: list[Finding] = []

        async def run_agent(mod) -> None:
            started = time.perf_counter()
            count = 0
            await publish("agent.started", {"title": mod.TITLE}, agent=mod.NAME)

            async def emit(f: Finding) -> None:
                nonlocal count
                count += 1
                findings.append(f)
                await publish("finding", f.model_dump(mode="json"), agent=mod.NAME)

            try:
                await asyncio.wait_for(mod.run(ctx, emit), timeout=AGENT_TIMEOUT_S)
                await publish("agent.completed", {"findings": count,
                                                  "ms": int((time.perf_counter() - started) * 1000)},
                              agent=mod.NAME)
            except Exception as e:  # one agent failing must not sink the case
                log.exception("agent %s failed", mod.NAME)
                await publish("agent.failed", {"error": str(e)[:300]}, agent=mod.NAME)

        await asyncio.gather(*(run_agent(m) for m in AGENTS))
        case.findings = findings

        # ---- verdict -------------------------------------------------------
        await publish("agent.started", {"title": "Verdict"}, agent=verdict.NAME)
        case.verdict = await verdict.run(findings, req, v, deps.gemini)
        await publish("verdict", case.verdict.model_dump(mode="json"), agent=verdict.NAME)
        case.status = CaseStatus.completed
        case.lease_until = None
        await store.save_case(case)
        await publish("case.completed", {"ms": int((time.perf_counter() - t0) * 1000)})
    except Exception as e:
        log.exception("case %s failed", case.id)
        case.status, case.error = CaseStatus.failed, str(e)[:500]
        await store.save_case(case)
        await publish("case.failed", {"error": case.error})
    return case


TERMINAL = {"case.completed", "case.failed"}
