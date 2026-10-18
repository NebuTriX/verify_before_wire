"""Tenants (companies), members and roles. Every piece of data belongs to one tenant."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from .models import utcnow


class Role(str, Enum):
    owner = "owner"                # everything, incl. members and billing
    policy_admin = "policy_admin"  # edit, simulate, approve policies
    approver = "approver"          # release / hold / override payments
    analyst = "analyst"            # run cases, record callbacks
    clerk = "clerk"                # submit cases
    auditor = "auditor"            # read-only, incl. audit log
    integration = "integration"    # ERP connector (API keys only): submit payments, sync master data; never decides


class Perm(str, Enum):
    case_create = "case:create"
    case_read = "case:read"
    case_decide = "case:decide"          # release / confirm hold
    callback_record = "callback:record"
    policy_edit = "policy:edit"
    policy_approve = "policy:approve"
    data_import = "data:import"
    members_manage = "members:manage"
    apikeys_manage = "apikeys:manage"
    audit_read = "audit:read"
    tenant_manage = "tenant:manage"


ROLE_PERMS: dict[Role, set[Perm]] = {
    Role.owner: set(Perm),
    Role.policy_admin: {Perm.policy_edit, Perm.policy_approve, Perm.case_read, Perm.audit_read, Perm.data_import},
    Role.approver: {Perm.case_create, Perm.case_read, Perm.case_decide, Perm.callback_record},
    Role.analyst: {Perm.case_create, Perm.case_read, Perm.callback_record},
    Role.clerk: {Perm.case_create, Perm.case_read},
    Role.auditor: {Perm.case_read, Perm.audit_read},
    Role.integration: {Perm.case_create, Perm.case_read, Perm.data_import},
}


class TenantSettings(BaseModel):
    company_domains: list[str] = Field(default_factory=list)
    base_currency: str = "USD"
    timezone: str = "Asia/Singapore"
    region: str = "asia-southeast1"     # data residency label, recorded on every case
    retention_days: int = 2555          # 7 years


class Tenant(BaseModel):
    id: str
    name: str
    settings: TenantSettings
    active_policy_version: int | None = None
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    status: str = "active"


class Member(BaseModel):
    uid: str
    email: str
    role: Role
    added_by: str
    added_at: datetime = Field(default_factory=utcnow)


class Invite(BaseModel):
    token_hash: str
    email: str
    role: Role
    created_by: str
    expires_at: datetime
    accepted_by: str | None = None


class ApiKey(BaseModel):
    id: str
    name: str
    prefix: str
    key_hash: str
    role: Role = Role.clerk            # what the integration may do
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    revoked: bool = False


def new_secret(prefix: str) -> tuple[str, str]:
    """Returns (plaintext shown once, sha256 stored)."""
    raw = f"{prefix}_{secrets.token_urlsafe(32)}"
    return raw, hash_secret(raw)


def hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
