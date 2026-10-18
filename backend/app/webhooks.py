"""Outbound webhooks: tell the customer's ERP (e.g. SAP via BTP Integration Suite) when a
recommendation is ready and when a person releases, holds or rejects a payment.

Delivery is an outbox: events are written to the store in the same request that changes the
case, and the worker delivers them with retries and exponential backoff, so a slow or offline
ERP never loses an event and never slows an investigation down.

Each request is signed like Stripe's: header `VBW-Signature: t=<unix>,v1=<hex>` where
v1 = HMAC-SHA256(secret, f"{t}.{raw_body}"). Receivers must check the signature and reject
timestamps older than 5 minutes (replay protection)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from .metrics import WEBHOOKS
from .models import Case

log = logging.getLogger(__name__)

EVENTS = ("case.recommended", "case.decided", "case.failed")
MAX_ATTEMPTS = 12      # backoff 10 s, 20 s, 40 s ... capped at 1 h: about 4.5 hours before giving up


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sign(secret: str, body: bytes, t: int | None = None) -> str:
    t = int(time.time()) if t is None else t
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify(secret: str, body: bytes, header: str, tolerance_s: int = 300) -> bool:
    """Reference implementation for receivers (also used by the SAP simulator and the tests)."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        t = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - t) > tolerance_s:
        return False
    return hmac.compare_digest(sign(secret, body, t), header)


def check_url(url: str, allow_insecure: bool) -> str | None:
    """Why this URL may not receive webhooks, or None. Blocks plain http and private/internal
    addresses (SSRF) unless the installation explicitly allows them (local development)."""
    u = urlparse(url)
    if u.scheme not in ("https", "http") or not u.hostname:
        return "Use an https:// URL."
    if allow_insecure:
        return None
    if u.scheme != "https":
        return "Use an https:// URL."
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return f"{u.hostname} does not resolve."
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return "That address is on a private network. Expose your receiver (e.g. SAP BTP) on a public https URL."
    return None


def case_payload(case: Case) -> dict:
    """What the ERP needs to act, without the evidence itself (fetch the case for that)."""
    v, wf = case.verdict, case.workflow
    return {"case_id": case.id, "external_ref": case.external_ref, "status": case.status.value,
            "decision": v.decision.value if v else None, "risk_score": v.risk_score if v else None,
            "workflow_state": wf.state, "requirements": wf.requirements,
            "release_allowed": wf.state == "released",
            "reasons": (v.top_reasons if v else [])[:5], "summary": v.summary if v else None,
            "decided_by": [{"email": a.email, "action": a.action, "at": a.at.isoformat()} for a in wf.approvals]}


def new_delivery(tid: str, webhook_id: str, event: str, data: dict) -> dict:
    """An outbox record for one event to one webhook, ready to be claimed and sent."""
    now = _now().isoformat()
    return {"id": "dlv_" + uuid.uuid4().hex[:16], "webhook_id": webhook_id, "event": event,
            "payload": {"id": "evt_" + uuid.uuid4().hex[:16], "type": event, "created_at": now, "tenant_id": tid, "data": data},
            "status": "pending", "attempts": 0, "next_attempt_at": now, "last_error": None, "created_at": now,
            "delivered_at": None, "response_status": None}


async def emit(store, tid: str, event: str, case: Case) -> int:
    """Queue `event` for every active webhook subscribed to it. Never raises: a webhook
    problem must not fail a case."""
    try:
        hooks = [h for h in await store.list_webhooks(tid) if h.get("active", True) and event in h.get("events", EVENTS)]
        for h in hooks:
            await store.enqueue_delivery(tid, new_delivery(tid, h["id"], event, case_payload(case)))
        return len(hooks)
    except Exception:
        log.exception("could not queue webhook %s for %s", event, case.id)
        return 0


async def deliver_one(store, http: httpx.AsyncClient, tid: str, d: dict, hook: dict | None, allow_insecure: bool) -> None:
    """Send one claimed delivery and record the outcome (retry with backoff, or give up)."""
    if not hook or not hook.get("active", True):
        await store.update_delivery(tid, d["id"], {"status": "failed", "last_error": "webhook removed or disabled"})
        return
    # Check the address again at send time: a hostname that resolved publicly when registered may
    # since point at an internal address (DNS rebinding). Redirects are never followed.
    if why := await asyncio.to_thread(check_url, hook["url"], allow_insecure):
        WEBHOOKS.labels("blocked").inc()
        await store.update_delivery(tid, d["id"], {"status": "failed", "last_error": f"blocked: {why}"})
        return
    body = json.dumps(d["payload"], separators=(",", ":")).encode()
    attempts = int(d.get("attempts") or 0) + 1
    status, error = None, None
    try:
        r = await http.post(hook["url"], content=body, timeout=10, follow_redirects=False, headers={
            "Content-Type": "application/json", "User-Agent": "verify-before-wire-webhooks/1.0",
            "VBW-Event": d["event"], "VBW-Delivery": d["id"], "VBW-Signature": sign(hook["secret"], body)})
        status = r.status_code
        if 200 <= status < 300:
            WEBHOOKS.labels("delivered").inc()
            await store.update_delivery(tid, d["id"], {"status": "delivered", "attempts": attempts,
                                                       "response_status": status, "delivered_at": _now().isoformat(),
                                                       "last_error": None})
            return
        error = f"HTTP {status}"
    except httpx.HTTPError as e:
        error = f"{type(e).__name__}: {e}"[:300]
    gave_up = attempts >= MAX_ATTEMPTS
    WEBHOOKS.labels("gave_up" if gave_up else "retry").inc()
    backoff = min(3600, 10 * 2 ** (attempts - 1))
    await store.update_delivery(tid, d["id"], {
        "status": "failed" if gave_up else "pending", "attempts": attempts, "response_status": status,
        "last_error": error, "next_attempt_at": (_now() + timedelta(seconds=backoff)).isoformat()})
    log.warning("webhook %s to %s failed (attempt %d): %s", d["id"], hook["url"], attempts, error)


async def deliver_due(store, http: httpx.AsyncClient, limit: int = 50, allow_insecure: bool = False) -> int:
    """Send every delivery that is due. Each one is claimed atomically first, so any number of
    worker processes can run this without sending a delivery twice. Returns how many were sent."""
    sent = 0
    hooks: dict[tuple[str, str], dict | None] = {}
    for d in await store.due_deliveries(limit):
        tid = d["tenant_id"]
        if not await store.claim_delivery(tid, d["id"]):
            continue                                   # another process has it
        key = (tid, d["webhook_id"])
        if key not in hooks:
            hooks[key] = next((h for h in await store.list_webhooks(tid) if h["id"] == d["webhook_id"]), None)
        await deliver_one(store, http, tid, d, hooks[key], allow_insecure)
        sent += 1
    return sent
