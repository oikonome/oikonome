-- Reconnecting a bank ONTO the accounts a restore brought back.
--
-- A restored item is a shell with no access token, so Plaid's update mode
-- cannot repair it — the only way forward is a fresh link, and a fresh
-- link mints new account ids (they are item-scoped) and new transaction
-- ids for the same real-world charges. Left alone that gives the tenant
-- two of every account and, across Plaid's ~24-month backfill, two of
-- every transaction.
--
-- Adoption rewrites the restored account's id to the incoming Plaid one,
-- so afterwards the tenant looks exactly like a normal Plaid tenant and
-- the sync hot path carries no indirection. This table is the record of
-- that rewrite: what became what, when, and the date before which the
-- restored history is authoritative, so the first sync can drop the
-- backfill it already has instead of duplicating it.
--
-- Tenant-scoped and RLS-protected, like `items` and `accounts`: the sync
-- path writes it on the tenant connection, and a row is only ever about
-- one household's account. It is not control-plane: the writer is the app
-- role, so RLS is what confines it, not a revoked grant.
CREATE TABLE IF NOT EXISTS account_adoptions (
    tenant_id       UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    account_id      TEXT NOT NULL,          -- the NEW (Plaid) id, after the rewrite
    old_account_id  TEXT NOT NULL,          -- what it was, for the audit trail
    item_id         TEXT NOT NULL,          -- the item that adopted it
    -- restored history is authoritative on/before this date; the first
    -- sync drops incoming rows older than it rather than duplicating them
    cutoff_date     DATE,
    adopted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- set once the first full sync has landed: the guard is a one-shot,
    -- and leaving it on would silently drop later CORRECTIONS to old rows
    reconciled_at   TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, account_id, adopted_at)
);
CREATE INDEX IF NOT EXISTS idx_account_adoptions_live
    ON account_adoptions(tenant_id, account_id) WHERE reconciled_at IS NULL;

DO $$
BEGIN
    EXECUTE 'ALTER TABLE account_adoptions ENABLE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON account_adoptions';
    EXECUTE
        'CREATE POLICY tenant_isolation ON account_adoptions '
        'USING (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
        'WITH CHECK (tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)';
END $$;
