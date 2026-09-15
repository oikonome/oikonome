-- Aggregator Items are a metered resource: an allowance is consumed per
-- Item CREATED, and deleting an Item does NOT give it back. items rows can
-- vanish (tenant deletion cascades every tenant_id table), so live counts
-- alone under-report what has been consumed.
-- This ledger is APPEND-ONLY: one row per Plaid Item ever created, never
-- updated or deleted (the app role's UPDATE/DELETE are revoked in
-- migrate.py's grant block).
--
-- Deliberately NO tenant_id column: tenancy.delete_tenant_rows sweeps every
-- table carrying one, and the count must survive tenant deletion — the Item
-- stays counted at the aggregator. `tenant` is a plain text snapshot for
-- operator reconciliation only; it holds no user data.
CREATE TABLE IF NOT EXISTS plaid_item_ledger (
    item_id           TEXT PRIMARY KEY,           -- globally unique at Plaid
    environment       TEXT NOT NULL DEFAULT 'production',
    -- platform = the instance's own credentials (consumes the instance's
    -- allowance); byo = the tenant's own keys (their account, their allowance)
    credential_source TEXT NOT NULL DEFAULT 'platform',
    tenant            TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
