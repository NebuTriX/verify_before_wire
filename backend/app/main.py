"""HTTP API + static UI, multi-tenant. Every /api/t/{tid}/... route checks the caller's role
in that workspace. One Cloud Run service serves both, so event streams are never buffered."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, ValidationError

from . import integrations, metrics, onboarding, ratelimit, trace, voice, webhooks, workflow
from .audit import verify_chain
from .auth import Access, access, assert_safe_auth_mode, principal_from
from .config import Settings, get_settings
from .deps import build_deps
from .models import Case, CaseStatus, Event, utcnow
from .orchestrator import AGENTS, TERMINAL, Deps, start_case
from .parsing import DOCS, MEDIA, UnsupportedFile, Labeler, parse_upload
from .policy import TEMPLATES, Policy, PolicySettings, Rule, policy_from_template, simulate
from .report import build_html, build_pdf
from .tenancy import ApiKey, Invite, Member, Perm, Role, Tenant, TenantSettings, hash_secret, new_secret

log = logging.getLogger("vbw")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logging.basicConfig(level=s.log_level, format='{"severity":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}')
    assert_safe_auth_mode(s.auth_mode, s.allow_dev_auth)
    warm = worker = None
    stop = asyncio.Event()
    if not hasattr(app.state, "deps"):
        app.state.deps = build_deps(s)
        warm = asyncio.create_task(app.state.deps.sources.sanctions.warm())
        if app.state.deps.gemini:          # learn at startup whether AI answers (credits, quota, key)
            app.state.ai_probe = asyncio.create_task(app.state.deps.gemini.probe())
        if s.worker_enabled:
            from .worker import run_worker
            worker = asyncio.create_task(run_worker(app.state.deps, stop))
    yield
    stop.set()
    if worker:
        await asyncio.wait([worker], timeout=10)
    if warm:
        warm.cancel()
    await app.state.deps.sources.aclose()


API_DESCRIPTION = """
An AI-assisted checkpoint that investigates a payment request **before money leaves the company**.

* **Payment checks**: upload an email, voice note or invoice (`/cases`), or send structured ERP data
  (`/erp/payments`, `/erp/payment-runs`) — SAP F110 fields map 1:1, see `docs/sap-integration.md`.
* **Live stream**: `/cases/{id}/stream` is a server-sent event stream of every check and data-source lookup.
* **Decisions** come from your versioned policy, never from the AI. Every action lands in a hash-chained,
  append-only audit log (`/audit`, `/audit/verify`).

Authenticate with `Authorization: Bearer <API key>` (Setup → API keys) or a signed-in session.
"""
TAGS = [{"name": "ERP integration", "description": "Structured payments, payment runs, master-data sync (SAP)."},
        {"name": "Webhooks", "description": "Signed callbacks to your ERP (HMAC-SHA256, retried ~4 h)."},
        {"name": "Data sources", "description": "Freshness and provenance of every register used."}]
app = FastAPI(title="Verify-Before-Wire API", version="2.0.0", lifespan=lifespan, description=API_DESCRIPTION,
              openapi_tags=TAGS, docs_url=None, redoc_url=None)

MAX_REQUEST_BYTES = 110 * 1024 * 1024
CSP = ("default-src 'self'; script-src 'self' https://www.gstatic.com https://apis.google.com; "
       "style-src 'self' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data: blob:; media-src 'self' blob:; "
       "frame-src https://*.firebaseapp.com https://*.web.app blob:; "   # Firebase sign-in frame on either auth domain
       "connect-src 'self' https://*.googleapis.com; frame-ancestors 'none'; base-uri 'none'")


@app.middleware("http")
async def security_and_logging(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", rid):
        rid = uuid.uuid4().hex[:16]
    t0 = time.perf_counter()
    try:
        if int(request.headers.get("content-length") or 0) > MAX_REQUEST_BYTES:
            return JSONResponse({"detail": "Request too large."}, status_code=413)
    except ValueError:
        return JSONResponse({"detail": "Bad Content-Length."}, status_code=400)
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)   # a route may set a stricter one
    resp.headers.update({"X-Request-ID": rid, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                         "Referrer-Policy": "no-referrer",
                         "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
                         "Permissions-Policy": "camera=(), microphone=(self), geolocation=(), payment=()",   # mic: AI verification calls
                         "Cross-Origin-Opener-Policy": "same-origin-allow-popups"})
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/assets") or request.url.path == "/":
        resp.headers["Cache-Control"] = "no-cache"   # revalidate (ETag), so a deploy never leaves a stale UI
    route = request.scope.get("route")
    if route is not None:
        metrics.HTTP.labels(request.method, getattr(route, "path", "other"), str(resp.status_code)).observe(time.perf_counter() - t0)
    if not request.url.path.startswith("/assets"):
        log.info(json.dumps({"rid": rid, "method": request.method, "path": request.url.path,
                             "status": resp.status_code, "ms": int((time.perf_counter() - t0) * 1000)}))
    return resp


def deps(r: Request) -> Deps:
    return r.app.state.deps


async def read_limited(f: UploadFile, max_bytes: int) -> bytes:
    """Read an upload in chunks and stop as soon as it exceeds the limit."""
    buf, total = bytearray(), 0
    while chunk := await f.read(1024 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, f"{f.filename} is larger than {max_bytes // (1024 * 1024)} MB.")
        buf += chunk
    return bytes(buf)


@app.exception_handler(ValidationError)
async def pyd_error(_: Request, exc: ValidationError):
    return JSONResponse({"detail": [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()]},
                        status_code=422)


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse({"detail": "Something went wrong on our side. Try again."}, status_code=500)


# ================================================================= public ====
@app.get("/healthz")
async def healthz(request: Request):
    d = deps(request)
    return {"ok": True, "version": app.version, "model": d.settings.gemini_model, "gemini": d.gemini is not None,
            "store": d.settings.store_backend, "auth": d.settings.auth_mode}


DOCS_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")


@app.get("/docs", include_in_schema=False)
async def api_docs():
    """Interactive API reference. Swagger UI loads from a CDN, so this page alone gets a wider CSP."""
    from fastapi.openapi.docs import get_swagger_ui_html
    r = get_swagger_ui_html(openapi_url="/openapi.json", title="Verify-Before-Wire API")
    r.headers["Content-Security-Policy"] = DOCS_CSP
    return r


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request):
    if not deps(request).settings.metrics_enabled:
        raise HTTPException(404, "Not found.")
    body, ctype = metrics.render()
    return Response(body, media_type=ctype)


@app.get("/readyz")
async def readyz(request: Request):
    """Readiness for load balancers / Kubernetes: the database must answer."""
    try:
        await asyncio.wait_for(deps(request).store.ping(), 3)
    except Exception as e:
        return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=503)
    return {"ok": True}


@app.get("/api/status")
async def service_status(request: Request):
    """Health of every dependency, for the status banner. Degraded services never block a check:
    the decision falls back (pattern reading, cached lists) or fails closed (HOLD) and says so."""
    await principal_from(request)
    d = deps(request)
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(d.store.ping(), 3)
        db = {"state": "ok", "ms": int((time.perf_counter() - t0) * 1000)}
    except Exception as e:
        db = {"state": "error", "reason": f"database unreachable ({type(e).__name__})"}
    ai = d.gemini.status() if d.gemini else {"state": "off", "reason": "No Gemini credentials configured",
                                             "fix": None, "mode": d.settings.gemini_mode}
    lists = d.sources.sanctions.status()
    now = time.time()
    recent = {k: {**v, "ago_s": int(now - v["at"])} for k, v in trace.LAST.items() if now - v["at"] < 3600}
    degraded = [n for n, st in (("database", db["state"]), ("ai", ai["state"])) if st in ("error", "paused")]
    degraded += [f"sanctions:{x['key']}" for x in lists if not x.get("loaded")]
    degraded += [f"source:{k}" for k, v in recent.items() if v["status"] == "error" and k != "gemini"]
    return {"ok": not degraded, "degraded": degraded, "database": db, "ai": ai,
            "sanctions": [{k: x.get(k) for k in ("key", "label", "loaded", "refreshing", "error", "published")} for x in lists],
            "sources": recent, "auth": {"mode": d.settings.auth_mode,
                                        "firebase_project": d.settings.firebase_project_id or d.settings.google_cloud_project or None},
            "store": d.settings.store_backend}


@app.post("/api/t/{tid}/ai/retry")
async def ai_retry(tid: str, request: Request):
    """After topping up credits: clear the pause and ask Gemini once whether it answers now."""
    a = await access(request, tid, Perm.tenant_manage)
    d = deps(request)
    if not d.gemini:
        raise HTTPException(409, "AI is not configured on this installation.")
    st = await d.gemini.probe()
    await d.store.audit(tid, a.principal.actor, "ai.retried", f"tenant/{tid}", {"state": st["state"]})
    return st


@app.get("/api/config")
async def public_config(request: Request):
    s = deps(request).settings
    pid = s.firebase_project_id or s.google_cloud_project
    return {"checks": {a.NAME: a.TITLE for a in AGENTS}, "sources": trace.CATALOG,
            "auth_mode": s.auth_mode, "allow_unverified_email": s.allow_unverified_email,
            "firebase": {"apiKey": s.firebase_api_key, "authDomain": s.firebase_auth_domain or (f"{pid}.firebaseapp.com" if pid else ""),
                         "projectId": pid, "appId": s.firebase_app_id}}


@app.get("/api/templates")
async def templates():
    return [{"id": k, "name": v["name"], "summary": v["summary"]} for k, v in TEMPLATES.items()]


@app.get("/api/csv-templates/{kind}", response_class=PlainTextResponse)
async def csv_template(kind: str):
    if kind not in ("vendors", "payments"):
        raise HTTPException(404, "Unknown template")
    return PlainTextResponse(onboarding.template_csv(kind), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{kind}-template.csv"'})


# =================================================================== user ====
@app.get("/api/me")
async def me(request: Request):
    p = await principal_from(request)
    return {"uid": p.uid, "email": p.email, "workspaces": await deps(request).store.tenants_for(p.uid)}


class NewWorkspace(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    company_domains: list[str] = Field(default_factory=list, max_length=20)
    base_currency: str = Field("USD", pattern=r"^[A-Z]{3}$")
    timezone: str = "Asia/Singapore"
    region: str = "asia-southeast1"
    template: str = "balanced"


@app.post("/api/workspaces", status_code=201)
async def create_workspace(request: Request, body: NewWorkspace):
    d, p = deps(request), await principal_from(request)
    if p.kind != "user":
        raise HTTPException(403, "API keys can't create workspaces.")
    if body.template not in TEMPLATES:
        raise HTTPException(422, f"template must be one of {list(TEMPLATES)}")
    if len(await d.store.tenants_for(p.uid)) >= 5:
        raise HTTPException(429, "You can own at most 5 workspaces.")
    tid = "t_" + uuid.uuid4().hex[:10]
    t = Tenant(id=tid, name=body.name, created_by=p.uid, settings=TenantSettings(
        company_domains=[x.lower().strip() for x in body.company_domains if x.strip()],
        base_currency=body.base_currency, timezone=body.timezone, region=body.region))
    await d.store.create_tenant(t, Member(uid=p.uid, email=p.email, role=Role.owner, added_by=p.uid))
    # Version 1 comes from a vetted template, so it activates without a second approver.
    pol = policy_from_template(tid, body.template, await d.store.next_policy_version(tid), p.uid)
    pol.status, pol.approved_by, pol.approved_at, pol.change_note = "active", "template", utcnow(), "Initial template"
    await d.store.put_policy(pol)
    t.active_policy_version = pol.version
    await d.store.save_tenant(t)
    await d.store.audit(tid, p.uid, "workspace.created", f"tenant/{tid}", {"name": t.name, "template": body.template})
    return {"tenant_id": tid}


def _mask_email(e: str) -> str:
    name, _, domain = e.partition("@")
    return f"{name[:2]}{'•' * max(1, len(name) - 2)}@{domain}"


@app.get("/api/invites/preview")
async def preview_invite(request: Request, token: str):
    """What an invite link is for, shown on the sign-in page before signing in. The token itself is
    the secret; the invited address is partly masked."""
    d = deps(request)
    inv = await d.store.get_invite(hash_secret(token))
    if not inv or inv.get("accepted_by") or datetime.fromisoformat(str(inv["expires_at"])) < utcnow():
        return {"valid": False}
    t = await d.store.get_tenant(inv["tenant_id"])
    return {"valid": True, "workspace": t.name if t else "a workspace", "role": inv["role"],
            "email": _mask_email(inv["email"]), "expires_at": inv["expires_at"]}


@app.post("/api/invites/accept")
async def accept_invite(request: Request, token: str = Body(..., embed=True)):
    d, p = deps(request), await principal_from(request)
    result, inv = await d.store.accept_invite(hash_secret(token), p.uid, p.email)
    if result == "invalid":
        raise HTTPException(404, "This invite is invalid, used or expired. Ask for a new one.")
    if result == "wrong_email":
        raise HTTPException(403, f"This invite is for {_mask_email(inv['email'])}. Sign out and sign in with that address.")
    t = await d.store.get_tenant(inv["tenant_id"])
    await d.store.put_member(t.id, Member(uid=p.uid, email=p.email, role=Role(inv["role"]),
                                          added_by=inv["created_by"]), t.name)
    await d.store.audit(t.id, p.uid, "member.joined", f"member/{p.uid}", {"role": inv["role"]})
    return {"tenant_id": t.id}


# ======================================================== workspace setup ====
@app.get("/api/t/{tid}")
async def workspace(tid: str, request: Request):
    a = await access(request, tid, Perm.case_read)
    t = await deps(request).store.get_tenant(tid)
    return {**t.model_dump(mode="json"), "my_role": a.role.value,
            "my_permissions": sorted(x.value for x in Perm if a.can(x))}


@app.patch("/api/t/{tid}/settings")
async def update_settings(tid: str, request: Request, body: TenantSettings):
    a = await access(request, tid, Perm.tenant_manage)
    d = deps(request)
    t = await d.store.get_tenant(tid)
    before = t.settings.model_dump(mode="json")
    t.settings = body
    await d.store.save_tenant(t)
    await d.store.audit(tid, a.principal.actor, "settings.updated", f"tenant/{tid}",
                        {"before": before, "after": body.model_dump(mode="json")})
    return t.settings


@app.get("/api/t/{tid}/onboarding")
async def onboarding_status(tid: str, request: Request):
    await access(request, tid, Perm.case_read)
    return await onboarding.checklist(deps(request).store, tid)


@app.post("/api/t/{tid}/import/{kind}")
async def import_csv(tid: str, kind: str, request: Request, file: UploadFile = File(...), dry_run: bool = Form(False)):
    a = await access(request, tid, Perm.data_import)
    d = deps(request)
    raw = await read_limited(file, 20 * 1024 * 1024)
    text = raw.decode("utf-8-sig", errors="replace")
    if kind == "vendors":
        items, errors = onboarding.parse_vendors(text)
    elif kind == "payments":
        items, errors = onboarding.parse_payments(text, {v.id for v in await d.store.list_vendors(tid)})
    else:
        raise HTTPException(404, "Import vendors or payments.")
    if not errors and not dry_run:
        await (d.store.import_vendors if kind == "vendors" else d.store.import_payments)(tid, items)
        await d.store.audit(tid, a.principal.actor, f"import.{kind}", f"tenant/{tid}",
                            {"rows": len(items), "file_sha256": hash_secret(text)})
    return {"valid": len(items), "errors": errors[:200], "error_count": len(errors),
            "imported": 0 if (errors or dry_run) else len(items),
            "note": "Nothing was imported: fix the errors and upload again." if errors else ""}


@app.get("/api/t/{tid}/sample-data/preview")
async def preview_sample(tid: str, request: Request):
    """Show exactly what "Use sample data" would add, with integrity checks, before anything is saved."""
    await access(request, tid, Perm.data_import)
    d = deps(request)
    if not d.settings.demo_enabled:
        raise HTTPException(404, "Sample data is disabled on this installation.")
    vendors, payments = onboarding.sample_data(d.settings.seed_file)
    existing = {v.id for v in await d.store.list_vendors(tid)}
    return {**onboarding.preview(vendors, payments), "already_loaded": sum(v.id in existing for v in vendors)}


@app.post("/api/t/{tid}/sample-data")
async def import_sample(tid: str, request: Request):
    a = await access(request, tid, Perm.data_import)
    d = deps(request)
    if not d.settings.demo_enabled:
        raise HTTPException(404, "Sample data is disabled on this installation.")
    vendors, payments = onboarding.sample_data(d.settings.seed_file)
    if not onboarding.preview(vendors, payments)["valid"]:
        raise HTTPException(422, "The sample data failed its integrity checks and was not imported.")
    await d.store.import_vendors(tid, vendors)
    await d.store.import_payments(tid, payments)
    # Read the sample back from the database, so the confirmation reflects stored data, not intentions.
    sample_vendors, sample_payments = {v.id for v in vendors}, {p.id for p in payments}
    stored = [v for v in await d.store.list_vendors(tid) if v.id in sample_vendors]
    stored_payments = 0
    for vid in sample_vendors:
        stored_payments += sum(p.id in sample_payments for p in await d.store.payments_for_vendor(tid, vid))
    await d.store.audit(tid, a.principal.actor, "import.sample", f"tenant/{tid}",
                        {"vendors": len(vendors), "payments": len(payments)})
    return {"vendors": len(vendors), "payments": len(payments), "stored_vendors": len(stored),
            "stored_payments": stored_payments, "store": d.settings.store_backend}


@app.get("/api/t/{tid}/vendors")
async def vendors(tid: str, request: Request):
    await access(request, tid, Perm.case_read)
    return [v.model_dump(mode="json") for v in await deps(request).store.list_vendors(tid)]


# ---------------------------------------------------------------- members ----
@app.get("/api/t/{tid}/members")
async def members(tid: str, request: Request):
    await access(request, tid, Perm.case_read)
    d = deps(request)
    pending = [{"email": i["email"], "role": i["role"], "expires_at": i["expires_at"]}
               for i in await d.store.list_invites(tid) if not i.get("accepted_by")]
    return {"members": [m.model_dump(mode="json") for m in await d.store.list_members(tid)], "invites": pending}


class InviteIn(BaseModel):
    email: EmailStr
    role: Role


@app.post("/api/t/{tid}/invites", status_code=201)
async def invite(tid: str, request: Request, body: InviteIn):
    a = await access(request, tid, Perm.members_manage)
    d = deps(request)
    if body.role == Role.owner and a.role != Role.owner:
        raise HTTPException(403, "Only an owner can invite another owner.")
    token, h = new_secret("inv")
    await d.store.put_invite(tid, Invite(token_hash=h, email=body.email.lower(), role=body.role,
                                         created_by=a.principal.uid,
                                         expires_at=utcnow() + timedelta(days=d.settings.invite_days)))
    await d.store.audit(tid, a.principal.actor, "member.invited", f"invite/{body.email}", {"role": body.role.value})
    base = str(request.base_url).rstrip("/")
    return {"invite_link": f"{base}/#invite={token}", "expires_in_days": d.settings.invite_days,
            "note": "Send this link to the person. It works once, for that email address only."}


@app.delete("/api/t/{tid}/members/{uid}")
async def remove_member(tid: str, uid: str, request: Request):
    a = await access(request, tid, Perm.members_manage)
    d = deps(request)
    owners = [m for m in await d.store.list_members(tid) if m.role == Role.owner]
    if len(owners) == 1 and owners[0].uid == uid:
        raise HTTPException(409, "A workspace needs at least one owner.")
    await d.store.remove_member(tid, uid)
    await d.store.audit(tid, a.principal.actor, "member.removed", f"member/{uid}", {})
    return {"ok": True}


# --------------------------------------------------------------- api keys ----
@app.get("/api/t/{tid}/api-keys")
async def api_keys(tid: str, request: Request):
    await access(request, tid, Perm.apikeys_manage)
    return await deps(request).store.list_api_keys(tid)


@app.post("/api/t/{tid}/api-keys", status_code=201)
async def create_api_key(tid: str, request: Request, name: str = Body(..., embed=True, min_length=2, max_length=60),
                         role: Role = Body(Role.clerk, embed=True)):
    a = await access(request, tid, Perm.apikeys_manage)
    if role not in (Role.clerk, Role.analyst, Role.auditor, Role.integration):
        raise HTTPException(422, "API keys can submit and read cases and sync data (clerk, analyst, auditor or "
                                 "integration), never release payments.")
    prefix = uuid.uuid4().hex[:12]
    raw, h = new_secret(f"vbw_{prefix}")
    key = ApiKey(id=prefix, name=name, prefix=prefix, key_hash=hash_secret(raw), role=role, created_by=a.principal.uid)
    await deps(request).store.put_api_key(tid, key.model_dump(mode="json"))
    await deps(request).store.audit(tid, a.principal.actor, "apikey.created", f"apikey/{prefix}", {"name": name})
    return {"api_key": raw, "note": "Copy it now. It is shown only once and stored only as a hash."}


@app.delete("/api/t/{tid}/api-keys/{prefix}")
async def revoke_api_key(tid: str, prefix: str, request: Request):
    a = await access(request, tid, Perm.apikeys_manage)
    d = deps(request)
    k = await d.store.get_api_key(prefix)
    if not k or k["tenant_id"] != tid:
        raise HTTPException(404, "Key not found.")
    await d.store.put_api_key(tid, {**k, "revoked": True})
    await d.store.audit(tid, a.principal.actor, "apikey.revoked", f"apikey/{prefix}", {})
    return {"ok": True}


# ===================================================== policy lifecycle ====
@app.get("/api/t/{tid}/policies")
async def policies(tid: str, request: Request):
    await access(request, tid, Perm.case_read)
    return [{**p.model_dump(mode="json", by_alias=True), "hash": p.content_hash()}
            for p in await deps(request).store.list_policies(tid)]


class PolicyDraft(BaseModel):
    name: str
    settings: PolicySettings
    rules: list[Rule]
    change_note: str = Field(min_length=5)


@app.post("/api/t/{tid}/policies", status_code=201)
async def create_draft(tid: str, request: Request, body: PolicyDraft):
    a = await access(request, tid, Perm.policy_edit)
    d = deps(request)
    active = await d.store.active_policy(tid)
    p = Policy(tenant_id=tid, version=await d.store.next_policy_version(tid), name=body.name,
               based_on=f"v{active.version}" if active else None, settings=body.settings, rules=body.rules,
               created_by=a.principal.uid, change_note=body.change_note)
    await d.store.put_policy(p)
    await d.store.audit(tid, a.principal.actor, "policy.drafted", f"policy/{p.version}", {"hash": p.content_hash()})
    return {"version": p.version, "hash": p.content_hash()}


async def _decided_cases(d: Deps, tid: str) -> list[dict]:
    out = []
    for c in await d.store.list_cases(tid, limit=500):
        if c.record:
            out.append({"id": c.id, "findings": c.record.raw_findings, "facts": c.record.facts,
                        "label": c.workflow.label})
    return out


@app.post("/api/t/{tid}/policies/{version}/simulate")
async def simulate_policy(tid: str, version: int, request: Request):
    await access(request, tid, Perm.policy_edit)
    d = deps(request)
    p = await d.store.get_policy(tid, version)
    if not p:
        raise HTTPException(404, "Policy not found.")
    return simulate(p, await d.store.active_policy(tid), await _decided_cases(d, tid))


@app.post("/api/t/{tid}/policies/{version}/submit")
async def submit_policy(tid: str, version: int, request: Request):
    a = await access(request, tid, Perm.policy_edit)
    d = deps(request)
    p = await d.store.get_policy(tid, version)
    if not p or p.status != "draft":
        raise HTTPException(409, "Only drafts can be submitted.")
    p.status, p.submitted_by = "pending_approval", a.principal.uid
    await d.store.put_policy(p)
    await d.store.audit(tid, a.principal.actor, "policy.submitted", f"policy/{version}", {"hash": p.content_hash()})
    return {"status": p.status}


@app.post("/api/t/{tid}/policies/{version}/{verdict}")
async def review_policy(tid: str, version: int, verdict: str, request: Request,
                        reason: str = Body("", embed=True)):
    if verdict not in ("approve", "reject"):
        raise HTTPException(404, "Use approve or reject.")
    a = await access(request, tid, Perm.policy_approve)
    d = deps(request)
    p = await d.store.get_policy(tid, version)
    if not p or p.status != "pending_approval":
        raise HTTPException(409, "Only submitted policies can be reviewed.")
    if a.principal.uid in (p.created_by, p.submitted_by):
        raise HTTPException(403, "Someone other than the author must review a policy change.")
    if verdict == "reject":
        p.status = "rejected"
        await d.store.put_policy(p)
        await d.store.audit(tid, a.principal.actor, "policy.rejected", f"policy/{version}", {"reason": reason})
        return {"status": p.status}
    t = await d.store.get_tenant(tid)
    replaces = t.active_policy_version
    if replaces and (old := await d.store.get_policy(tid, replaces)):
        old.status = "retired"
        await d.store.put_policy(old)
    p.status, p.approved_by, p.approved_at = "active", a.principal.uid, utcnow()
    await d.store.put_policy(p)
    t.active_policy_version = p.version
    await d.store.save_tenant(t)
    await d.store.audit(tid, a.principal.actor, "policy.activated", f"policy/{version}",
                        {"hash": p.content_hash(), "replaces": replaces})
    return {"status": "active"}


# ================================================================= cases ====
async def _new_case(d: Deps, a: Access, files: list[tuple[str, str | None, bytes]], scenario: str | None = None,
                    idem: str | None = None, external_ref: str | None = None) -> tuple[Case, bool]:
    s, tid = d.settings, a.tenant_id
    if idem is not None and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idem):
        raise HTTPException(422, "Idempotency-Key must be 1-128 letters, digits or . _ : -")
    if external_ref is not None and len(external_ref) > 128:
        raise HTTPException(422, "external_ref is too long.")
    if idem and (existing := await d.store.find_case_by_idempotency(tid, idem)):
        return existing, False
    ratelimit.charge(tid, s.rate_limit_per_minute)
    t = await d.store.get_tenant(tid)
    if not t.active_policy_version:
        raise HTTPException(409, "Choose a policy before running checks.")
    if not files:
        raise HTTPException(400, "Add at least one file: an email (.eml), voice note, invoice or text.")
    if len(files) > s.max_files:
        raise HTTPException(400, f"Upload at most {s.max_files} files.")
    case = Case(id="c_" + uuid.uuid4().hex[:12], tenant_id=tid, created_by=a.principal.uid, scenario=scenario,
                idempotency_key=idem, external_ref=external_ref)
    label = Labeler()
    for name, mime, data in files:
        if len(data) > s.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"{name} is larger than {s.max_upload_mb} MB.")
        try:
            # Off the event loop: PDF extraction can take seconds and would stall every other request.
            for doc, raw in await asyncio.to_thread(parse_upload, name, mime, data, label):
                doc.storage_uri = await d.store.put_blob(f"{tid}/cases/{case.id}/{doc.label}", raw, doc.mime_type)
                case.sources.append(doc)
        except UnsupportedFile as e:
            raise HTTPException(415, str(e)) from None
    await d.store.create_case(case)
    await d.store.audit(tid, a.principal.actor, "case.created", f"case/{case.id}",
                        {"inputs": {x.label: x.sha256 for x in case.sources}, "external_ref": external_ref})
    return case, True


async def _read_uploads(s: Settings, files: list[UploadFile]) -> list[tuple[str, str | None, bytes]]:
    """Enforce the file-count and per-file size limits while reading, before anything is stored."""
    if len(files) > s.max_files:
        raise HTTPException(400, f"Upload at most {s.max_files} files.")
    return [(f.filename or "upload", f.content_type, await read_limited(f, s.max_upload_mb * 1024 * 1024)) for f in files]


@app.post("/api/t/{tid}/cases", status_code=201)
async def create_case(tid: str, request: Request, files: list[UploadFile] = File(...),
                      external_ref: str | None = Form(None)):
    a = await access(request, tid, Perm.case_create)
    payload = await _read_uploads(deps(request).settings, files)
    case, _ = await _new_case(deps(request), a, payload, idem=request.headers.get("idempotency-key"),
                              external_ref=external_ref)
    return {"case_id": case.id, "inputs": [x.label for x in case.sources]}


SYNC_WAIT_S = 120      # how long POST /checks waits for a recommendation before returning the running case


@app.post("/api/t/{tid}/checks")
async def check_sync(tid: str, request: Request, files: list[UploadFile] = File(...),
                     external_ref: str | None = Form(None)):
    """For integrations: submit and wait for the recommendation (typically 10-30 s)."""
    a = await access(request, tid, Perm.case_create)
    d = deps(request)
    payload = await _read_uploads(d.settings, files)
    case, new = await _new_case(d, a, payload, idem=request.headers.get("idempotency-key"), external_ref=external_ref)
    if new and (task := await start_case(d, case.tenant_id, case.id, None)):
        await asyncio.wait({task}, timeout=SYNC_WAIT_S)       # wait, never cancel: it finishes in the background
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SYNC_WAIT_S
    while case.status not in (CaseStatus.completed, CaseStatus.failed) and loop.time() < deadline:
        await asyncio.sleep(0.4)
        case = await d.store.get_case(tid, case.id)
    return _case_out(case)        # status "running" if still in progress: poll GET /cases/{id}


def _case_out(c: Case) -> dict:
    # internal storage paths stay on the server (inputs and call recordings are served by their own endpoints)
    return c.model_dump(mode="json", exclude={"sources": {"__all__": {"storage_uri"}},
                                              "workflow": {"ai_calls": {"__all__": {"recording_uri"}}}})


@app.get("/api/t/{tid}/cases")
async def list_cases(tid: str, request: Request, limit: int = 50):
    await access(request, tid, Perm.case_read)
    return [{"id": c.id, "created_at": c.created_at, "status": c.status, "external_ref": c.external_ref,
             "decision": c.verdict.decision if c.verdict else None, "score": c.verdict.risk_score if c.verdict else None,
             "state": c.workflow.state, "amount": c.request.amount if c.request else None,
             "currency": c.request.currency if c.request else None,
             "payee": c.request.beneficiary_name if c.request else None}
            for c in await deps(request).store.list_cases(tid, min(limit, 200))]


@app.get("/api/t/{tid}/cases/{cid}")
async def get_case(tid: str, cid: str, request: Request):
    await access(request, tid, Perm.case_read)
    c = await deps(request).store.get_case(tid, cid)
    if not c:
        raise HTTPException(404, "Case not found.")
    return _case_out(c)


@app.get("/api/t/{tid}/cases/{cid}/sources/{label}")
async def case_source(tid: str, cid: str, label: str, request: Request):
    """The original voice note, PDF or image, so reviewers can check the evidence themselves.
    Emails and text are already in the case JSON as parsed text and are not served raw."""
    await access(request, tid, Perm.case_read)
    d = deps(request)
    c = await d.store.get_case(tid, cid)
    doc = next((s for s in c.sources if s.label == label), None) if c else None
    if not doc or not doc.storage_uri:
        raise HTTPException(404, "Input not found.")
    if doc.mime_type not in MEDIA | DOCS:
        raise HTTPException(415, "Only voice notes, videos, PDFs and images can be opened here.")
    data = await d.store.get_blob(doc.storage_uri)
    safe_name = re.sub(r"[^\w.-]", "_", doc.filename)[-120:]
    return Response(data, media_type=doc.mime_type, headers={
        "Content-Disposition": f'inline; filename="{safe_name}"',
        "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; media-src 'self'",
        "Accept-Ranges": "none"})


def _sse(ev: Event) -> str:
    return f"id: {ev.seq}\nevent: {ev.type}\ndata: {ev.model_dump_json()}\n\n"


@app.get("/api/t/{tid}/cases/{cid}/stream")
async def stream(tid: str, cid: str, request: Request, replay: int = 0):
    await access(request, tid, Perm.case_read)
    d = deps(request)
    case = await d.store.get_case(tid, cid)
    if not case:
        raise HTTPException(404, "Case not found.")
    last = int(request.headers.get("last-event-id") or 0)

    async def gen():
        yield "retry: 2000\n\n"
        if replay and case.scenario and d.settings.demo_enabled and \
                await d.store.try_claim(tid, cid, d.settings.lease_seconds):
            async for chunk in _replay(d, case):
                yield chunk
            return
        q: asyncio.Queue = asyncio.Queue()
        if task := await start_case(d, case.tenant_id, case.id, q):
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(ev)
                if ev.type in TERMINAL:
                    break
            await task
            return
        seq, quiet = last, 0.0
        while True:
            evs = await d.store.events_after(tid, cid, seq)
            for ev in evs:
                seq = ev.seq
                yield _sse(ev)
                if ev.type in TERMINAL:
                    return
            if await request.is_disconnected():
                return
            quiet = 0.0 if evs else quiet + 0.4
            if quiet and quiet % 2.0 < 0.4:
                cur = await d.store.get_case(tid, cid)
                if cur is None or cur.status in (CaseStatus.completed, CaseStatus.failed):
                    return
                if cur.status == CaseStatus.running and cur.lease_until and cur.lease_until.timestamp() < time.time():
                    yield _sse(Event(seq=seq, type="case.stalled", data={}))
                    return
            if quiet and quiet % 15.0 < 0.4:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


async def _replay(d: Deps, case: Case):
    """Demo fail-safe: replays a recorded run of a bundled scenario with its original timing."""
    p = d.settings.demo_dir / "replays" / f"{case.scenario}.json"
    if not p.exists():
        yield _sse(Event(seq=1, type="case.failed", data={"error": "No recording for this scenario"}))
        return
    rec = json.loads(p.read_text())
    snap = Case(**{**rec["case"], "id": case.id, "tenant_id": case.tenant_id, "created_by": case.created_by,
                   "sources": [s.model_dump() for s in case.sources], "status": "completed"})
    await d.store.save_case(snap)
    prev = 0.0
    for i, e in enumerate(rec["events"], 1):
        await asyncio.sleep(min(1.2, max(0.0, e["t"] - prev)))
        prev = e["t"]
        ev = Event(seq=i, type=e["type"], agent=e.get("agent"), data=e.get("data", {}))
        await d.store.append_event(case.tenant_id, case.id, ev)
        yield _sse(ev)


@app.get("/api/t/{tid}/cases/{cid}/export")
async def export_case(tid: str, cid: str, request: Request):
    await access(request, tid, Perm.case_read)
    d = deps(request)
    c = await d.store.get_case(tid, cid)
    if not c or c.status != CaseStatus.completed:
        raise HTTPException(409, "Case is not completed.")
    evs = await d.store.events_after(tid, cid, 0)
    t0 = evs[0].ts if evs else None
    return {"case": c.model_dump(mode="json", exclude={"sources", "tenant_id", "workflow"}),
            "events": [{"t": (e.ts - t0).total_seconds(), "type": e.type, "agent": e.agent, "data": e.data}
                       for e in evs]}


@app.get("/api/t/{tid}/cases/{cid}/report.pdf")
async def report_pdf(tid: str, cid: str, request: Request):
    a = await access(request, tid, Perm.case_read)
    d = deps(request)
    c = await d.store.get_case(tid, cid)
    if not c or not c.verdict:
        raise HTTPException(409, "The investigation has not finished yet.")
    events = await d.store.events_after(tid, cid, 0)
    await d.store.audit(tid, a.principal.actor, "case.report_downloaded", f"case/{cid}", {})
    try:
        pdf = await asyncio.to_thread(build_pdf, c, events)
    except Exception:
        log.exception("PDF build failed; serving HTML")
        return HTMLResponse(build_html(c))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="case-{cid}.pdf"'})


class CallbackIn(BaseModel):
    contact: str = Field(min_length=2, max_length=120)
    number: str = Field(min_length=5, max_length=40)
    number_source: str = Field(pattern="^(vendor_master|company_directory|other)$")
    outcome: str = Field(pattern="^(confirmed|denied|no_answer)$")
    notes: str = Field("", max_length=2000)


@app.post("/api/t/{tid}/cases/{cid}/callbacks", status_code=201)
async def add_callback(tid: str, cid: str, request: Request, body: CallbackIn):
    a = await access(request, tid, Perm.callback_record)
    d = deps(request)
    res = await d.store.update_case(tid, cid, lambda c: workflow.record_callback(c, a, body.model_dump()))
    if res is None:
        raise HTTPException(404, "Case not found.")
    case, cb = res
    await d.store.audit(tid, a.principal.actor, "case.callback_recorded", f"case/{cid}",
                        {"outcome": cb.outcome, "number_source": cb.number_source})
    if cb.outcome == "denied":
        await d.store.audit(tid, "system", "case.fraud_signal", f"case/{cid}", {"reason": "vendor denied request"})
    return {"state": case.workflow.state, "callback_satisfied": workflow.callback_satisfied(case)}


class ActionIn(BaseModel):
    action: str = Field(pattern="^(release|hold|reject|clear_compliance)$")
    reason: str = Field("", max_length=2000)


@app.post("/api/t/{tid}/cases/{cid}/actions")
async def case_action(tid: str, cid: str, request: Request, body: ActionIn):
    a = await access(request, tid, Perm.case_read)  # finer checks inside workflow.act
    d = deps(request)
    res = await d.store.update_case(tid, cid, lambda c: workflow.act(c, a, body.action, body.reason))
    if res is None:
        raise HTTPException(404, "Case not found.")
    case, state = res
    await d.store.audit(tid, a.principal.actor, f"case.{body.action}", f"case/{cid}",
                        {"state": state, "reason": body.reason,
                         "override": bool(case.verdict and case.verdict.decision.value == "HOLD" and body.action == "release")})
    await webhooks.emit(d.store, tid, "case.decided", case)
    return {"state": state, "workflow": case.workflow.model_dump(mode="json")}


app.include_router(integrations.router)
app.include_router(voice.router)


# ================================================================== audit ====
@app.get("/api/t/{tid}/audit")
async def audit_log(tid: str, request: Request, limit: int = 200):
    await access(request, tid, Perm.audit_read)
    entries = await deps(request).store.list_audit(tid)
    return [e.model_dump(mode="json") for e in entries[-min(limit, 1000):]][::-1]


@app.get("/api/t/{tid}/audit/verify")
async def audit_verify(tid: str, request: Request):
    await access(request, tid, Perm.audit_read)
    return verify_chain(await deps(request).store.list_audit(tid))


# ================================================================== demo =====
def _scenarios(s: Settings) -> list[dict]:
    p = s.demo_dir / "scenarios.json"
    return json.loads(p.read_text()) if p.exists() else []


@app.get("/api/demo")
async def list_demo(request: Request):
    s = deps(request).settings
    if not s.demo_enabled:
        return []
    return [{**{k: sc[k] for k in ("id", "title", "description")},
             "has_replay": (s.demo_dir / "replays" / f"{sc['id']}.json").exists()} for sc in _scenarios(s)]


@app.post("/api/t/{tid}/demo/{scenario_id}", status_code=201)
async def run_demo(tid: str, scenario_id: str, request: Request):
    a = await access(request, tid, Perm.case_create)
    d = deps(request)
    if not d.settings.demo_enabled:
        raise HTTPException(404, "Not found.")
    sc = next((x for x in _scenarios(d.settings) if x["id"] == scenario_id), None)
    if not sc:
        raise HTTPException(404, "Unknown scenario.")
    files = [(p.name, None, p.read_bytes()) for f in sc["files"] if (p := d.settings.demo_dir / "files" / f).exists()]
    case, _ = await _new_case(d, a, files, scenario_id)
    return {"case_id": case.id, "inputs": [x.label for x in case.sources]}


_static = get_settings().static_dir
if _static.exists():
    app.mount("/assets", StaticFiles(directory=_static), name="assets")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(_static / "index.html")
