-- The household's activity log: who changed what, and when.
--
-- Two adults keep one set of books, and until now the ledger recorded the
-- CHANGE (a category override, a bill's new amount, a note) and nothing
-- about the person: a member's edit and the owner's read the same. This
-- table is the record of the hand-authored acts — categories, splits,
-- notes, bills and their offers, rules, receipts, reimbursements, merchant
-- names, budget settings — one row per act, written by the door that made
-- the change, and read back on the Users page (web) and the Activity
-- screen (mobile). Machine writes (the sync, the bill pass, the nightly
-- categorizer) are deliberately absent: the log answers "who did this",
-- and the answer to a machine write is always the app.
--
-- The actor is stored as TEXT (the person's sign-in address, or the
-- script token's name) beside their user id, and the id carries no
-- foreign key: `users` is a control-plane table a tenant table cannot
-- reference, and a person removed from the household keeps their name on
-- the changes they made — that is the point of a log. Rows are keyed by a
-- UUID so an export/restore round trip carries them without collision.
--
-- `summary` is the sentence the clients render, composed at write time by
-- engine/activity.py; `kind`, `action`, `target` and `detail` are the
-- structured facts behind it (filters, tests, a future undo).
CREATE TABLE IF NOT EXISTS activity_log (
    tenant_id     UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    id            UUID NOT NULL DEFAULT gen_random_uuid(),
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_user_id UUID,                          -- NULL for a script token / an email-link actor
    actor         TEXT NOT NULL,                 -- sign-in address, or "script:<name>"
    kind          TEXT NOT NULL,                 -- category | split | note | bill | rule | receipt | ...
    action        TEXT NOT NULL,                 -- set | cleared | added | edited | archived | ...
    target        TEXT,                          -- the transaction id / bill payee / merchant / category
    label         TEXT NOT NULL DEFAULT '',      -- the target as a person reads it
    summary       TEXT NOT NULL,                 -- the sentence
    detail        JSONB CONSTRAINT activity_log_detail_is_an_object
                  CHECK (detail IS NULL OR jsonb_typeof(detail) = 'object'),
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS activity_log_at ON activity_log (tenant_id, at DESC);

ALTER TABLE activity_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON activity_log;
CREATE POLICY tenant_isolation ON activity_log
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));
