# How it works

<sub>[← README](../README.md) · **How it works** · [Demo script](demo.md) · [SAP](sap-integration.md) · [Trust](trust.md) · [Production](production.md) · [Firebase](firebase-setup.md)</sub>

Before a company sends money, Verify-Before-Wire checks the request against facts: the vendor's
real bank account, its payment history, who really sent the email, official registers, and the
voice or video itself. It does not try to *prove* a voice is fake. A deepfake can sound exactly like
your CFO, but it cannot change the bank account in your vendor master, or answer the phone at the
number you have on file.

## One payment, step by step

```mermaid
sequenceDiagram
    autonumber
    participant R as Request<br/>(email · voice · video · invoice · SAP)
    participant I as Intake
    participant C as Seven checks
    participant S as Data sources
    participant P as Policy
    participant H as People
    R->>I: upload, or POST /erp/payment-runs
    I->>I: input guard, then read (Gemini, or patterns; ERP data needs no reading)
    I->>C: the structured request
    par in parallel
        C->>S: vendor master · payment history
        C->>S: OFAC · UN · EU · UK lists
        C->>S: GLEIF · VIES · RDAP · DNS · ECB FX
        C->>S: on-server media forensics
    end
    C->>P: findings, each with its source and evidence
    P->>H: RELEASE / CALL BACK / HOLD + requirements
    H->>H: callback (by phone or AI agent), second approver, release
```

1. **Input guard.** Uploads are type-checked, size-limited and screened for prompt injection.
2. **Intake.** Gemini reads emails, PDFs, voice notes and videos into one structured request, and
   every quote it reports is verified against the source. Without Gemini, pattern matching reads the
   request and the case page says so. Structured SAP data skips this step entirely.
3. **Seven checks run at once.** Each one compares the request with a fact and records where the
   fact came from:

   | Check | Compares the request with |
   |---|---|
   | Vendor record | The vendor master: is this payee known, and is the account the one you verified? |
   | Sender & domain | RDAP registration age, lookalike detection, SPF/DKIM/DMARC, free-mail senders |
   | Payment history | Amount and timing against this vendor's past payments (ECB FX for other currencies) |
   | Sanctions screen | OFAC SDN, UN Security Council, EU consolidated and UK lists (about 70,000 names) |
   | Company & bank registers | GLEIF (is the payee active, who owns the SWIFT code) and EU VIES (VAT number) |
   | Language & consistency | Urgency, secrecy, callback avoidance, and contradictions between inputs |
   | Voice & video forensics | Digital silence, still-image video, generator metadata, and an indicative AI review |

4. **Policy.** Every finding adds risk points; your versioned rules turn the score and the facts into
   **RELEASE**, **CALL BACK** or **HOLD**, plus requirements such as "callback to the number on file"
   or "second approver". This step is deterministic: no AI is involved.
5. **People decide.** The case page shows the live investigation, the evidence, and *why* the
   decision was made. An analyst records the callback, or lets the **AI agent call the vendor** on the
   number in the vendor master and record the answers. An approver, never the submitter, releases or
   holds the payment.
6. **Record.** Every step is in a hash-chained, append-only audit log, and every decision stores the
   policy hash, the input hashes, the raw findings and the sanctions-list versions used.

## Where AI is used, and where it is not

| Gemini does | Gemini never does |
|---|---|
| Read unstructured requests (text, PDF, audio, video) | Decide, score, or release a payment |
| Transcribe AI verification calls | Count an answer that is not quoted word for word in the transcript |
| Give an indicative deepfake review (capped at medium severity) | Override a deterministic check |
| Write the case summary and the callback script | Run on SAP payments (structured data needs no reading) |

When Gemini is unavailable (no credits, quota, or outage), every check still runs and decisions are
unchanged; the UI shows what fell back and offers **Top up AI credits** and **Try AI again**.

## Each company sets its own rules

- **Settings** are dials: hold and callback thresholds, which checks are required, what a failed
  check forces (hold or callback), severity overrides, and optional straight-through processing for
  clean, low-risk ERP payments (`auto_release_below`).
- **Rules** are "when this, then that": for example *any new bank account needs a callback*, or
  *payments above USD 250,000 need a second approver*.
- **Changes are safe**: a draft is simulated on past payments ("frauds stopped, legitimate payments
  interrupted"), then a *second* admin approves it. The policy's SHA-256 is stored with every decision.

## Roles

| Role | Can |
|---|---|
| Owner | Everything, including members, webhooks and API keys |
| Policy admin | Edit, simulate and approve policies; clear compliance reviews |
| Approver | Release, hold or reject payments (never ones they submitted) |
| Analyst | Run checks, record callbacks, place AI verification calls |
| Clerk | Submit checks |
| Auditor | Read everything, including the audit log |
| ERP connector (API key) | Submit payments and sync master data; never release |

## Where things live in the code

| Path | What it does |
|---|---|
| `backend/app/orchestrator.py` | One investigation, step by step (`_Investigation.run`) |
| `backend/app/agents/` | The seven checks, plus the verdict narrator |
| `backend/app/policy.py` | Policy model, rule evaluation, decision, simulation |
| `backend/app/workflow.py` | Maker-checker rules for people's actions |
| `backend/app/sources.py`, `registries.py`, `sanctions_lists.py` | Live data sources and official lists |
| `backend/app/media.py` | On-server voice and video forensics (ffmpeg + numpy) |
| `backend/app/calls.py`, `voice.py` | AI verification calls: rules, API, Twilio connector |
| `backend/app/integrations.py`, `webhooks.py` | SAP / ERP API and signed, retried webhooks |
| `backend/app/worker.py` | Background worker: unwatched cases, expired leases, webhook delivery |
| `backend/app/store.py`, `sqlstore.py`, `db_schema.py`, `migrations/` | Persistence: PostgreSQL (Alembic), Firestore, memory |
| `backend/app/trace.py`, `metrics.py` | Live data-source events and Prometheus metrics |
| `backend/static/` | The web app, landing page, onboarding tour and call screen (no build step) |
| `backend/tests/` | 122 tests on SQLite and PostgreSQL |
