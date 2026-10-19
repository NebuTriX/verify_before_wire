# Three-minute demo

<sub>[← README](../README.md) · [How it works](how-it-works.md) · **Demo script** · [SAP](sap-integration.md) · [Trust](trust.md) · [Production](production.md) · [Firebase](firebase-setup.md)</sub>

A script for presenting Verify-Before-Wire live. Every step uses the bundled sample data; nothing
needs to be prepared except a running app.

**Before you start** (5 minutes ahead)

```bash
docker compose up --build          # or: make db && make dev
```

- Open http://localhost:8080 once, so the official sanctions lists finish downloading (about 70 MB).
- Optional: `export GEMINI_API_KEY=…` before starting, for AI reading, transcription and summaries.
  Without it, every check still runs and the decisions are the same.
- Keep a second terminal ready for `make sap-demo`.

---

### 0:00 · The problem (landing page)
Open http://localhost:8080.

> "Business email compromise and deepfake voice notes trick finance teams into paying fraudsters.
> Detecting a perfect deepfake is an arms race. So we don't try to win it: **we verify the request,
> not the voice.**"

Scroll once: the parallax hero, the live mockup, the data sources it checks.

### 0:25 · Onboarding (Start free)
Sign in with any email, create a workspace. The **onboarding film** plays, then the guided tour.

Click **Use sample data**: the **preview** shows every vendor, verified account and payment summary,
with integrity checks. Click **Add to my workspace**; it confirms what was read back from PostgreSQL.

### 0:55 · A deepfake CFO, investigated live
**Payment checks → CFO voice note + email, SGD 480k.**

Point at the **live map**: data sources on the left, seven checks in the middle, the policy on the
right. Dots travel to each register and back with real latency, and the **event stream** shows every
server-sent event.

> "Lookalike domain: `acme-hold1ngs.com`. A payee that isn't in the vendor master. And the voice
> note has *exact digital silence* between words. A real microphone always hears the room."

**HOLD.** Click **Why this decision?**: the score build-up, each signal linked to its evidence, the
policy path. *No AI involved in the decision.*

### 1:35 · The AI calls the real vendor
Run **Real vendor, new bank account, USD 61k** (a genuine, authenticated supplier email asking to
pay a new account). In the Decision panel: **Let the AI agent call and record it.**

> "It dials the number **in our vendor master**, never one from the email. The questions come from
> the case facts. Every answer must quote the vendor's own words from the transcript, or it doesn't
> count, and a person still accepts it."

Answer into the microphone as the vendor ("No, we never changed our bank details"). The transcript
shows the denial, ✓ *found in transcript*, and the recording's SHA-256.

### 2:10 · SAP, at scale
In the second terminal:

```bash
make sap-demo
```

> "An SAP F110 payment run, three payments, checked in about two seconds: one released, two held.
> Signed webhooks tell SAP to keep the payment block."

(Use `make dev` for this step: with `docker compose up`, the simulator's local webhook receiver is
outside the container, so the run is checked but the webhooks can't reach it.)

### 2:35 · Trust
Open **Audit log**. The browser recomputes every SHA-256 and matches the server; PostgreSQL itself
refuses edits and deletes. Open **Data sources**: each sanctions list with its publication date and
fingerprint.

> "Your policy decides. Every decision is reproducible. Nothing can be rewritten."

### 2:55 · Close
> "Verify before you wire."

---

**If something goes wrong during the demo**

| Symptom | What to do |
|---|---|
| Amber "AI paused" banner | Keep going: checks and decisions don't depend on AI. Mention graceful degradation. |
| Sanctions list "still loading" | OFAC loads first; the others follow. Payments are held until OFAC is ready (fail-closed). |
| Microphone blocked | Allow it in the address bar, or record the callback by hand in the Decision panel. |
| Worried about the venue network | Record replays beforehand with `python backend/scripts/record_replays.py <url> <workspace> <api-key>`; then **Shift+R** on a sample case plays the recorded run. |
