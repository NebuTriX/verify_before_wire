# Verify-Before-Wire

An AI checkpoint that investigates a payment request before money leaves the company.
Drop in an email, voice note or invoice; five agents check it against live data and the
vendor master, and return **RELEASE**, **CALL BACK** or **HOLD** with cited evidence and an
audit PDF. We verify the request, not the voice.

## Quick start (local, no cloud needed)

```bash
cd backend
pip install -r requirements-dev.txt
python scripts/make_demo.py          # demo emails, invoices, vendor master
python -m pytest -q                  # 11 tests, uses stand-ins for Gemini and live sources
STORE_BACKEND=memory uvicorn app.main:app --reload --port 8080
```
Open http://localhost:8080. Without Google credentials, Gemini is off and intake falls
back to pattern extraction; live RDAP/DNS/FX/OFAC lookups still run if you have internet.
To use Gemini locally: `gcloud auth application-default login` and set
`GOOGLE_CLOUD_PROJECT` in `backend/.env` (see `.env.example`).

## Deploy to Google Cloud

```bash
PROJECT_ID=your-project REGION=asia-southeast1 ./deploy.sh
cd backend && GOOGLE_CLOUD_PROJECT=your-project python scripts/seed_firestore.py
```
`deploy.sh` enables APIs, creates Firestore, an uploads bucket (7-day lifecycle), a
least-privilege service account, optional Secret Manager entry for `OPENSANCTIONS_API_KEY`,
and deploys one Cloud Run service that serves both the API and the UI.

## Architecture

```
Browser ── POST /api/cases (files) ──────────────► Cloud Run (FastAPI)
        ◄─ GET /api/cases/{id}/stream (SSE) ─────  │
                                                   ├─ intake: Gemini 3 Flash on Vertex AI
                                                   │   (email text, PDF, image, audio natively)
                                                   ├─ 5 agents in parallel (asyncio)
                                                   │   vendor · domain · history · sanctions · language
                                                   ├─ verdict: deterministic rules + Gemini narrative
                                                   ├─ Firestore: cases, events, vendors, payments
                                                   └─ Cloud Storage: uploaded inputs
Live sources: RDAP (rdap.org) · public DNS · ECB rates (Frankfurter) · OpenSanctions or OFAC SDN
```

The stream handler that first claims a case runs it (Firestore transaction + lease); any
other viewer tails the persisted events, so streaming works across Cloud Run instances
and survives reconnects (`Last-Event-ID`).

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/cases` | multipart `files` (≤6, ≤15 MB each) → `{case_id}` |
| GET | `/api/cases/{id}/stream` | SSE events; `?replay=1` plays the recorded run |
| GET | `/api/cases/{id}` | case state |
| GET | `/api/cases/{id}/report.pdf` | audit case file (`report.html` fallback) |
| GET | `/api/cases/{id}/export` | recording format for replays |
| GET/POST | `/api/demo`, `/api/demo/{scenario}` | list / run bundled scenarios |
| GET | `/api/vendors`, `/healthz` | vendor master, health |

Events: `case.started`, `intake.started`, `intake.completed`, `agent.started`, `finding`,
`agent.completed`, `agent.failed`, `verdict`, `case.completed`, `case.failed`.

## Demo fail-safe

After deploying, record each scenario once and redeploy so recordings ship in the image:
```bash
python backend/scripts/record_replays.py https://<your-service-url>
./deploy.sh
```
In the UI press **Shift+R** (footer shows "recorded playback") or open `/?replay=1`.

## Configuration

All settings in `backend/app/config.py`, overridable by env var (upper case).
Key ones: `GEMINI_MODEL` (default `gemini-3-flash-preview`, global endpoint),
`COMPANY_DOMAINS`, `OPENSANCTIONS_API_KEY`, `BASE_CURRENCY`, `BUSINESS_TIMEZONE`.
For a more striking demo, regenerate data with a domain you registered:
`FRAUD_DOMAIN=your-lookalike.com python scripts/make_demo.py`.

## Not production yet

No user auth (add IAP or Firebase Auth before real use), synthetic vendor master,
and no rate limiting. Decision support only: the approver makes the final call.
