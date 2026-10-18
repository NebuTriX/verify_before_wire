"""Plays SAP S/4HANA + SAP BTP Integration Suite against a running Verify-Before-Wire, end to end:

  1. syncs the vendor master (LFA1/LFBK) and cleared payments (BSAK) with an integration API key
  2. registers a webhook receiver (what the CPI iFlow would expose)
  3. submits an F110 payment proposal (REGUH/REGUP) as one payment run
  4. receives the signed webhooks, verifies them, and prints what SAP would do with each payment:
     keep the payment block, or lift it only after a person released the payment in VBW.

Local development (dev sign-in, WEBHOOK_ALLOW_INSECURE=true in backend/.env):
    python scripts/sap_simulator.py
Against a deployment: --url https://vbw.example.com --tenant t_... --api-key vbw_... --owner-token <Firebase ID token>
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.webhooks import verify  # noqa: E402  (the same check a CPI Groovy script would do)

SEED = Path(__file__).resolve().parent / "seed_data.json"
RECEIVED: list[dict] = []


def sap_vendor(v: dict) -> dict:
    """seed vendor -> the JSON a CPI mapping would build from LFA1 + LFBK + ADR6."""
    return {"id": v["id"].upper().replace("V-", "00001"), "name": v["legal_name"], "aliases": v.get("aliases", []),
            "country": v["country"], "email_domains": v["domains"], "callback_contact": v["callback_contact"],
            "callback_phone": v["callback_phone"], "currency": v["default_currency"],
            "payment_terms_days": v.get("payment_terms_days", 30),
            "bank_accounts": [{"country": a["country"], "bank_name": a["bank_name"], "account": a.get("account_number"),
                               "iban": a.get("iban"), "swift": a.get("swift_bic"), "valid_from": a["verified_on"]}
                              for a in v["bank_accounts"]]}


def proposal_item(v: dict, run: str, amount: float, account: str | None = None, holder: str | None = None,
                  bank: str | None = None, swift: str | None = None) -> dict:
    """One REGUH line of the F110 proposal for vendor v."""
    a = v["bank_accounts"][0]
    return {"external_ref": f"F110:20261018:{run}:1000:{v['id']}", "company_code": "1000",
            "vendor": {"id": v["id"], "name": v["name"], "country": v["country"]},
            "amount": amount, "currency": v["currency"],
            "bank": {"country": a["country"], "account": account or a["account"], "iban": a.get("iban"),
                     "swift": swift or a.get("swift"), "bank_name": bank or a["bank_name"], "account_holder": holder},
            "documents": [{"number": "5100004711", "fiscal_year": "2026", "reference": f"INV-{run}-{v['id'][-3:]}"}],
            "requested_by": "AP_CLERK01"}


class Receiver(BaseHTTPRequestHandler):
    secret = ""

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        ok = verify(self.secret, body, self.headers.get("VBW-Signature", ""))
        self.send_response(200 if ok else 401)
        self.end_headers()
        if ok:
            RECEIVED.append(json.loads(body))

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--dev-user", default="sap-admin@acme-holdings.com", help="dev sign-in only")
    ap.add_argument("--tenant")
    ap.add_argument("--api-key")
    ap.add_argument("--port", type=int, default=8099, help="local webhook receiver port")
    args = ap.parse_args()
    c = httpx.Client(base_url=args.url, timeout=90)
    owner = {"X-Dev-User": args.dev_user}

    # -- workspace + integration key (in production an admin does this once in the UI)
    tid = args.tenant
    if not tid:
        tpl = c.get("/api/templates").json()[0]["id"]
        tid = c.post("/api/workspaces", headers=owner, json={"name": "Acme Holdings (SAP demo)", "company_domains": ["acme-holdings.com"],
                                                             "base_currency": "USD", "template": tpl}).json()["tenant_id"]
    key = args.api_key or c.post(f"/api/t/{tid}/api-keys", headers=owner, json={"name": "SAP S/4HANA (simulator)", "role": "integration"}).json()["api_key"]
    sap = {"Authorization": f"Bearer {key}"}
    print(f"workspace {tid}  ·  integration key {key[:12]}…")

    # -- 1. master data
    seed = json.loads(SEED.read_text())
    vendors = [sap_vendor(v) for v in seed["vendors"]]
    ids = {v["id"]: sap_vendor(v)["id"] for v in seed["vendors"]}
    hist = [{**p, "vendor_id": ids[p["vendor_id"]]} for p in seed["payments"]]
    print("vendor sync      ", c.put(f"/api/t/{tid}/erp/vendors", headers=sap, json={"vendors": vendors}).json())
    print("history sync     ", c.put(f"/api/t/{tid}/erp/payment-history", headers=sap, json={"payments": hist}).json())

    # -- 2. webhook receiver (CPI iFlow endpoint)
    hook = c.post(f"/api/t/{tid}/webhooks", headers=owner,
                  json={"url": f"http://127.0.0.1:{args.port}/vbw", "description": "SAP BTP CPI (simulated)"}).json()
    if "secret" not in hook:
        sys.exit(f"could not register webhook: {hook}")
    Receiver.secret = hook["secret"]
    srv = HTTPServer(("127.0.0.1", args.port), Receiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # -- 3. the F110 proposal
    lcl, prc, hos = vendors[0], vendors[1], vendors[2]
    run = f"R{int(time.time()) % 100000:05d}"
    items = [proposal_item(lcl, run, 78400.0),                                          # routine
             proposal_item(prc, run, 61250.0, account="884412009931", bank="Kasikornbank", swift="KASITHBK"),  # bank changed
             proposal_item(hos, run, 4980.0, holder="Bank Melli Iran", bank="Bank Melli Iran")]               # sanctioned holder
    t0 = time.time()
    r = c.post(f"/api/t/{tid}/erp/payment-runs?wait=60", headers=sap,
               json={"run_date": "2026-10-18", "run_id": run, "company_code": "1000", "items": items}).json()
    print(f"\nF110 run {run}: {r['count']} payments checked in {time.time() - t0:.1f}s  ->  {r['summary']}")
    for it in r["items"]:
        print(f"  {it['external_ref']:<38} {it['decision'] or 'pending':<9} risk {it['risk_score']!s:>3}  "
              f"{'; '.join(it['reasons'][:2])[:90]}")

    # -- 4. what SAP does with the webhooks
    deadline = time.time() + 20
    while len(RECEIVED) < len(items) and time.time() < deadline:
        time.sleep(0.3)
    print(f"\nwebhooks received and signature-verified: {len(RECEIVED)}")
    for ev in RECEIVED:
        d = ev["data"]
        action = "lift payment block, include in payment media" if d["release_allowed"] else \
                 f"keep payment block (ZLSPR) until released in VBW · needs {', '.join(d['requirements']) or 'review'}"
        print(f"  {ev['type']:<17} {d['external_ref']:<38} {d['decision']:<8} -> SAP: {action}")
    srv.shutdown()


if __name__ == "__main__":
    main()
