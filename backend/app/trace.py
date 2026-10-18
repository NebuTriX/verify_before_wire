"""Data-source tracing. The source clients are shared by every case, so the case being
investigated is carried in a contextvar: the orchestrator sets a sink, and each traced call
publishes `source.call` / `source.done` events into that case's live stream."""
from __future__ import annotations

import contextlib
import contextvars
import functools
import itertools
import logging
import time

from .metrics import SOURCE
from typing import Awaitable, Callable

Sink = Callable[[str, dict, "str | None"], Awaitable[object]]

_sink: contextvars.ContextVar[Sink | None] = contextvars.ContextVar("vbw_trace_sink", default=None)
_agent: contextvars.ContextVar[str | None] = contextvars.ContextVar("vbw_trace_agent", default=None)
_call: contextvars.ContextVar[dict | None] = contextvars.ContextVar("vbw_trace_call", default=None)
_ids = itertools.count(1)
log = logging.getLogger(__name__)

# Display metadata for every source the UI can draw (served to the browser by /api/config).
# `kind`: live (internet register), ai (Gemini), workspace (your own data), local (analysed on this server).
CATALOG = {
    "vendor_master": {"name": "Vendor master", "kind": "workspace"},
    "payment_history": {"name": "Payment history", "kind": "workspace"},
    "rdap": {"name": "RDAP domain registry", "kind": "live"},
    "dns": {"name": "Public DNS", "kind": "live"},
    "sanctions": {"name": "Sanctions lists", "kind": "live"},
    "fx": {"name": "ECB FX rates", "kind": "live"},
    "ifsc": {"name": "IFSC bank directory", "kind": "live"},
    "gleif": {"name": "GLEIF entity register", "kind": "live"},
    "vies": {"name": "EU VIES VAT register", "kind": "live"},
    "forensics": {"name": "Media forensics", "kind": "local"},
    "gemini": {"name": "Gemini", "kind": "ai"},
}


LAST: dict[str, dict] = {}     # source -> last outcome in this process (for the status panel)


def _observe(source: str, status: str, seconds: float, detail: str = "") -> None:
    SOURCE.labels(source, status).observe(seconds)
    LAST[source] = {"status": status, "at": time.time(), "ms": int(seconds * 1000), "detail": detail[:160]}


def set_sink(sink: Sink | None) -> contextvars.Token:
    return _sink.set(sink)


def reset_sink(token: contextvars.Token) -> None:
    _sink.reset(token)


@contextlib.contextmanager
def agent_scope(agent: str):
    """Attribute the data-source lookups made inside this block to `agent` (the live map draws the edge)."""
    token = _agent.set(agent)
    try:
        yield
    finally:
        _agent.reset(token)


def cache_hit() -> None:
    """Called by a source cache on a hit, so the trace can say the answer was not fetched live."""
    if (c := _call.get()) is not None:
        c["cached"] = True


async def _publish(type_: str, data: dict) -> None:
    sink = _sink.get()
    if sink is None:
        return
    try:
        await sink(type_, data, _agent.get())
    except Exception:  # tracing must never break an investigation
        log.debug("trace event %s dropped", type_, exc_info=True)


async def record(source: str, target: str, status: str = "ok", detail: str = "", ms: int = 0) -> None:
    """A lookup that is not a network call (e.g. the workspace's own vendor master)."""
    cid = next(_ids)
    _observe(source, status, ms / 1000, detail if status == "error" else "")
    await _publish("source.call", {"call": cid, "source": source, "target": target})
    await _publish("source.done", {"call": cid, "source": source, "target": target,
                                   "status": status, "detail": detail, "ms": ms})


def traced(source: str, target: Callable[..., str | None], summary: Callable[[object], str] | None = None):
    """Decorate an async source method. `target(*args)` names what is looked up (None skips
    tracing); `summary(result)` turns the answer into a short line for the UI."""
    def wrap(fn):
        @functools.wraps(fn)
        async def inner(self, *args, **kwargs):
            tgt = target(*args, **kwargs) if _sink.get() is not None else None
            if tgt is None:
                return await fn(self, *args, **kwargs)
            cid, state = next(_ids), {"cached": False}
            tok = _call.set(state)
            await _publish("source.call", {"call": cid, "source": source, "target": tgt})
            t0 = time.perf_counter()
            try:
                res = await fn(self, *args, **kwargs)
            except Exception as e:
                st = "paused" if getattr(e, "paused", False) else "error"
                _observe(source, st, time.perf_counter() - t0, str(e))
                await _publish("source.done", {"call": cid, "source": source, "target": tgt, "status": st,
                                               "detail": str(e)[:200], "ms": int((time.perf_counter() - t0) * 1000)})
                raise
            finally:
                _call.reset(tok)
            detail = ""
            if summary:
                try:
                    detail = summary(res)
                except Exception:
                    detail = ""
            status = "cached" if state["cached"] else "ok"
            _observe(source, status, time.perf_counter() - t0)
            await _publish("source.done", {"call": cid, "source": source, "target": tgt,
                                           "status": status, "detail": detail,
                                           "ms": int((time.perf_counter() - t0) * 1000)})
            return res
        return inner
    return wrap
