-- zombie-Item reaper. An Item bills monthly for as long
-- as it exists at Plaid, so connections dead 30+ days get auto-released
-- (/item/remove) and archived locally with a reason the Accounts page and
-- the daily-email notice read. archived_reason values:
--   auto-reap   — reaper released it after OIKONOME_PLAID_REAP_DAYS of
--                 unrecoverable failure
--   plaid-gone  — orphan reconcile found it already gone at Plaid
--                 (ITEM_NOT_FOUND on /item/get)
-- User-initiated disconnects keep a NULL reason (pre-existing behavior).
ALTER TABLE items ADD COLUMN IF NOT EXISTS archived_reason TEXT;
ALTER TABLE items ADD COLUMN IF NOT EXISTS archived_at    TIMESTAMPTZ;

-- Slot-ledger removal stamp: the ledger stays append-only for the app
-- role (an Item's slot is burned for good — releasing it at the
-- aggregator gets nothing back), but the reaper records WHEN and WHY an
-- Item stopped billing, so the admin console's ledger shows live vs
-- released. Written via the admin role only (migrate.py revokes
-- app-role UPDATE).
ALTER TABLE plaid_item_ledger ADD COLUMN IF NOT EXISTS removed_at     TIMESTAMPTZ;
ALTER TABLE plaid_item_ledger ADD COLUMN IF NOT EXISTS removed_reason TEXT;
