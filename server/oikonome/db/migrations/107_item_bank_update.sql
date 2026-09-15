-- When the BANK last handed Plaid new data, as opposed to when we last
-- asked Plaid for it.
--
-- "Sync now" reads Plaid's copy of an item (/transactions/sync); it does
-- not make Plaid go to the bank. So a sync can legitimately return
-- nothing for hours after a charge appears in the bank's own app, and the
-- UI had no way to say why — it only ever showed "synced 2m ago", which
-- is our poll, not the bank's. Plaid reports the real thing on
-- /item/get as status.transactions.last_successful_update; that call is
-- included in the Transactions subscription (unlike /transactions/refresh,
-- which is billed per request and is never called — see sync/plaid.py).
--
-- Stored per item so every surface can show both clocks side by side.
ALTER TABLE items ADD COLUMN IF NOT EXISTS bank_updated_at TIMESTAMPTZ;
ALTER TABLE items ADD COLUMN IF NOT EXISTS bank_failed_at  TIMESTAMPTZ;
