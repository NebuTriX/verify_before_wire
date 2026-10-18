"""ERP (SAP) integration: structured payment checks, master-data sync, signed webhooks,
the background worker and the registry agent. Runs on the memory and SQL stores."""
import asyncio
import json

import httpx

from app.registries import LegalEntity, VatCheck
from conftest import OWNER, H, make_workspace

VENDOR = {"id": "0000100234", "name": "Lion City Logistics Pte Ltd", "country": "SG",
          "email_domains": ["lioncitylogistics.com.sg"], "callback_contact": "Mei Ling Tan", "callback_phone": "+65 6123 4567",
          "currency": "SGD", "bank_accounts": [{"country": "SG", "bank_name": "DBS Bank", "account": "0721234567",
                                                "swift": "DBSSSGSG", "valid_from": "2024-01-10"}]}


def payment(ref, account="0721234567", swift="DBSSSGSG", holder=None, amount=78400.0):
    return {"external_ref": ref, "company_code": "1000", "run_date": "2026-10-18", "run_id": "ACME1",
            "vendor": {"id": "0000100234", "name": "Lion City Logistics Pte Ltd", "country": "SG"},
            "amount": amount, "currency": "SGD", "documents": [{"number": "5100000123", "fiscal_year": "2026",
                                                                 "reference": "LCL-INV-2026-0988"}],
            "bank": {"country": "SG", "account": account, "swift": swift, "bank_name": "DBS Bank", "account_holder": holder}}


def integration_key(client, tid):
    r = client.post(f"/api/t/{tid}/api-keys", headers=H(OWNER), json={"name": "SAP S/4HANA prod", "role": "integration"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def setup(client):
    tid = make_workspace(client, team=False)
    key = integration_key(client, tid)
    assert client.put(f"/api/t/{tid}/erp/vendors", headers=key, json={"vendors": [VENDOR]}).json() == {"upserted": 1}
    return tid, key


def test_erp_payment_known_account_is_not_held_and_is_idempotent(client):
    tid, key = setup(client)
    r = client.post(f"/api/t/{tid}/erp/payments?wait=30", headers=key, json=payment("F110:20261018:ACME1:1000:0000100234"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] in ("RELEASE", "CALLBACK") and body["release_allowed"] is False   # people release, not the API
    again = client.post(f"/api/t/{tid}/erp/payments", headers=key, json=payment("F110:20261018:ACME1:1000:0000100234"))
    assert again.json()["case_id"] == body["case_id"]
    st = client.get(f"/api/t/{tid}/erp/payments/F110:20261018:ACME1:1000:0000100234", headers=key).json()
    assert st["case_id"] == body["case_id"] and st["decision"] == body["decision"]
    case = client.get(f"/api/t/{tid}/cases/{body['case_id']}", headers=H(OWNER)).json()
    assert case["sources"][0]["label"] == "erp_payment"
    assert not any(f["code"] == "NO_SENDER" for f in case["findings"])     # ERP payments have no email by design


def test_erp_payment_to_new_account_is_stopped(client):
    tid, key = setup(client)
    r = client.post(f"/api/t/{tid}/erp/payments?wait=30", headers=key, json=payment("F110:X:1", account="998877665544"))
    assert r.json()["decision"] in ("CALLBACK", "HOLD")
    assert "callback" in r.json()["requirements"]


def test_payment_run_batch(client):
    tid, key = setup(client)
    run = {"run_date": "2026-10-18", "run_id": "ACME2", "company_code": "1000",
           "items": [payment("RUN2:1"), payment("RUN2:2", account="111122223333")]}
    r = client.post(f"/api/t/{tid}/erp/payment-runs?wait=30", headers=key, json=run).json()
    assert r["count"] == 2 and r["pending"] == 0 and r["released_automatically"] == 0
    assert sum(r["summary"].values()) == 2


def test_integration_key_cannot_decide_or_manage(client):
    tid, key = setup(client)
    cid = client.post(f"/api/t/{tid}/erp/payments?wait=30", headers=key, json=payment("P:1")).json()["case_id"]
    assert client.post(f"/api/t/{tid}/cases/{cid}/actions", headers=key, json={"action": "release"}).status_code == 403
    assert client.post(f"/api/t/{tid}/webhooks", headers=key, json={"url": "https://example.com/h"}).status_code == 403


def test_webhook_signed_delivery_and_retry(client, sources):
    from app import webhooks
    d = client.app.state.deps
    d.settings.webhook_allow_insecure = True
    received, fail_first = [], {"n": 1}

    def handler(req: httpx.Request) -> httpx.Response:
        if fail_first["n"]:
            fail_first["n"] -= 1
            return httpx.Response(500)
        received.append(req)
        return httpx.Response(200)
    sources.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tid, key = setup(client)
    hook = client.post(f"/api/t/{tid}/webhooks", headers=H(OWNER), json={"url": "http://sap-cpi.local/vbw"}).json()
    assert hook["secret"].startswith("whsec")
    assert "secret" not in client.get(f"/api/t/{tid}/webhooks", headers=H(OWNER)).json()[0]
    client.post(f"/api/t/{tid}/erp/payments?wait=30", headers=key, json=payment("W:1"))

    async def drain():
        await webhooks.deliver_due(d.store, sources.http, allow_insecure=True)   # first attempt: HTTP 500 -> retry later
        for x in await d.store.list_deliveries(tid):
            await d.store.update_delivery(tid, x["id"], {"next_attempt_at": "2000-01-01T00:00:00+00:00"})
        await webhooks.deliver_due(d.store, sources.http, allow_insecure=True)
    client.portal.call(drain)
    assert len(received) == 1
    req = received[0]
    assert webhooks.verify(hook["secret"], req.content, req.headers["VBW-Signature"])
    assert not webhooks.verify("wrong", req.content, req.headers["VBW-Signature"])
    body = json.loads(req.content)
    assert body["type"] == "case.recommended" and body["data"]["external_ref"] == "W:1"
    dl = client.get(f"/api/t/{tid}/webhooks/deliveries", headers=H(OWNER)).json()[0]
    assert dl["status"] == "delivered" and dl["attempts"] == 2


def test_webhook_rejects_private_urls(client):
    tid = make_workspace(client, team=False)
    r = client.post(f"/api/t/{tid}/webhooks", headers=H(OWNER), json={"url": "http://127.0.0.1:9000/x"})
    assert r.status_code == 422


def test_worker_runs_unwatched_cases(client):
    from app.worker import run_worker
    tid, key = setup(client)
    cid = client.post(f"/api/t/{tid}/erp/payments", headers=key, json=payment("Q:1")).json()["case_id"]
    d = client.app.state.deps

    async def go():
        stop = asyncio.Event()
        t = asyncio.create_task(run_worker(d, stop))
        for _ in range(100):
            c = await d.store.get_case(tid, cid)
            if c.status.value == "completed":
                break
            await asyncio.sleep(0.05)
        stop.set()
        await t
        return c
    assert client.portal.call(go).verdict is not None


class FakeGleif:
    name = "GLEIF (fake)"

    async def by_bic(self, bic):
        return LegalEntity("5493000IBP32UQZ0KL24", "DBS BANK LTD.", "SG", "ACTIVE", "ISSUED", "2012-11-29", [bic])

    async def by_name(self, name, country=None):
        return LegalEntity("254900LCL0000000001", "LION CITY LOGISTICS PTE LTD", "SG", "INACTIVE", "LAPSED", "2015-01-01", [])


class FakeVies:
    name = "VIES (fake)"

    @staticmethod
    def split(vat):
        return ("DE", vat[2:]) if vat.startswith("DE") else None

    async def check(self, vat):
        return VatCheck("DE", vat[2:], False, None)


def test_registry_agent_flags_contradictions(client, sources):
    sources.gleif, sources.vies = FakeGleif(), FakeVies()
    tid, key = setup(client)
    p = payment("R:1", swift="DBSSSGSG")
    p["bank"]["iban"] = "DE89370400440532013000"          # German IBAN behind a Singapore SWIFT code
    p["bank"]["bank_name"] = "Commerzbank"
    p["vendor"]["vat_number"] = "DE123456789"
    cid = client.post(f"/api/t/{tid}/erp/payments?wait=30", headers=key, json=p).json()["case_id"]
    codes = {f["code"] for f in client.get(f"/api/t/{tid}/cases/{cid}", headers=H(OWNER)).json()["findings"]}
    assert {"BIC_OWNER_FOUND", "IBAN_BIC_COUNTRY_MISMATCH", "BANK_NAME_MISMATCH", "PAYEE_ENTITY_INACTIVE", "VAT_INVALID"} <= codes


def test_sources_status(client):
    tid = make_workspace(client, team=False)
    s = client.get(f"/api/t/{tid}/sources/status", headers=H(OWNER)).json()
    assert s["workspace"]["vendors"] >= 1 and s["ai"]["role"].startswith("Reads")


def test_auto_release_is_opt_in_and_audited(client):
    tid, key = setup(client)
    d = client.app.state.deps
    first = client.post(f"/api/t/{tid}/erp/payments?wait=30", headers=key, json=payment("A:0")).json()
    assert first["workflow_state"] == "awaiting_decision"              # off by default: a person releases

    async def turn_on():
        pol = await d.store.active_policy(tid)
        before = pol.content_hash()
        pol.settings.auto_release_below = 20
        assert pol.content_hash() != before
        pol.settings.auto_release_below = None
        assert pol.content_hash() == before                            # optional setting: old hashes unchanged
        pol.settings.auto_release_below = 20
        await d.store.put_policy(pol)
    client.portal.call(turn_on)
    clean = client.post(f"/api/t/{tid}/erp/payments?wait=30", headers=key, json=payment("A:1")).json()
    assert clean["decision"] == "RELEASE" and clean["release_allowed"] is True
    risky = client.post(f"/api/t/{tid}/erp/payments?wait=30", headers=key, json=payment("A:2", account="555566667777")).json()
    assert risky["release_allowed"] is False
    actions = [e["action"] for e in client.get(f"/api/t/{tid}/audit", headers=H(OWNER)).json()]
    assert actions.count("case.auto_released") == 1


def test_payment_run_is_rate_limited_once_not_per_line(client):
    tid, key = setup(client)
    client.app.state.deps.settings.rate_limit_per_minute = 5
    run = {"run_id": "BIG", "items": [payment(f"BIG:{i}") for i in range(12)]}
    r = client.post(f"/api/t/{tid}/erp/payment-runs", headers=key, json=run)
    assert r.status_code == 200 and r.json()["count"] == 12
