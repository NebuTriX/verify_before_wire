import json


def run(client, scenario):
    case_id = client.post(f"/api/demo/{scenario}").json()["case_id"]
    events = []
    with client.stream("GET", f"/api/cases/{case_id}/stream") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    return case_id, events


def test_ceo_fraud_holds(app_client):
    case_id, events = run(app_client, "ceo-fraud")
    types = [e["type"] for e in events]
    assert types[0] == "case.started" and types[-1] == "case.completed"
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    codes = {e["data"]["code"] for e in events if e["type"] == "finding"}
    for c in ("BANK_ACCOUNT_CHANGED", "LOOKALIKE_DOMAIN", "EMAIL_AUTH_FAILED", "REPLY_TO_MISMATCH",
              "DOMAIN_NOT_REGISTERED", "AMOUNT_ANOMALY", "LANG_SECRECY", "LANG_CALLBACK_DISCOURAGED",
              "SOURCE_CONFLICT", "BENEFICIARY_NAME_MISMATCH"):
        assert c in codes, c
    assert "LANG_URGENCY" not in codes  # invented quote dropped
    verdict = next(e for e in events if e["type"] == "verdict")["data"]
    assert verdict["decision"] == "HOLD"
    assert verdict["callback_number"] == "+65 6123 4567"
    pdf = app_client.get(f"/api/cases/{case_id}/report.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


def test_legit_releases(app_client):
    _, events = run(app_client, "legit-invoice")
    verdict = next(e for e in events if e["type"] == "verdict")["data"]
    flags = [e["data"]["code"] for e in events if e["type"] == "finding" and e["data"]["severity"] != "info"]
    assert verdict["decision"] == "RELEASE", flags


def test_vendor_bank_change_holds(app_client):
    _, events = run(app_client, "vendor-bank-change")
    codes = {e["data"]["code"] for e in events if e["type"] == "finding"}
    assert "BANK_ACCOUNT_CHANGED" in codes and "EMAIL_AUTH_PASSED" in codes
    assert next(e for e in events if e["type"] == "verdict")["data"]["decision"] == "HOLD"


def test_second_stream_tails(app_client):
    case_id, first = run(app_client, "legit-invoice")
    with app_client.stream("GET", f"/api/cases/{case_id}/stream") as r:
        again = [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:")]
    assert [e["seq"] for e in again] == [e["seq"] for e in first]


def test_upload_rejects_bad_type(app_client):
    r = app_client.post("/api/cases", files=[("files", ("x.exe", b"MZ", "application/x-msdownload"))])
    assert r.status_code == 415
