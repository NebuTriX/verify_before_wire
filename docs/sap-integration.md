# Verify-Before-Wire as an SAP plug-in

<sub>[← README](../README.md) · [How it works](how-it-works.md) · [Demo script](demo.md) · **SAP** · [Trust](trust.md) · [Production](production.md) · [Firebase](firebase-setup.md)</sub>

Verify-Before-Wire (VBW) sits between **"invoice approved"** and **"money leaves the bank"** in SAP.
SAP stays the system of record and the only system that pays. VBW never moves money; it tells SAP
whether a payment may go, and SAP enforces that with its own native control, the **payment block**.

```
 SAP S/4HANA / ECC                       SAP BTP Integration Suite (CPI)            Verify-Before-Wire
 ─────────────────                       ───────────────────────────────            ──────────────────
 Vendor master (LFA1/LFB1/LFBK, BP) ──── nightly + on change ───────────────► PUT /erp/vendors
 Cleared items (BSAK / ACDOCA)      ──── nightly delta ─────────────────────► PUT /erp/payment-history
 Invoice posted with block "V"      ──── event / scheduled pull ────────────► POST /erp/payment-runs
                                                                              │ checks: vendor & bank on file,
                                                                              │ history, OFAC/UN/EU/UK, GLEIF,
                                                                              │ VIES, domain, policy
 Payment block lifted (ZLSPR = ' ') ◄─── iFlow verifies signature ◄──────── webhook case.recommended /
 F110 pays it in the next run            and calls Journal Entry – Change       case.decided (signed)
 Block stays: AP sees reason in VBW ◄──────────────────────────────────────── (HOLD / CALL BACK)
```

The demo of this whole loop runs locally: `python backend/scripts/sap_simulator.py` (see the end).

## Why the payment block

Every SAP finance team already trusts the payment block (`BSEG-ZLSPR`): the payment program
(F110) skips blocked items. VBW therefore needs **no modification of F110 and no core changes**:

1. Invoices to be verified are posted with a dedicated block reason, e.g. **`V` – "awaiting
   Verify-Before-Wire"** (configure it with your other block reasons, transaction OB27, and set it
   as the vendor's default block in LFB1-ZAHLS or by substitution for the company codes in scope).
2. VBW checks the payment. Only when the case is **released** (by a person in VBW, or automatically by
   your policy for clean low-risk payments) does the integration lift block `V`.
3. HOLD and CALL BACK cases keep the block. AP works the case in VBW (callback to the number on file,
   second approver, compliance clearance); the release then triggers `case.decided` and the block is lifted.

If VBW is unavailable, nothing is paid by accident: the block simply stays (fail-closed).

## Two integration patterns

| | **A. Clean core, event-driven (recommended)** | **B. Payment-run gate (ABAP add-on)** |
|---|---|---|
| Runs on | SAP BTP Integration Suite (CPI) or any iPaaS | In SAP: a Z-program / enhancement around F110 |
| Works with | S/4HANA Cloud, S/4HANA, ECC | S/4HANA on-premise / private cloud, ECC |
| What is checked | Each blocked invoice, with the vendor's bank details from the master | The exact proposal lines F110 will pay (REGUH/REGUP: final bank, amount) |
| How SAP acts | Lift block `V` via the Journal Entry – Change API (S/4HANA) or a small RFC (ECC) | Leave non-released items blocked before the payment run executes |

Pattern A is simplest to roll out and keeps SAP upgrade-safe. Pattern B checks the bank details at
the last possible moment, so teams with on-premise SAP often add it as a second gate. Both call the
same API. Your SAP team chooses the enhancement point for B (e.g. the payment-proposal BTEs in FIBF
for your release); the HTTP call and field mapping below are the same either way.

**Bank details changing after the check.** In pattern A, SAP pays to the bank account in the vendor
master at payment time. Sync vendor-master changes (BP change events or change documents on LFBK)
to `PUT /erp/vendors`: when a bank account changes, re-submit the vendor's open items. VBW will flag
the new account (no payment history to it, callback required). Pattern B avoids the gap entirely.

## API (all JSON, authenticated with an *ERP connector* API key)

Create the key in **Setup → Connect your accounting system → Access: ERP connector**. That role can
submit payments and sync master data, and **can never release a payment or change policy**.

### Check a payment run: `POST /api/t/{tenant}/erp/payment-runs?wait=30`

```json
{
  "run_date": "2026-10-18", "run_id": "ACME1", "company_code": "1000",
  "items": [{
    "external_ref": "F110:20261018:ACME1:1000:0000100234",
    "vendor": {"id": "0000100234", "name": "Lion City Logistics Pte Ltd", "country": "SG", "vat_number": null},
    "amount": 78400.00, "currency": "SGD",
    "bank": {"country": "SG", "bank_key": "7171", "account": "0721234567", "iban": null,
             "swift": "DBSSSGSG", "bank_name": "DBS Bank Ltd", "account_holder": null},
    "documents": [{"number": "5100004711", "fiscal_year": "2026", "reference": "LCL-INV-2026-0988"}],
    "requested_by": "AP_CLERK01"
  }]
}
```

* `external_ref` is your key: re-sending the same one returns the existing case (idempotent), and
  `GET /erp/payments/{external_ref}` returns its current state for polling.
* `?wait=N` (max 60 s) returns results inline; without it the call returns immediately and results
  arrive by webhook. A typical check takes 1–3 s; the background worker runs it either way.
* Single payments: `POST /erp/payments` with one item.

Each item comes back as:

```json
{"case_id": "c_…", "external_ref": "F110:…", "decision": "HOLD", "risk_score": 100,
 "workflow_state": "awaiting_decision", "requirements": ["callback"], "release_allowed": false,
 "reasons": ["Bank account differs from the verified account on file", "…"], "case_url": "/#/case/c_…"}
```

**Only `release_allowed: true` may lift the block.** A RELEASE *recommendation* still waits for a
person unless your policy turns on straight-through processing (`auto_release_below`, see below).

### Field mapping

| VBW field | SAP (payment proposal) | SAP (vendor master) |
|---|---|---|
| `vendor.id` | REGUH-LIFNR | LFA1-LIFNR / BP number |
| `vendor.name` | REGUH-NAME1 | LFA1-NAME1 |
| `vendor.country` | REGUH-LAND1 | LFA1-LAND1 |
| `vendor.vat_number` | | LFA1-STCEG |
| `amount`, `currency` | REGUH-RWBTR (absolute), REGUH-WAERS | |
| `bank.country`, `bank.bank_key`, `bank.account` | REGUH-ZBNKS, ZBNKL, ZBNKN | LFBK-BANKS, BANKL, BANKN |
| `bank.iban`, `bank.swift` | REGUH-ZIBAN, ZSWIF | TIBAN-IBAN, BNKA-SWIFT |
| `bank.account_holder` | REGUH-ZNME1 / KOINH | LFBK-KOINH |
| `documents[].number / fiscal_year / reference` | REGUP-BELNR, GJAHR, XBLNR | |
| `company_code`, `run_date`, `run_id` | REGUH-ZBUKR, LAUFD, LAUFI | |
| vendor `email_domains` | | ADR6-SMTP_ADDR (domain part) |
| vendor `callback_phone` | | ADR2 / contact person, **as verified by your master-data team** |

### Master data: `PUT /erp/vendors` and `PUT /erp/payment-history`

Upserts by vendor id. Send everything nightly and changes as they happen. The callback phone number
must come from your verified master data. VBW only ever tells staff to call that number, never a
number from the payment request. Payment history (cleared items, ideally 2–3 years) gives the
amount and timing baselines.

### Webhooks

Register the iFlow's HTTPS endpoint in **Setup → Webhooks** (or `POST /api/t/{tenant}/webhooks`).
Events: `case.recommended`, `case.decided` (released / held / rejected), `case.failed`.
Deliveries are retried with backoff for about 4 hours and listed in Setup with their HTTP results.

Every request carries `VBW-Signature: t=<unix>,v1=<hex>` where
`v1 = HMAC-SHA256(secret, "<t>.<raw body>")`. Verify it in the iFlow (Groovy script step) and
reject anything older than 5 minutes:

```groovy
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
def body = message.getBody(String)
def sig  = message.getHeaders().get("VBW-Signature")             // t=...,v1=...
def p    = sig.split(",").collectEntries { it.split("=", 2) as List }
def t    = p["t"] as long
if (Math.abs(System.currentTimeMillis() / 1000 - t) > 300) throw new Exception("stale webhook")
def mac  = Mac.getInstance("HmacSHA256")
mac.init(new SecretKeySpec(secret.getBytes("UTF-8"), "HmacSHA256"))    // secret from the CPI secure store
def expected = mac.doFinal("${t}.${body}".getBytes("UTF-8")).encodeHex().toString()
if (!java.security.MessageDigest.isEqual(expected.bytes, p["v1"].bytes)) throw new Exception("bad signature")
```

Then, only if `data.release_allowed == true`, clear block `V` on the documents of `data.external_ref`.

### Pattern B sketch (ABAP, on-premise)

```abap
" After the F110 proposal (LAUFD/LAUFI), send the proposal lines to VBW and keep blocks on anything
" not released. HTTP destination VBW (SM59) holds the URL; the API key is in SSF/secure storage.
SELECT * FROM reguh INTO TABLE @DATA(lt_reguh) WHERE laufd = @p_laufd AND laufi = @p_laufi AND xvorl = 'X'.
" ...build the JSON above from REGUH/REGUP (/ui2/cl_json=>serialize)...
cl_http_client=>create_by_destination( EXPORTING destination = 'VBW' IMPORTING client = DATA(lo_http) ).
lo_http->request->set_method( 'POST' ).
lo_http->request->set_header_field( name = '~request_uri' value = |/api/t/{ gv_tenant }/erp/payment-runs?wait=60| ).
lo_http->request->set_header_field( name = 'Authorization' value = |Bearer { gv_api_key }| ).
lo_http->request->set_content_type( 'application/json' ).
lo_http->request->set_cdata( lv_json ).
lo_http->send( ). lo_http->receive( ).
" For each item with release_allowed = abap_false: keep/put payment block 'V' on its REGUP documents
" (proposal edit, or FI_DOCUMENT_CHANGE on BSEG-ZLSPR) so the payment run does not pay it.
```

## Straight-through processing

Large payment runs cannot wait for a person to click every clean payment. In **Policy**, set
`"auto_release_below": 10` (must not exceed `callback_at`). A payment is then released by the
policy itself only if the recommendation is RELEASE, there are no requirements, every required check
ran, and the risk score is below the threshold. Each auto-release is written to the audit log as
`case.auto_released` with the policy version. Like every policy change, turning this on needs a
second admin's approval and can be simulated on past payments first.

## Hosting next to SAP

The same Docker image runs on SAP BTP (Kyma runtime), AWS, Azure, Google Cloud or on-premise with
PostgreSQL (see `docs/production.md`). Data stays in your tenancy; Gemini can use your own Google
Cloud project, and ERP payments involve no AI at all (structured data needs no reading).

## Try it locally

```bash
docker compose -f docker-compose.dev.yml up -d          # PostgreSQL
cd backend && uvicorn app.main:app --reload --port 8080  # WEBHOOK_ALLOW_INSECURE=true in backend/.env
python scripts/sap_simulator.py
```

The simulator plays SAP + CPI. It syncs three vendors and their payment history, registers a local
webhook receiver, sends an F110 run of three payments (routine, changed bank account, a sanctioned
account holder), verifies each signed webhook, and prints what SAP would do:

```
F110 run R39078: 3 payments checked in 2.0s  ->  {'RELEASE': 1, 'CALLBACK': 0, 'HOLD': 2}
webhooks received and signature-verified: 3
  case.recommended  F110:…:00001LCL  RELEASE  -> SAP: keep payment block until released in VBW
  case.recommended  F110:…:00001HOS  HOLD     -> SAP: keep payment block · needs compliance_review
  case.recommended  F110:…:00001PRC  HOLD     -> SAP: keep payment block · needs callback
```
