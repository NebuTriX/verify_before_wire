from tests.conftest import (ADMIN2, APPROVER, APPROVER2, CLERK, OWNER, H, make_workspace, run_case,
                            verdict_of)


def codes(events):
    return {e["data"]["code"] for e in events if e["type"] == "finding"}


def test_onboarding_checklist_self_serve(client):
    r = client.post("/api/workspaces", headers=H(OWNER), json={"name": "Solo Co", "template": "lean"})
    tid = r.json()["tenant_id"]
    steps = {s["id"]: s["done"] for s in client.get(f"/api/t/{tid}/onboarding", headers=H(OWNER)).json()["steps"]}
    assert steps["workspace"] and steps["policy"] and not steps["vendors"]
    bad = "vendor_id,legal_name\nv1,X\n"
    r = client.post(f"/api/t/{tid}/import/vendors", headers=H(OWNER), files={"file": ("v.csv", bad)})
    assert r.json()["imported"] == 0 and r.json()["error_count"] == 1
    good = client.get("/api/csv-templates/vendors").text
    r = client.post(f"/api/t/{tid}/import/vendors", headers=H(OWNER), files={"file": ("v.csv", good)})
    assert r.json()["imported"] == 1


def test_fraud_holds_and_legit_releases(client):
    tid = make_workspace(client)
    _, ev = run_case(client, tid, "ceo-fraud")
    for c in ("BANK_ACCOUNT_CHANGED", "LOOKALIKE_DOMAIN", "EMAIL_AUTH_FAILED", "REPLY_TO_MISMATCH",
              "LANG_SECRECY", "SOURCE_CONFLICT"):
        assert c in codes(ev), c
    assert "LANG_URGENCY" not in codes(ev)  # the invented quote was dropped
    v = verdict_of(ev)
    assert v["decision"] == "HOLD" and "callback" in v["requirements"]
    _, ev = run_case(client, tid, "legit-invoice")
    assert verdict_of(ev)["decision"] == "RELEASE"


def test_same_evidence_different_company_policy(client):
    strict = make_workspace(client, "strict-bank", owner="a@bank.com", team=False)
    lean = make_workspace(client, "lean", owner="b@startup.com", team=False)
    _, s = run_case(client, strict, "legit-invoice", who="a@bank.com")
    _, l = run_case(client, lean, "legit-invoice", who="b@startup.com")
    assert "second_approver" in verdict_of(s)["requirements"]      # strict: >USD 50k needs two
    assert "second_approver" not in verdict_of(l)["requirements"]


def test_tenant_isolation(client):
    a = make_workspace(client, owner="a@a.com", team=False)
    b = make_workspace(client, owner="b@b.com", team=False)
    cid, _ = run_case(client, a, "legit-invoice", who="a@a.com")
    assert client.get(f"/api/t/{a}/cases/{cid}", headers=H("b@b.com")).status_code == 404
    assert client.get(f"/api/t/{b}/cases/{cid}", headers=H("b@b.com")).status_code == 404


def test_case_sources_served_safely(client):
    tid = make_workspace(client)
    cid, ev = run_case(client, tid, "ceo-fraud")
    src = lambda label, who=CLERK: client.get(f"/api/t/{tid}/cases/{cid}/sources/{label}", headers=H(who))
    r = src("voice_note")
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/")
    assert r.headers["content-security-policy"].startswith("sandbox")       # route's own CSP survives
    assert src("email").status_code == 415                                  # emails are shown as parsed text only
    assert src("nope").status_code == 404
    assert src("voice_note", who="outsider@x.com").status_code in (403, 404)
    lookalike = next(e["data"] for e in ev if e["type"] == "finding" and e["data"]["code"] == "LOOKALIKE_DOMAIN")
    assert lookalike["meta"] == {"domain": "acme-hold1ngs.com", "imitates": "acme-holdings.com"}


def test_maker_checker_release_flow(client):
    tid = make_workspace(client)
    cid, ev = run_case(client, tid, "ceo-fraud", who=APPROVER)
    act = lambda who, action, reason="": client.post(f"/api/t/{tid}/cases/{cid}/actions", headers=H(who),
                                                     json={"action": action, "reason": reason})
    assert act(CLERK, "release").status_code == 403                  # clerk can't decide
    assert act(APPROVER, "release").status_code == 403               # submitter can't decide own case
    r = act(APPROVER2, "release", "x" * 25)
    assert r.status_code == 409 and "callback" in r.json()["detail"]  # callback required first
    cb = {"contact": "Tan Mei Ling", "number": "+65 6123 4567", "number_source": "other", "outcome": "confirmed"}
    client.post(f"/api/t/{tid}/cases/{cid}/callbacks", headers=H(APPROVER2), json=cb)
    assert act(APPROVER2, "release", "x" * 25).status_code == 409    # number not from the master file
    client.post(f"/api/t/{tid}/cases/{cid}/callbacks", headers=H(APPROVER2), json={**cb, "number_source": "vendor_master"})
    assert act(APPROVER2, "release", "too short").status_code == 422  # override needs a real reason
    r = act(APPROVER2, "release", "Vendor confirmed new HSBC account by phone")
    assert r.json()["state"] == "awaiting_second_approval"           # override forces a second approver
    assert act(APPROVER2, "release", "again from me, same person").status_code == 403
    assert act(OWNER, "release", "Second approval after reviewing callback").json()["state"] == "released"
    pdf = client.get(f"/api/t/{tid}/cases/{cid}/report.pdf", headers=H(OWNER))
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


def test_policy_four_eyes_and_simulation(client):
    tid = make_workspace(client)
    run_case(client, tid, "legit-invoice")
    cur = client.get(f"/api/t/{tid}/policies", headers=H(OWNER)).json()[0]
    draft = {"name": "Tighter", "settings": {**cur["settings"], "callback_at": 1, "hold_at": 2},
             "rules": cur["rules"], "change_note": "Hold almost everything"}
    v = client.post(f"/api/t/{tid}/policies", headers=H(OWNER), json=draft).json()["version"]
    sim = client.post(f"/api/t/{tid}/policies/{v}/simulate", headers=H(OWNER)).json()
    assert sim["cases"] == 1 and sim["changed"] == 1
    client.post(f"/api/t/{tid}/policies/{v}/submit", headers=H(OWNER))
    assert client.post(f"/api/t/{tid}/policies/{v}/approve", headers=H(OWNER)).status_code == 403
    assert client.post(f"/api/t/{tid}/policies/{v}/approve", headers=H(ADMIN2)).json()["status"] == "active"
    assert client.get(f"/api/t/{tid}", headers=H(OWNER)).json()["active_policy_version"] == v
    bad = {**draft, "settings": {**draft["settings"], "callback_at": 90, "hold_at": 50}}
    assert client.post(f"/api/t/{tid}/policies", headers=H(OWNER), json=bad).status_code == 422


def test_audit_chain_verifies(client):
    tid = make_workspace(client)
    run_case(client, tid, "legit-invoice")
    r = client.get(f"/api/t/{tid}/audit/verify", headers=H(OWNER)).json()
    assert r["ok"] and r["entries"] >= 8
    assert client.get(f"/api/t/{tid}/audit", headers=H(CLERK)).status_code == 403


def test_api_key_integration_and_idempotency(client):
    tid = make_workspace(client, team=False)
    key = client.post(f"/api/t/{tid}/api-keys", headers=H(OWNER), json={"name": "SAP"}).json()["api_key"]
    auth = {"Authorization": f"Bearer {key}", "Idempotency-Key": "pay-123"}
    eml = open("../demo/files/legit_invoice.eml", "rb").read()
    r1 = client.post(f"/api/t/{tid}/checks", headers=auth, files=[("files", ("i.eml", eml))]).json()
    r2 = client.post(f"/api/t/{tid}/checks", headers=auth, files=[("files", ("i.eml", eml))]).json()
    assert r1["id"] == r2["id"] and r1["verdict"]["decision"] == "RELEASE"
    other = make_workspace(client, owner="x@x.com", team=False)
    assert client.get(f"/api/t/{other}/cases", headers=auth).status_code == 404
    client.delete(f"/api/t/{tid}/api-keys/{key.split('_')[1]}", headers=H(OWNER))
    assert client.get(f"/api/t/{tid}/cases", headers=auth).status_code == 401


def test_fail_closed_when_sanctions_down(client, sources):
    from app.sources import SourceUnavailable

    async def down(*a, **k):
        raise SourceUnavailable("list unreachable")
    sources.sanctions.screen_with_coverage = down
    tid = make_workspace(client, team=False)
    _, ev = run_case(client, tid, "legit-invoice", who=OWNER)
    assert verdict_of(ev)["decision"] == "HOLD"   # balanced: sanctions is a required check


def test_rejects_bad_uploads(client):
    tid = make_workspace(client, team=False)
    r = client.post(f"/api/t/{tid}/cases", headers=H(OWNER), files=[("files", ("x.exe", b"MZ", "application/x-msdownload"))])
    assert r.status_code == 415
