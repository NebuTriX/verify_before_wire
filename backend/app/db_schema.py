"""Relational schema for PostgreSQL (and SQLite in tests).

Conventions
- Every tenant-scoped table starts with tenant_id and has it in its primary key or an index,
  so one workspace's data is always one index range away and never mixed with another's.
- Typed columns are the queryable, indexed projection of a record. `doc` holds the complete record
  exactly as the API and the decision engine see it (Pydantic model as JSON), so a record round-trips
  without loss and adding a display-only field never needs a migration.
- JSON is JSONB on PostgreSQL. Timestamps are timezone-aware (timestamptz) and always UTC.
- audit_log is append-only: on PostgreSQL a trigger (see migrations) rejects UPDATE, DELETE and
  TRUNCATE, so even the application's own database user cannot rewrite history. The hash chain
  (app/audit.py) additionally makes any out-of-band edit detectable.

Schema changes go through Alembic (backend/migrations): never edit an applied revision.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, Column, DateTime, ForeignKeyConstraint, Index, Integer, MetaData,
                        Numeric, String, Table, Text, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator

metadata = MetaData(naming_convention={
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})

JSONType = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator):
    """timestamptz that always hands back an aware UTC datetime (SQLite drops the offset)."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _ts(name: str, nullable: bool = False, **kw) -> Column:
    return Column(name, UTCDateTime(), nullable=nullable, **kw)


# ------------------------------------------------------------- workspaces ----
tenants = Table(
    "tenants", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("status", String(20), nullable=False, server_default="active"),
    Column("active_policy_version", Integer),
    Column("created_by", String(320), nullable=False),
    _ts("created_at"),
    Column("doc", JSONType, nullable=False),
)

members = Table(
    "members", metadata,
    Column("tenant_id", String(32), primary_key=True),
    Column("uid", String(320), primary_key=True),
    Column("email", String(320), nullable=False),
    Column("role", String(32), nullable=False),
    Column("added_by", String(320), nullable=False),
    _ts("added_at"),
    ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    Index(None, "uid"),                      # "which workspaces is this person in?"
)

invites = Table(
    "invites", metadata,
    Column("token_hash", String(64), primary_key=True),   # sha256 of the link token, never the token
    Column("tenant_id", String(32), nullable=False, index=True),
    Column("email", String(320), nullable=False),
    Column("role", String(32), nullable=False),
    _ts("expires_at"),
    Column("accepted_by", String(320)),
    Column("doc", JSONType, nullable=False),
    ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
)

api_keys = Table(
    "api_keys", metadata,
    Column("prefix", String(64), primary_key=True),       # public lookup part of the key
    Column("tenant_id", String(32), nullable=False, index=True),
    Column("name", String(120), nullable=False),
    Column("role", String(32), nullable=False),
    Column("revoked", Boolean, nullable=False, server_default="false"),
    Column("doc", JSONType, nullable=False),               # includes key_hash (sha256), never the key
    ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
)

counters = Table(
    "counters", metadata,
    Column("tenant_id", String(32), primary_key=True),
    Column("name", String(40), primary_key=True),          # policy_version, audit
    Column("value", BigInteger, nullable=False, server_default="0"),
    Column("last_hash", String(64)),                       # audit chain head
)

policies = Table(
    "policies", metadata,
    Column("tenant_id", String(32), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("name", String(200), nullable=False),
    Column("status", String(24), nullable=False),
    Column("doc", JSONType, nullable=False),
    ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
)

# ---------------------------------------------------------- reference data ----
vendors = Table(
    "vendors", metadata,
    Column("tenant_id", String(32), primary_key=True),
    Column("id", String(64), primary_key=True),             # e.g. the ERP vendor number (SAP LIFNR)
    Column("legal_name", String(300), nullable=False),
    Column("country", String(2), nullable=False),
    _ts("updated_at"),
    Column("doc", JSONType, nullable=False),
    ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    Index(None, "tenant_id", "legal_name"),
)

# One row per approved bank account, normalised (upper-case, no spaces) so "is this account on
# file for anyone?" is an index lookup even with hundreds of thousands of vendors.
vendor_bank_accounts = Table(
    "vendor_bank_accounts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant_id", String(32), nullable=False),
    Column("vendor_id", String(64), nullable=False),
    Column("iban_norm", String(40)),
    Column("account_norm", String(40)),
    Column("swift_bic", String(11)),
    Column("country", String(2)),
    ForeignKeyConstraint(["tenant_id", "vendor_id"], ["vendors.tenant_id", "vendors.id"], ondelete="CASCADE"),
    Index(None, "tenant_id", "iban_norm"),
    Index(None, "tenant_id", "account_norm"),
    Index(None, "tenant_id", "swift_bic"),
    Index(None, "tenant_id", "vendor_id"),
)

payments = Table(
    "payments", metadata,
    Column("tenant_id", String(32), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("vendor_id", String(64), nullable=False),
    Column("invoice_number", String(128)),
    Column("amount", Numeric(20, 2), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("paid_on", String(32), nullable=False),          # ISO date as imported
    Column("account_ref", String(128)),
    Column("doc", JSONType, nullable=False),
    ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    Index(None, "tenant_id", "vendor_id"),
)

# ------------------------------------------------------------------ cases ----
cases = Table(
    "cases", metadata,
    Column("tenant_id", String(32), primary_key=True),
    Column("id", String(32), primary_key=True),
    Column("status", String(16), nullable=False),           # queued | running | completed | failed
    _ts("created_at"),
    _ts("updated_at"),
    _ts("lease_until", nullable=True),                      # worker lease while running
    Column("idempotency_key", String(128)),
    Column("external_ref", String(128)),                    # payment id in the ERP
    Column("decision", String(16)),                         # RELEASE | CALLBACK | HOLD
    Column("risk_score", Integer),
    Column("workflow_state", String(32), nullable=False),
    Column("amount", Numeric(20, 2)),
    Column("currency", String(3)),
    Column("payee", String(300)),
    Column("doc", JSONType, nullable=False),
    ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    Index("ix_cases_tenant_created", "tenant_id", text("created_at DESC")),
    Index("uq_cases_tenant_idempotency", "tenant_id", "idempotency_key", unique=True,
          postgresql_where=text("idempotency_key IS NOT NULL"), sqlite_where=text("idempotency_key IS NOT NULL")),
    Index(None, "tenant_id", "external_ref"),
    Index(None, "status", "lease_until"),                   # the worker's "what can I pick up?"
)

case_events = Table(
    "case_events", metadata,
    Column("tenant_id", String(32), primary_key=True),
    Column("case_id", String(32), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("type", String(40), nullable=False),
    Column("agent", String(40)),
    _ts("ts"),
    Column("data", JSONType, nullable=False),
    ForeignKeyConstraint(["tenant_id", "case_id"], ["cases.tenant_id", "cases.id"], ondelete="CASCADE"),
)

# --------------------------------------------------------------- audit log ----
# Append-only (trigger on PostgreSQL) and hash-chained: hash = sha256(entry without hash),
# prev_hash = hash of the entry before it in the same workspace.
audit_log = Table(
    "audit_log", metadata,
    Column("tenant_id", String(32), primary_key=True),
    Column("seq", BigInteger, primary_key=True),
    _ts("at"),
    Column("actor", String(320), nullable=False),
    Column("action", String(64), nullable=False),
    Column("target", String(200), nullable=False),
    Column("detail", JSONType, nullable=False),
    Column("prev_hash", String(64), nullable=False),
    Column("hash", String(64), nullable=False, unique=True),
    Index(None, "tenant_id", "target"),
)

# --------------------------------------------------------------- webhooks ----
webhooks = Table(
    "webhooks", metadata,
    Column("tenant_id", String(32), primary_key=True),
    Column("id", String(40), primary_key=True),
    Column("url", Text, nullable=False),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("events", JSONType, nullable=False),
    Column("secret", String(200), nullable=False),          # HMAC signing key (needed in clear to sign)
    _ts("created_at"),
    Column("doc", JSONType, nullable=False),
    ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
)

# Transactional outbox: a delivery row is written with the state change it reports, and a worker
# sends it with retries, so the ERP never misses a decision because a network call failed.
webhook_deliveries = Table(
    "webhook_deliveries", metadata,
    Column("id", String(40), primary_key=True),
    Column("tenant_id", String(32), nullable=False),
    Column("webhook_id", String(40), nullable=False),
    Column("event", String(64), nullable=False),
    Column("payload", JSONType, nullable=False),
    Column("status", String(16), nullable=False),           # pending | delivered | failed
    Column("attempts", Integer, nullable=False, server_default="0"),
    _ts("next_attempt_at"),
    _ts("created_at"),
    _ts("delivered_at", nullable=True),
    Column("response_status", Integer),
    Column("last_error", Text),
    Index(None, "status", "next_attempt_at"),
    Index(None, "tenant_id", "created_at"),
)
