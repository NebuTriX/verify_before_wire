"""Initial schema: workspaces, reference data, cases, events, audit log, webhooks.

Trust property: on PostgreSQL the audit_log table is append-only at the database level.
Triggers reject UPDATE and DELETE (row level) and TRUNCATE (statement level) with an error,
so neither the application nor anyone holding its database credentials can rewrite or erase
history. Combined with the per-workspace SHA-256 hash chain (app/audit.py), tampering by a
database superuser who disables the trigger is still detectable by re-verifying the chain.

Revision ID: 0001
Revises: 
Create Date: 2026-10-18 21:18:50.439616
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('audit_log',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('seq', sa.BigInteger(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor', sa.String(length=320), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target', sa.String(length=200), nullable=False),
    sa.Column('detail', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('prev_hash', sa.String(length=64), nullable=False),
    sa.Column('hash', sa.String(length=64), nullable=False),
    sa.PrimaryKeyConstraint('tenant_id', 'seq', name=op.f('pk_audit_log')),
    sa.UniqueConstraint('hash', name=op.f('uq_audit_log_hash'))
    )
    op.create_index(op.f('ix_audit_log_tenant_id_target'), 'audit_log', ['tenant_id', 'target'], unique=False)
    op.create_table('counters',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=40), nullable=False),
    sa.Column('value', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('last_hash', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('tenant_id', 'name', name=op.f('pk_counters'))
    )
    op.create_table('tenants',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('active_policy_version', sa.Integer(), nullable=True),
    sa.Column('created_by', sa.String(length=320), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('doc', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tenants'))
    )
    op.create_table('webhook_deliveries',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('webhook_id', sa.String(length=40), nullable=False),
    sa.Column('event', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('response_status', sa.Integer(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_deliveries'))
    )
    op.create_index(op.f('ix_webhook_deliveries_status_next_attempt_at'), 'webhook_deliveries', ['status', 'next_attempt_at'], unique=False)
    op.create_index(op.f('ix_webhook_deliveries_tenant_id_created_at'), 'webhook_deliveries', ['tenant_id', 'created_at'], unique=False)
    op.create_table('api_keys',
    sa.Column('prefix', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('revoked', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('doc', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_api_keys_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('prefix', name=op.f('pk_api_keys'))
    )
    op.create_index(op.f('ix_api_keys_tenant_id'), 'api_keys', ['tenant_id'], unique=False)
    op.create_table('cases',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('external_ref', sa.String(length=128), nullable=True),
    sa.Column('decision', sa.String(length=16), nullable=True),
    sa.Column('risk_score', sa.Integer(), nullable=True),
    sa.Column('workflow_state', sa.String(length=32), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=2), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('payee', sa.String(length=300), nullable=True),
    sa.Column('doc', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_cases_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tenant_id', 'id', name=op.f('pk_cases'))
    )
    op.create_index(op.f('ix_cases_status_lease_until'), 'cases', ['status', 'lease_until'], unique=False)
    op.create_index('ix_cases_tenant_created', 'cases', ['tenant_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index(op.f('ix_cases_tenant_id_external_ref'), 'cases', ['tenant_id', 'external_ref'], unique=False)
    op.create_index('uq_cases_tenant_idempotency', 'cases', ['tenant_id', 'idempotency_key'], unique=True, postgresql_where=sa.text('idempotency_key IS NOT NULL'), sqlite_where=sa.text('idempotency_key IS NOT NULL'))
    op.create_table('invites',
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('accepted_by', sa.String(length=320), nullable=True),
    sa.Column('doc', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_invites_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('token_hash', name=op.f('pk_invites'))
    )
    op.create_index(op.f('ix_invites_tenant_id'), 'invites', ['tenant_id'], unique=False)
    op.create_table('members',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('uid', sa.String(length=320), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('added_by', sa.String(length=320), nullable=False),
    sa.Column('added_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_members_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tenant_id', 'uid', name=op.f('pk_members'))
    )
    op.create_index(op.f('ix_members_uid'), 'members', ['uid'], unique=False)
    op.create_table('payments',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('vendor_id', sa.String(length=64), nullable=False),
    sa.Column('invoice_number', sa.String(length=128), nullable=True),
    sa.Column('amount', sa.Numeric(precision=20, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('paid_on', sa.String(length=32), nullable=False),
    sa.Column('account_ref', sa.String(length=128), nullable=True),
    sa.Column('doc', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_payments_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tenant_id', 'id', name=op.f('pk_payments'))
    )
    op.create_index(op.f('ix_payments_tenant_id_vendor_id'), 'payments', ['tenant_id', 'vendor_id'], unique=False)
    op.create_table('policies',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('doc', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_policies_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tenant_id', 'version', name=op.f('pk_policies'))
    )
    op.create_table('vendors',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('legal_name', sa.String(length=300), nullable=False),
    sa.Column('country', sa.String(length=2), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('doc', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_vendors_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tenant_id', 'id', name=op.f('pk_vendors'))
    )
    op.create_index(op.f('ix_vendors_tenant_id_legal_name'), 'vendors', ['tenant_id', 'legal_name'], unique=False)
    op.create_table('webhooks',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('events', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('secret', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('doc', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], name=op.f('fk_webhooks_tenant_id_tenants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tenant_id', 'id', name=op.f('pk_webhooks'))
    )
    op.create_table('case_events',
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('case_id', sa.String(length=32), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('type', sa.String(length=40), nullable=False),
    sa.Column('agent', sa.String(length=40), nullable=True),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('data', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id', 'case_id'], ['cases.tenant_id', 'cases.id'], name=op.f('fk_case_events_tenant_id_cases'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('tenant_id', 'case_id', 'seq', name=op.f('pk_case_events'))
    )
    op.create_table('vendor_bank_accounts',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('vendor_id', sa.String(length=64), nullable=False),
    sa.Column('iban_norm', sa.String(length=40), nullable=True),
    sa.Column('account_norm', sa.String(length=40), nullable=True),
    sa.Column('swift_bic', sa.String(length=11), nullable=True),
    sa.Column('country', sa.String(length=2), nullable=True),
    sa.ForeignKeyConstraint(['tenant_id', 'vendor_id'], ['vendors.tenant_id', 'vendors.id'], name=op.f('fk_vendor_bank_accounts_tenant_id_vendors'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_vendor_bank_accounts'))
    )
    op.create_index(op.f('ix_vendor_bank_accounts_tenant_id_account_norm'), 'vendor_bank_accounts', ['tenant_id', 'account_norm'], unique=False)
    op.create_index(op.f('ix_vendor_bank_accounts_tenant_id_iban_norm'), 'vendor_bank_accounts', ['tenant_id', 'iban_norm'], unique=False)
    op.create_index(op.f('ix_vendor_bank_accounts_tenant_id_swift_bic'), 'vendor_bank_accounts', ['tenant_id', 'swift_bic'], unique=False)
    op.create_index(op.f('ix_vendor_bank_accounts_tenant_id_vendor_id'), 'vendor_bank_accounts', ['tenant_id', 'vendor_id'], unique=False)

    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION vbw_audit_log_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'audit_log is append-only: % is not allowed', TG_OP
                    USING ERRCODE = 'insufficient_privilege';
            END $$;
        """)
        op.execute("""CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log
                      FOR EACH ROW EXECUTE FUNCTION vbw_audit_log_append_only();""")
        op.execute("""CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
                      FOR EACH STATEMENT EXECUTE FUNCTION vbw_audit_log_append_only();""")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
        op.execute("DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log")
        op.execute("DROP FUNCTION IF EXISTS vbw_audit_log_append_only()")
    op.drop_index(op.f('ix_vendor_bank_accounts_tenant_id_vendor_id'), table_name='vendor_bank_accounts')
    op.drop_index(op.f('ix_vendor_bank_accounts_tenant_id_swift_bic'), table_name='vendor_bank_accounts')
    op.drop_index(op.f('ix_vendor_bank_accounts_tenant_id_iban_norm'), table_name='vendor_bank_accounts')
    op.drop_index(op.f('ix_vendor_bank_accounts_tenant_id_account_norm'), table_name='vendor_bank_accounts')
    op.drop_table('vendor_bank_accounts')
    op.drop_table('case_events')
    op.drop_table('webhooks')
    op.drop_index(op.f('ix_vendors_tenant_id_legal_name'), table_name='vendors')
    op.drop_table('vendors')
    op.drop_table('policies')
    op.drop_index(op.f('ix_payments_tenant_id_vendor_id'), table_name='payments')
    op.drop_table('payments')
    op.drop_index(op.f('ix_members_uid'), table_name='members')
    op.drop_table('members')
    op.drop_index(op.f('ix_invites_tenant_id'), table_name='invites')
    op.drop_table('invites')
    op.drop_index('uq_cases_tenant_idempotency', table_name='cases', postgresql_where=sa.text('idempotency_key IS NOT NULL'), sqlite_where=sa.text('idempotency_key IS NOT NULL'))
    op.drop_index(op.f('ix_cases_tenant_id_external_ref'), table_name='cases')
    op.drop_index('ix_cases_tenant_created', table_name='cases')
    op.drop_index(op.f('ix_cases_status_lease_until'), table_name='cases')
    op.drop_table('cases')
    op.drop_index(op.f('ix_api_keys_tenant_id'), table_name='api_keys')
    op.drop_table('api_keys')
    op.drop_index(op.f('ix_webhook_deliveries_tenant_id_created_at'), table_name='webhook_deliveries')
    op.drop_index(op.f('ix_webhook_deliveries_status_next_attempt_at'), table_name='webhook_deliveries')
    op.drop_table('webhook_deliveries')
    op.drop_table('tenants')
    op.drop_table('counters')
    op.drop_index(op.f('ix_audit_log_tenant_id_target'), table_name='audit_log')
    op.drop_table('audit_log')
