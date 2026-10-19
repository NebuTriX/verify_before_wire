"""Data-source tracing: what the live investigation map and source timeline are drawn from."""
import pytest

from app import trace
from app.sources import TTLCache

from conftest import make_workspace, run_case


class Client:
    def __init__(self):
        self.cache = TTLCache(60)

    @trace.traced("rdap", lambda domain: domain, lambda r: f"answer {r}")
    async def lookup(self, domain):
        if (hit := self.cache.get(domain)) is not None:
            return hit
        if domain == "down.example":
            raise RuntimeError("registry unreachable")
        return self.cache.put(domain, 42)


async def test_traced_call_reports_ok_cached_and_error():
    seen = []

    async def sink(type_, data, agent):
        seen.append((type_, agent, data))

    c = Client()
    token = trace.set_sink(sink)
    try:
        with trace.agent_scope("domain"):
            assert await c.lookup("a.example") == 42
            assert await c.lookup("a.example") == 42          # second time: from the cache
            with pytest.raises(RuntimeError):
                await c.lookup("down.example")
    finally:
        trace.reset_sink(token)
    done = [d for t, _, d in seen if t == "source.done"]
    assert [d["status"] for d in done] == ["ok", "cached", "error"]
    assert done[0]["detail"] == "answer 42" and "unreachable" in done[2]["detail"]
    assert all(a == "domain" for _, a, _ in seen)
    calls = [d["call"] for t, _, d in seen if t == "source.call"]
    assert calls == [d["call"] for d in done] and len(set(calls)) == 3


async def test_untraced_without_sink():
    assert await Client().lookup("b.example") == 42     # no case running: nothing published, no error


def test_stream_carries_source_catalogue_and_lookups(client):
    tid = make_workspace(client)
    _, events = run_case(client, tid, "vendor-bank-change")
    started = next(e for e in events if e["type"] == "case.started")["data"]
    assert {"vendor_master", "rdap", "sanctions", "gemini"} <= set(started["sources"])
    done = {e["data"]["source"]: e for e in events if e["type"] == "source.done"}
    assert done["vendor_master"]["agent"] == "vendor" and done["vendor_master"]["data"]["detail"].startswith("matched")
    assert done["payment_history"]["agent"] == "history"
