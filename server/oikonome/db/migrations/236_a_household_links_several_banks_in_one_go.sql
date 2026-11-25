-- A household can connect several banks in one Plaid Link session
-- (specs/plaid-link.md, "Several banks in one session").
--
-- Plaid's multi-institution Link needs a Plaid USER: an id Plaid mints at
-- /user/create for one of our client_user_ids, under one client_id. A
-- household links under the instance's keys or under its own, and an id
-- made under one client is never sent with another's secret, so the row
-- is keyed by the client_id it was made under. The id is not a secret,
-- but it is an identifier at a third party tied to this instance's keys:
-- it is not exported and not restored (a household restored elsewhere
-- gets a new one the first time it links), and it goes with the household
-- when the household is erased.
--
--   plaid_users                 one Plaid user per (household, client_id)
--   plaid_link_sessions.multi_item  the session's link token let the person
--                               add more than one bank: such a session is
--                               finished once Plaid says the person left
--                               Link, or once it has gone quiet, not at its
--                               first bank
--   plaid_link_sessions.results_until  until when Plaid can still report a
--                               bank for its link token (the token's own
--                               lifetime plus the hours Plaid keeps results):
--                               a finished several-bank session keeps taking
--                               banks until then, and its row is kept
--   plaid_link_sessions.last_result_at  when the session last brought a new
--                               bank (the quiet clock)
--   plaid_link_sessions.looked_at  when a finished several-bank session was
--                               last asked of Plaid (the sweep's cadence)
--   plaid_item_ledger: a bank handed back because a several-bank session
--                               went past the institution cap is written
--                               with removed_reason 'overshoot', which the
--                               cap's released-Item hold leaves out (a
--                               bounded number per day)
--
-- Additive: no stored row changes. A session started by an older build
-- reads false, and is finished at its first bank exactly as before.

CREATE TABLE IF NOT EXISTS plaid_users (
    tenant_id  UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    client_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- the client_user_id the user was made for: the household id, or it
    -- with a short suffix when Plaid refused the plain one (NULL = plain)
    client_user_id TEXT,
    -- link tokens Plaid refused with this user in a row; dropped at 2
    refusals   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, client_id)
);
ALTER TABLE plaid_users ADD COLUMN IF NOT EXISTS client_user_id TEXT;
ALTER TABLE plaid_users
    ADD COLUMN IF NOT EXISTS refusals INTEGER NOT NULL DEFAULT 0;
-- several banks per sign-in switched off for this household and keys
-- until then (Plaid refusing the flag, or a session that went far past
-- the cap), why, and how many such pauses in a row (each doubles)
ALTER TABLE plaid_users
    ADD COLUMN IF NOT EXISTS multi_item_off_until TIMESTAMPTZ;
ALTER TABLE plaid_users ADD COLUMN IF NOT EXISTS off_reason TEXT;
ALTER TABLE plaid_users
    ADD COLUMN IF NOT EXISTS cooldowns INTEGER NOT NULL DEFAULT 0;
ALTER TABLE plaid_users ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON plaid_users;
CREATE POLICY tenant_isolation ON plaid_users
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));
GRANT SELECT, INSERT, UPDATE, DELETE ON plaid_users
    TO oikonome_app, oikonome_admin;

ALTER TABLE plaid_link_sessions
    ADD COLUMN IF NOT EXISTS multi_item BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE plaid_link_sessions
    ADD COLUMN IF NOT EXISTS results_until TIMESTAMPTZ;
ALTER TABLE plaid_link_sessions
    ADD COLUMN IF NOT EXISTS last_result_at TIMESTAMPTZ;
ALTER TABLE plaid_link_sessions
    ADD COLUMN IF NOT EXISTS looked_at TIMESTAMPTZ;
-- the client_id whose keys made the session's link token: every exchange
-- and removal of the session uses those keys (236)
ALTER TABLE plaid_link_sessions ADD COLUMN IF NOT EXISTS client_id TEXT;
-- the newest client poll's protocol (2 = keeps its window open on a
-- quiet answer); NULL = only older clients have looked (236)
ALTER TABLE plaid_link_sessions ADD COLUMN IF NOT EXISTS client_v SMALLINT;
