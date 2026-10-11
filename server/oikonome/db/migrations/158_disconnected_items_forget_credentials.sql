-- A disconnected connection keeps no credential (sync/base.py
-- disconnect_item). An exchange API key or a SimpleFIN bridge URL is read
-- by nothing once its connection is archived — it comes back only by
-- linking anew — so a kept one is a live secret riding every backup.
-- Disconnects made before that rule left theirs in place; this drops them.
--
-- Two kinds keep their token:
--   * plaid: an archived Plaid item that still holds a token is one whose
--     release failed, and the reaper's retry needs the token to finish it
--   * plan_csv: an archived-by-design push item whose token is a marker,
--     not a secret (keep in step with base.DISCONNECT_KEEPS_TOKEN)
--
-- Idempotent: only rows that still hold a token are written.

UPDATE items
   SET access_token = NULL
 WHERE status = 'archived'
   AND access_token IS NOT NULL
   AND aggregator NOT IN ('plaid', 'plan_csv');
