"""Authentication and authorisation.
- People sign in with Firebase Authentication / Identity Platform (email, Google, or the
  company's SAML/OIDC SSO). The browser sends the ID token as a Bearer token.
- Integrations (ERP, treasury system) use per-tenant API keys: Bearer vbw_<prefix>_<secret>.
- AUTH_MODE=dev accepts an X-Dev-User header for local work only; it refuses to run on Cloud Run."""
from __future__ import annotations

import asyncio
import hmac
import os
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

from .tenancy import ROLE_PERMS, Perm, Role, hash_secret


@dataclass
class Principal:
    uid: str
    email: str
    kind: str = "user"                 # user | api_key
    api_key_tenant: str | None = None
    api_key_role: Role | None = None

    @property
    def actor(self) -> str:
        return self.uid if self.kind == "user" else f"apikey:{self.uid}"


@dataclass
class Access:
    principal: Principal
    tenant_id: str
    role: Role

    def can(self, perm: Perm) -> bool:
        return perm in ROLE_PERMS[self.role]


_token_cache: dict[str, tuple[float, Principal]] = {}


def assert_safe_auth_mode(mode: str, allow_dev: bool) -> None:
    if mode == "dev" and os.getenv("K_SERVICE") and not allow_dev:
        raise RuntimeError("AUTH_MODE=dev is not allowed on Cloud Run. Set AUTH_MODE=firebase.")


async def principal_from(request: Request) -> Principal:
    d = request.app.state.deps
    s = d.settings
    authz = request.headers.get("authorization", "")
    token = authz[7:].strip() if authz.lower().startswith("bearer ") else ""

    if token.startswith("vbw_"):
        parts = token.split("_", 2)
        if len(parts) != 3:
            raise HTTPException(401, "Malformed API key.")
        key = await d.store.get_api_key(parts[1])
        if not key or key["revoked"] or not hmac.compare_digest(key["key_hash"], hash_secret(token)):
            raise HTTPException(401, "API key is invalid or revoked.")
        return Principal(uid=key["id"], email=f"apikey:{key['name']}", kind="api_key",
                         api_key_tenant=key["tenant_id"], api_key_role=Role(key["role"]))

    if s.auth_mode == "dev":
        email = request.headers.get("x-dev-user") or (token if "@" in token else "")
        if not email:
            raise HTTPException(401, "Sign in first.")
        return Principal(uid=f"dev:{email.lower()}", email=email.lower())

    if not token:
        raise HTTPException(401, "Sign in first.")
    key = hash_secret(token)
    if (hit := _token_cache.get(key)) and hit[0] > time.time():
        return hit[1]
    from google.auth.transport import requests as ga_requests
    from google.oauth2 import id_token
    try:
        claims = await asyncio.to_thread(id_token.verify_firebase_token, token, ga_requests.Request(),
                                         s.firebase_project_id or s.google_cloud_project)
    except Exception:
        raise HTTPException(401, "Your session expired. Sign in again.") from None
    if not claims.get("email_verified", False) and not s.allow_unverified_email:
        raise HTTPException(403, "Verify your email address first.")
    p = Principal(uid=claims["sub"], email=(claims.get("email") or "").lower())
    _token_cache[key] = (min(claims.get("exp", time.time() + 300), time.time() + 300), p)
    return p


async def access(request: Request, tenant_id: str, perm: Perm) -> Access:
    p = await principal_from(request)
    if p.kind == "api_key":
        if p.api_key_tenant != tenant_id:
            raise HTTPException(404, "Workspace not found.")
        a = Access(p, tenant_id, p.api_key_role)
    else:
        m = await request.app.state.deps.store.get_member(tenant_id, p.uid)
        if not m:
            raise HTTPException(404, "Workspace not found.")  # don't reveal it exists
        a = Access(p, tenant_id, Role(m.role))
    if not a.can(perm):
        raise HTTPException(403, f"Your role ({a.role.value}) can't do this.")
    return a
