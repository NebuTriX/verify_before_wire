# Running Verify-Before-Wire in production

<sub>[← README](../README.md) · [How it works](how-it-works.md) · [Demo script](demo.md) · [SAP](sap-integration.md) · [Trust](trust.md) · **Production** · [Firebase](firebase-setup.md)</sub>

## Components

```
                ┌────────────── load balancer / TLS ──────────────┐
 browsers ─────►│  web  (uvicorn app.main:app)  × N, stateless    │──► PostgreSQL 16 (RDS / Azure / Cloud SQL / on-prem)
 SAP / ERP ────►│  REST API, SSE streams, UI                      │      14 tables, Alembic-migrated, audit log append-only
                └─────────────────────────────────────────────────┘
                   worker  (python -m app.worker)  × M              ──► object storage for uploads (S3 / Azure Blob / GCS / disk)
                   claims cases by lease, runs investigations,      ──► official registers (OFAC, UN, EU, UK, GLEIF, VIES, RDAP, DNS, ECB FX)
                   delivers webhooks with retries                   ──► Gemini in your own Google Cloud project (optional)
 migrate job   (alembic upgrade head), once per release
```

* **Web** processes hold no state. Any replica can serve any request, including a live event stream:
  the replica running an investigation pushes events directly, and the others read them from the
  database.
* **Workers** pick up cases nobody is watching (ERP/API submissions) and cases whose worker died
  (expired lease). A case is claimed with an atomic lease in PostgreSQL, so workers scale
  horizontally without coordination. Small installations keep `WORKER_ENABLED=true` and run the worker
  inside the web process.
* **Migrations** are a release step: `alembic upgrade head`. With `DB_AUTO_MIGRATE=false` the app
  refuses to start on an out-of-date schema and tells you what to run. In development
  `DB_AUTO_MIGRATE=true` (the default) migrates at startup; an advisory lock lets only one instance do it.

Ready-made layouts: `deploy/docker-compose.yml` (a single VM: db, migrate, app and a scalable worker
service), `deploy.sh` (Google Cloud Run + Firestore), and the same image on Kubernetes/Kyma/ECS.

## Database

| Table | Holds | Notes |
|---|---|---|
| `tenants`, `members`, `invites`, `api_keys` | workspaces and access | API keys and invites stored as hashes only |
| `policies` | immutable policy versions | content hash recorded with every decision |
| `vendors`, `vendor_bank_accounts` | vendor master | normalised IBAN / account / BIC indexed for fast lookups |
| `payments` | payment history | indexed by tenant and vendor |
| `cases` | one row per payment check | typed columns (status, decision, score, amount, external_ref, lease) + full JSON |
| `case_events` | the streamed investigation, event by event | replayable; what the live map and timeline show |
| `audit_log` | hash-chained audit trail | **append-only**: UPDATE / DELETE / TRUNCATE raise in PostgreSQL |
| `webhooks`, `webhook_deliveries` | outbound integration + outbox | delivery attempts, status codes, retries |

Every tenant-scoped table carries `tenant_id`, and every query filters on it.

**Sizing.** A payment check writes roughly 40–60 small event rows and one case row. One million checks
a year is about 50 M event rows, which a single `db.m6g.large`-class instance handles comfortably. Past
that, partition `case_events` and `audit_log` by month. Both are append-only and are only ever read
per case or in sequence.

**Operations.** Use your provider's automated backups with point-in-time recovery. Pool size per
process is `DATABASE_POOL_SIZE` (10) + `DATABASE_MAX_OVERFLOW` (20). Size PostgreSQL
`max_connections` for (web + worker processes) × 30, or put PgBouncer in transaction mode in front.

## Configuration that matters in production

| Variable | Production value |
|---|---|
| `STORE_BACKEND` / `DATABASE_URL` | `sql` / `postgresql+asyncpg://…` (TLS: add `?ssl=require`) |
| `DB_AUTO_MIGRATE` | `false` (run the migrate job) |
| `WORKER_ENABLED` | `false` on web replicas when you run dedicated workers |
| `AUTH_MODE` | `firebase` (Google / email / SAML / OIDC). `dev` refuses to start unless explicitly allowed |
| `BLOB_BACKEND` | `s3` / `azure` / `gcs` (customer-managed keys supported) |
| `SANCTIONS_LISTS`, `SANCTIONS_REFRESH_HOURS` | `ofac,un,eu,uk`, `12` |
| `SANCTIONS_CACHE_DIR` | a writable path (e.g. `/tmp/sanctions`) |
| `WEBHOOK_ALLOW_INSECURE` | `false` (https only; private addresses refused) |
| `DEMO_ENABLED` | `false` |
| `GEMINI_MODE` | `vertex` in your own project (Workload Identity Federation from AWS/Azure) |

## Health and monitoring

* `GET /healthz`: the process is up (liveness).
* `GET /readyz`: the database answers within 3 s (readiness; take the replica out of rotation otherwise).
* Logs are JSON on stdout with a request id (`X-Request-ID`) per request. Alert on `worker loop
  error`, `webhook … failed`, `case … failed`, and on sanctions-list download errors (also visible
  on the **Data sources** page).
* Things worth putting on a dashboard: cases queued older than 2 minutes, webhook deliveries
  pending/failed, and the age of each sanctions snapshot.

## Scaling notes and known limits

* **Rate limiting** (`RATE_LIMIT_PER_MINUTE`, per workspace) is counted per process. Behind several
  replicas, add a limit at the gateway as well.
* **Sanctions lists** are downloaded by each process and cached on its disk (about 70 MB of source
  files). With many replicas, mount a shared volume at `SANCTIONS_CACHE_DIR`, or use OpenSanctions
  (`OPENSANCTIONS_API_KEY`).
* **Webhook secrets** are stored readable by the app, because they are needed to sign each delivery.
  Restrict database access accordingly and rotate a secret by re-creating the webhook.
* **Row-level security.** Tenant isolation is enforced in the application layer and covered by
  tests. Adding PostgreSQL RLS policies on `tenant_id` is the next hardening step.
* The previous single-table JSON layout (`vbw_docs`) is not migrated automatically; there were no
  production installations on it.

## Release checklist

1. CI green: unit, API and integration tests on SQLite **and** PostgreSQL (migrations applied), the
   fraud evaluation (`scripts/evaluate.py --min-catch 1.0 --max-false-alarm 0.10`), and Bandit,
   pip-audit, gitleaks, Trivy and CodeQL.
2. Run `alembic upgrade head` (the compose `migrate` service, or a Kubernetes Job).
3. Roll out web and worker replicas. Leases make a rolling restart safe: an interrupted case is
   picked up again once its lease expires.
4. Check `/readyz` and the **Data sources** page (all four lists *Ready*).
