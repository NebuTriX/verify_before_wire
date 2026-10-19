"""Regression tests for issues found in code review (each test names the failure it prevents)."""
import asyncio
from datetime import timedelta

import httpx

from conftest import OWNER, H, make_workspace, run_case
from test_integrations import payment, setup


def test_ai_read_documents_cannot_choose_the_vendor_record(client, monkeypatch):
    """An email saying 'Vendor ID: v-lcl' must not make the case match that trusted vendor by id."""
    from app import intake
    real = intake.extract

    async def poisoned(*a, **k):
        req, notes, read_by = await real(*a, **k)
        req.vendor_id = "v-lcl"
        return req, notes, read_by
    monkeypatch.setattr(intake, "extract", poisoned)
    tid = make_workspace(client, team=False)
    cid, _ = run_case(client, tid, "ceo-fraud", who=OWNER)
    case = client.get(f"/api/t/{tid}/cases/{cid}", headers=H(OWNER)).json()
    assert case["request"]["vendor_id"] is None
    assert not any("from your ERP" in (f.get("detail") or "") for f in case["findings"])


def _recording_http(log):
    def handler(req):
        log.append(req)
        return httpx.Response(200)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_concurrent_workers_send_each_webhook_once(client, sources):
    from app import webhooks
    d = client.app.state.deps
    d.settings.webhook_allow_insecure = True
    sent = []
    sources.http = _recording_http(sent)
    tid, key = setup(client)
    client.post(f"/api/t/{tid}/webhooks", headers=H(OWNER), json={"url": "http://erp.local/hook"})
    client.post(f"/api/t/{tid}/erp/payments?wait=30", headers=key, json=payment("ONCE:1"))

    async def race():
        await asyncio.gather(*(webhooks.deliver_due(d.store, sources.http, allow_insecure=True) for _ in range(5)))
    client.portal.call(race)
    assert len(sent) == 1


def test_webhook_test_reaches_only_that_hook(client, sources):
    d = client.app.state.deps
    d.settings.webhook_allow_insecure = True
    sent = []
    sources.http = _recording_http(sent)
    tid = make_workspace(client, team=False)
    a = client.post(f"/api/t/{tid}/webhooks", headers=H(OWNER), json={"url": "http://a.local/hook"}).json()
    client.post(f"/api/t/{tid}/webhooks", headers=H(OWNER), json={"url": "http://b.local/hook"})
    r = client.post(f"/api/t/{tid}/webhooks/{a['id']}/test", headers=H(OWNER)).json()
    assert r["status"] == "delivered" and r["event"] == "test.ping"
    assert [str(x.url) for x in sent] == ["http://a.local/hook"]


def test_delivery_rechecks_the_address(client, sources):
    """A URL allowed at registration is re-checked at send time (DNS rebinding / policy change)."""
    from app import webhooks
    d = client.app.state.deps
    d.settings.webhook_allow_insecure = True
    sent = []
    sources.http = _recording_http(sent)
    tid, key = setup(client)
    client.post(f"/api/t/{tid}/webhooks", headers=H(OWNER), json={"url": "http://127.0.0.1:9/hook"})
    client.post(f"/api/t/{tid}/erp/payments?wait=30", headers=key, json=payment("SSRF:1"))
    client.portal.call(lambda: webhooks.deliver_due(d.store, sources.http, allow_insecure=False))
    assert sent == []
    dl = client.get(f"/api/t/{tid}/webhooks/deliveries", headers=H(OWNER)).json()[0]
    assert dl["status"] == "failed" and dl["last_error"].startswith("blocked")


def test_lease_heartbeat(client):
    tid, key = setup(client)
    cid = client.post(f"/api/t/{tid}/erp/payments", headers=key, json=payment("LEASE:1")).json()["case_id"]
    st = client.app.state.deps.store

    async def go():
        assert await st.try_claim(tid, cid, 1)
        before = (await st.get_case(tid, cid)).lease_until
        assert await st.renew_lease(tid, cid, 300)
        after = (await st.get_case(tid, cid)).lease_until
        assert after - before > timedelta(seconds=200)
        await st.update_case(tid, cid, lambda c: setattr(c, "status", type(c.status)("completed")))
        assert not await st.renew_lease(tid, cid, 300)          # finished cases are never revived
    client.portal.call(go)


def test_payment_run_reports_auto_releases(client):
    tid, key = setup(client)
    d = client.app.state.deps

    async def on():
        pol = await d.store.active_policy(tid)
        pol.settings.auto_release_below = 20
        await d.store.put_policy(pol)
    client.portal.call(on)
    r = client.post(f"/api/t/{tid}/erp/payment-runs?wait=30", headers=key,
                    json={"items": [payment("AR:1"), payment("AR:2", account="444455556666")]}).json()
    assert r["released_automatically"] == sum(1 for x in r["items"] if x["release_allowed"]) == 1


def test_broken_optional_list_does_not_break_screening(tmp_path):
    from app.sanctions_lists import ListSpec, SanctionsLists, parse_ofac, parse_un

    def handler(req):
        if "ofac" in str(req.url):
            return httpx.Response(200, content=b'1,"EVIL CORP",-0-,SDGT\n')
        return httpx.Response(200, content=b"<CONSOLIDATED_LIST><INDIVIDUAL>")   # truncated XML

    async def go():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        lists = SanctionsLists([ListSpec("ofac", "OFAC", "https://x/ofac", parse_ofac, 5),
                                ListSpec("un", "UN", "https://x/un", parse_un, 5)], http, tmp_path, 12)
        ofac, un = await lists.ensure("ofac", 5), await lists.ensure("un", 5)
        return ofac, un, lists.status()
    ofac, un, status = asyncio.run(go())
    assert ofac is not None and un is None
    assert next(s for s in status if s["key"] == "un")["error"].startswith("could not read")


def test_wrong_account_does_not_burn_an_invite(client):
    tid = make_workspace(client, team=False)
    link = client.post(f"/api/t/{tid}/invites", headers=H(OWNER), json={"email": "ana@acme.com", "role": "approver"}).json()["invite_link"]
    token = link.split("#invite=")[1]
    pv = client.get(f"/api/invites/preview?token={token}").json()
    assert pv["valid"] and pv["role"] == "approver" and pv["email"].startswith("an") and "ana@" not in pv["email"]
    wrong = client.post("/api/invites/accept", headers=H("mallory@acme.com"), json={"token": token})
    assert wrong.status_code == 403
    assert client.post("/api/invites/accept", headers=H("ana@acme.com"), json={"token": token}).status_code == 200
    assert client.get(f"/api/invites/preview?token={token}").json() == {"valid": False}
