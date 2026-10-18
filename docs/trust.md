# Why a finance team can trust Verify-Before-Wire

<sub>[← README](../README.md) · [How it works](how-it-works.md) · [Demo script](demo.md) · [SAP](sap-integration.md) · **Trust** · [Production](production.md) · [Firebase](firebase-setup.md)</sub>

A payment-fraud control earns trust in three ways: it is **right for verifiable reasons**, it
**cannot be quietly bypassed or rewritten**, and it **keeps the company in control of its data and
decisions**. Below, each claim names the mechanism and how you can check it yourself.

## 1. The decision is rules, not a black box

* **No AI in the decision.** Risk points come from deterministic checks; the outcome (RELEASE, CALL
  BACK, HOLD) comes from your versioned policy. Gemini only reads unstructured requests (emails, voice
  notes) and writes a summary, and every quote it reports is verified against the source text before
  it is shown. ERP payments involve no AI at all. *Check:* each case's **How this decision was made**
  panel shows the score build-up and every rule that fired.
* **Your policy, under four-eyes control.** Thresholds and rules are versioned. A change is simulated
  on past payments ("how many frauds stopped, how many legitimate payments interrupted") and needs a
  second admin's approval before it applies. The policy's SHA-256 is stored with every decision.
* **Fail-closed.** If a required check cannot run, for example the OFAC list is unavailable, the
  payment is held, not waved through. In SAP the payment block simply stays.
* **Measured, not claimed.** `scripts/evaluate.py` replays labelled fraud and legitimate payments
  (plus real legitimate emails from public datasets) and fails CI if a fraud slips through or false
  alarms rise above 10%.

## 2. Evidence from authoritative sources, with provenance

| Question | Source | Why it is credible |
|---|---|---|
| Is this the bank account we verified for this vendor? Have we paid it before? | **Your own vendor master and payment history** (synced from SAP) | Your system of record. The strongest evidence there is. |
| Is the payee or its bank sanctioned? | **OFAC SDN, UN Security Council, EU consolidated (FSF), UK Sanctions List (FCDO)**, about 70,000 names | Downloaded from the governments that publish them, refreshed every 12 h |
| Is the payee a real, active company? Does this SWIFT code belong to the bank named? | **GLEIF** (Global LEI Foundation, G20/FSB mandate) | The official legal-entity register; maps BICs to legal entities |
| Is the invoicing company's EU VAT number valid? | **EU VIES** (European Commission) | The official VAT register |
| Is the sender's domain new or a lookalike? Does its mail authenticate? | **RDAP** registries, public **DNS** (SPF, DMARC), the email's own headers | The internet's registries of record |
| Is the amount unusual? | **ECB reference rates** + your payment history | Official FX; your own baseline |

**Provenance is stored with every decision.** Sanctions findings record which list versions were
screened (publication date and SHA-256 of the file), and registry findings link to the exact GLEIF
record. Months later an auditor can see what the payment was checked against on that day.
*Check:* the **Data sources** page, and the list chips on any sanctions check.

Absence of a record is treated honestly. Most small suppliers have no LEI, so "not in GLEIF" adds no
risk; contradictions do (a SWIFT code owned by a different bank, an IBAN from another country, a
dissolved company, an invalid VAT number).

### Voice, video and AI calls: multimodal, but never the decider

* **Voice notes and video messages** are analysed on the server: digital silence (synthetic speech has
  none of a microphone's room noise), a still picture under a voice-over, and generator names in the
  file's metadata. Gemini adds an indicative review. These signals are capped at *medium* because no
  deepfake detector is reliable; the request is verified against your records regardless.
* **AI verification calls** dial only the number in your vendor master. The questions are templates
  filled with case facts. The recording is kept with its SHA-256. An answer counts only if its quote
  appears word for word in the transcript, and, when a second independent transcript exists, the two
  agree. The outcome is a fixed rule over those verified answers. A person listens and accepts the call
  before it counts as the required callback, and an approver still releases the payment.

## 3. A record nobody can rewrite

* **Hash-chained audit log.** Every action (submission, recommendation, callback, release, policy
  change, data import, API key and webhook change) is an entry carrying the SHA-256 of the entry
  before it. Editing or deleting any entry breaks every hash after it.
* **Append-only in the database.** In PostgreSQL, triggers reject UPDATE, DELETE and TRUNCATE on the
  audit table, so not even the application can rewrite history.
* **Independently verifiable.** The **Audit log** page re-computes every hash in your own browser and
  compares it with the server's check. Export the log as JSON at any time.
* **Reproducible decisions.** Each case stores the input file hashes, the raw findings, the policy
  version and hash, and the full event stream of the investigation.

## 4. Controls that match how fraud actually happens

* **Callbacks only to numbers on file.** The callback script uses the vendor master's verified number,
  never one from the request. Fraudsters put their own number in the email.
* **Separation of duties.** Whoever submits a payment cannot release it. Releasing a HOLD needs a
  reason and, depending on policy, a second approver. ERP API keys can submit payments and sync data
  but can never release a payment or change policy.
* **Bank-detail changes are the main signal.** A new account, an account holder whose name doesn't
  match, or an account never paid before forces a callback, the control that stops most business
  email compromise.

## 5. You keep control of data and infrastructure

* **Run it where your data lives:** your AWS, Azure or Google Cloud account, SAP BTP (Kyma) or
  on-premise, with your PostgreSQL and object storage (customer-managed keys supported).
* **AI under your contract:** Gemini through *your own* Google Cloud project (Workload Identity
  Federation from AWS/Azure: no stored keys), with account numbers, emails and phone numbers masked
  before any text is sent. Or no AI at all for ERP-only use.
* **Least privilege by default:** role-based access (owner, policy admin, approver, analyst, clerk,
  auditor, ERP connector), hashed API keys and invites, strict CSP, request ids on every log line.
* **Security in the pipeline:** every change runs Bandit, pip-audit, gitleaks, Trivy and CodeQL, and
  the test suite on SQLite and real PostgreSQL.

## What would make the evidence even stronger (commercial sources)

The architecture adds a data source as one more check with its own findings, so each of these is an
incremental integration, not a redesign:

* **Account-ownership verification.** EU *Verification of Payee* (mandatory for euro credit transfers
  since October 2025) and UK *Confirmation of Payee* confirm that the account holder's name matches
  the payee, usually through your bank's API. SWIFT *Payment Pre-validation* does the same
  cross-border. This is the single highest-value addition.
* **Company registries and credit data:** Dun & Bradstreet, OpenCorporates, Creditsafe for
  ownership, directors and recent changes of address or officers.
* **Commercial sanctions and PEP data:** LSEG World-Check, Dow Jones, or OpenSanctions under a
  commercial licence, for adverse media and politically exposed persons beyond the official lists.
* **Shared fraud intelligence:** known mule accounts and fraud-reported IBANs from bank or industry
  consortia.

## What it is not (yet)

* Not a replacement for KYC/KYB onboarding or for your bank's own fraud controls. It is the last
  check before money leaves, using your data.
* A sanctions name match is a *possible* match for compliance to clear, not a determination.
* Independent assurance is the next step for a production rollout: SOC 2 Type II / ISO 27001, an
  external penetration test, a DPA template, and PostgreSQL row-level security as defence in depth for
  tenant isolation.
