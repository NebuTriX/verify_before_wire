"""Background worker: runs investigations nobody is watching (ERP/API submissions), retries
cases whose worker died (expired lease) and delivers webhooks.

Two ways to run it:
  * inside the web process (WORKER_ENABLED=true, the default) — one container does everything;
  * as its own process for scale:  python -m app.worker
    Run web replicas with WORKER_ENABLED=false and as many worker replicas as you need. Cases
    are claimed with a lease in the database (store.try_claim), so any number of workers can
    run side by side without two of them investigating the same case.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone

from .deps import build_deps
from .models import CaseStatus
from .orchestrator import ERP_LABEL, Deps, start_case
from .webhooks import deliver_due

log = logging.getLogger("vbw.worker")


async def _ready(d: Deps, tid: str, cid: str) -> bool:
    """ERP/API payments run at once. A case submitted in the browser (upload or sample) is left to
    its own live view for worker_grace_seconds, so a recorded demo replay is never raced, and is
    only picked up here if nobody opened it."""
    c = await d.store.get_case(tid, cid)
    if c is None:
        return False
    if c.status != CaseStatus.queued:            # a running case with an expired lease: its worker died, retry now
        return True
    if any(x.label == ERP_LABEL for x in c.sources):
        return True
    return (datetime.now(timezone.utc) - c.created_at).total_seconds() >= d.settings.worker_grace_seconds


async def run_worker(d: Deps, stop: asyncio.Event) -> None:
    s = d.settings
    sem = asyncio.Semaphore(max(1, s.worker_concurrency))
    running: set[asyncio.Task] = set()
    log.info("worker started (concurrency %d)", s.worker_concurrency)

    async def one(tid: str, cid: str) -> None:
        async with sem:
            try:
                task = await start_case(d, tid, cid, None)
                if task:
                    await task
            except Exception:
                log.exception("worker could not run case %s", cid)

    while not stop.is_set():
        busy = False
        try:
            free = s.worker_concurrency - len(running)
            if free > 0:
                for tid, cid in await d.store.claimable_cases(limit=free):
                    if not await _ready(d, tid, cid):
                        continue
                    t = asyncio.create_task(one(tid, cid))
                    running.add(t)
                    t.add_done_callback(running.discard)
                    busy = True
            if await deliver_due(d.store, d.sources.http, allow_insecure=s.webhook_allow_insecure):
                busy = True
        except Exception:
            log.exception("worker loop error")
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.2 if busy else s.worker_poll_seconds)
        except asyncio.TimeoutError:
            pass
    if running:
        await asyncio.wait(running, timeout=s.lease_seconds)


def main() -> None:
    from .config import get_settings
    s = get_settings()
    logging.basicConfig(level=s.log_level, format='{"severity":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}')

    async def run() -> None:
        d = build_deps(s)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:         # Windows event loops: fall back to a plain signal handler
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        warm = asyncio.create_task(d.sources.sanctions.warm())
        try:
            await run_worker(d, stop)
        finally:
            warm.cancel()
            await d.sources.aclose()
    asyncio.run(run())


if __name__ == "__main__":
    main()
