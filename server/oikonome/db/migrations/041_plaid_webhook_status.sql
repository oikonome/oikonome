-- Plaid webhook receiver: ITEM webhooks (ERROR, PENDING_EXPIRATION,
-- USER_PERMISSION_REVOKED) record what Plaid told us about a connection
-- OUT OF BAND, separate from `status` (which the sync loop owns and
-- overwrites on every run). The re-auth UX and the future zombie-Item
-- reaper read these; a successful update-mode re-link
-- and a clean sync clear them.
ALTER TABLE items ADD COLUMN IF NOT EXISTS webhook_status    TEXT;
ALTER TABLE items ADD COLUMN IF NOT EXISTS webhook_status_at TIMESTAMPTZ;
