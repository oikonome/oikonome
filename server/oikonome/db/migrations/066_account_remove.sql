-- Per-account remove: soft-hide one account under a live multi-account
-- connection without releasing the Item. Disconnect/purge paths use the
-- existing items.status='archived' + /item/remove flow; this column only
-- covers "remove this account, keep the institution connection".
-- Sync must not re-surface a user-removed account (balance stays null).
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS user_removed_at TIMESTAMPTZ;
