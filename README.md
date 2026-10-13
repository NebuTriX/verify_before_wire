# Verify-Before-Wire

An AI checkpoint that investigates a payment request before money leaves the company.
Drop in an email, voice note or invoice; five agents check it against live data and the
vendor master, and return **RELEASE**, **CALL BACK** or **HOLD** with cited evidence and an
audit PDF. We verify the request, not the voice.

Read **docs/how-it-works.html** first: it explains the whole system in plain words with an architecture diagram.

## Quick start (local)

```bash
cd backend
pip install -r requirements-dev.txt
python scripts/make_demo.py               # sample vendors, payments, scenario files
python -m pytest -q                       # 18 tests
STORE_BACKEND=memory AUTH_MODE=dev uvicorn app.main:app --reload --port 8080
```
Open http://localhost:8080, sign in with any email (development sign-in), create a workspace,
click "Load sample data", then run a sample case. Gemini is used when `GOOGLE_CLOUD_PROJECT` is set
and you've run `gcloud auth application-default login`; otherwise intake falls back to pattern matching.

## Deploy

1. Create a GCP project with billing. Add Firebase to it (console.firebase.google.com), register a web app,
   and enable the Google and Email sign-in providers. For enterprise SSO, enable Identity Platform SAML/OIDC.
2. Run:
```bash
PROJECT_ID=your-project FIREBASE_API_KEY=<web api key> REGION=asia-southeast1 ./deploy.sh
```
3. Add the Cloud Run URL's domain to Firebase Auth → Settings → Authorized domains.
4. Sign in, create your workspace, import data or load the sample, invite an approver.
5. Optional replay fail-safe: create an analyst API key, then
   `python backend/scripts/record_replays.py <url> <workspace-id> <api-key>` and redeploy.

## Integration API (for ERP / treasury systems)

```bash
curl -X POST "$URL/api/t/$WORKSPACE/checks" \
  -H "Authorization: Bearer $VBW_API_KEY" -H "Idempotency-Key: payment-48213" \
  -F "files=@request.eml" -F "external_ref=PAY-48213"
```
Returns the full case with the `verdict` (decision, score, requirements, rules applied). The ERP should
release funds only after the case's `workflow.state` becomes `released`.

## Configuration

Platform settings are environment variables (see `backend/app/config.py`). Company-level settings,
policies and data are managed by each workspace in the app itself.
