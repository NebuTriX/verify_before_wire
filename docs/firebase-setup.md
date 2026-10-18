# Run Verify-Before-Wire on your Firebase project

<sub>[← README](../README.md) · [How it works](how-it-works.md) · [Demo script](demo.md) · [SAP](sap-integration.md) · [Trust](trust.md) · [Production](production.md) · **Firebase**</sub>

Firebase gives you sign-in (Firebase Authentication) and the database (Cloud Firestore). Gemini can
use an AI Studio key, or Vertex AI billed to the same Google Cloud project. Every value below goes
into `backend/.env` (gitignored). Check everything with:

```bash
cd backend && python scripts/firebase_check.py
```

It tests the web key against Firebase Auth, writes a probe document to Firestore and asks Gemini one
question, then tells you exactly what to fix.

## 1. Sign-in (Firebase Authentication)

Firebase Console → your project → **Project settings → General → Your apps → Web app** (create one
named `verify_before_wire` if there is none). Copy `apiKey`, `projectId` and `appId`:

```ini
AUTH_MODE=firebase
FIREBASE_PROJECT_ID=<projectId>            # the id, e.g. google-ai-developer-1a2b3, not the display name
FIREBASE_API_KEY=<apiKey of the web app>
FIREBASE_APP_ID=<appId>                    # optional
```

Then in **Authentication → Sign-in method**, enable **Google** and **Email/Password**. Under
**Authentication → Settings → Authorized domains**, make sure `localhost` and your deployed domain are
listed.

The server verifies every Firebase ID token itself against Google's public keys, using only
`FIREBASE_PROJECT_ID`, so sign-in needs no service account. Users must verify their email before
they can open a workspace.

> **Use a different key for Gemini.** The Firebase web `apiKey` is public: it is sent to every
> browser. If the same key can call Gemini, anyone can copy it from the page and spend your credits.
> In Google Cloud Console → APIs & Services → Credentials, restrict the web key to the Identity
> Toolkit / Firebase APIs (with HTTP-referrer restrictions), and give the server its own Gemini key, or
> use Vertex AI (step 3).

## 2. Database (Cloud Firestore)

1. Firebase Console → **Firestore Database → Create database** (Native mode, a region close to you).
2. **Project settings → Service accounts → Generate new private key.** Save the JSON **outside the
   repository**, e.g. `~/.config/vbw/firebase-sa.json`.
3. Configure:

```ini
STORE_BACKEND=firestore
GOOGLE_CLOUD_PROJECT=<projectId>
GOOGLE_APPLICATION_CREDENTIALS=/Users/you/.config/vbw/firebase-sa.json
BLOB_BACKEND=gcs                           # uploads in Cloud Storage for Firebase
UPLOADS_BUCKET=<projectId>.firebasestorage.app
```

For local development without Cloud Storage, `BLOB_BACKEND=file` keeps uploads on disk. On Cloud Run
(`deploy.sh`) the service's own identity is used and no key file is needed.

PostgreSQL (`STORE_BACKEND=sql`) remains supported for AWS, Azure and on-premise installations. Both
stores run the same test suite.

## 3. Gemini

**Option A: AI Studio key** (`GEMINI_MODE=api_key`, `GEMINI_API_KEY=…`). Billing is prepaid credits on
the AI Studio project. When they run out, the app shows **Top up AI credits** (it opens
https://ai.studio/projects), keeps working with pattern reading and on-server forensics, and has a
**Try AI again** button for after you top up.

**Option B: Vertex AI on the same project** (recommended for production). It is billed to the
project's billing account, needs no prepaid credits, and keeps data in your project:

```ini
GEMINI_MODE=vertex
GEMINI_PROJECT=<projectId>
GEMINI_LOCATION=global
```

Enable the **Vertex AI API** for the project and give the service account the **Vertex AI User**
role. The service-account key from step 2 is then used for both Firestore and Gemini.

## What keeps working when a service is down

| Down | What happens | What you see |
|---|---|---|
| Gemini (credits, quota, outage) | Requests read with pattern matching; voice/video checked with on-server forensics; summaries from templates. **Decisions unchanged** | Amber banner with **Top up AI credits** and **Try AI again**; "AI paused" in the sidebar; Gemini shown as *paused* on the live map |
| A sanctions list (UN/EU/UK) | Screening uses the other lists; the case lists what was skipped | Banner + *Data sources* page |
| OFAC list | Payments are **held** (fail-closed) | Red banner |
| GLEIF / VIES / RDAP / DNS | That check reports "not verified" instead of guessing | Banner names the register |
| Database | New checks pause; nothing is lost | Red banner; `/readyz` returns 503 |
| The server itself | The browser keeps your page and retries | "Can't reach the server" banner |
