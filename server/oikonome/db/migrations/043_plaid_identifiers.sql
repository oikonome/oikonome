-- Plaid identifier retention: Plaid support and the
-- dashboard Activity Log key on item_id (have it), institution_id
-- (populated at link), the Hosted Link session's
-- link_session_id, and per-call request_ids. Persist the session id on
-- the item and the request_id on every sync_log row (last page's id on
-- success, the failing call's on error).
ALTER TABLE items    ADD COLUMN IF NOT EXISTS link_session_id TEXT;
ALTER TABLE sync_log ADD COLUMN IF NOT EXISTS request_id      TEXT;
