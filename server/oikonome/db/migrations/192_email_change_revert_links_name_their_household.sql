-- A login-email revert link is bound to the household its account was in
-- when the link was minted.
--
-- A users row can be moved into another household (a member reclaimed by
-- the person whose address it took becomes the owner of a fresh one), and
-- a link minted before the move must not reach the household it landed
-- in. The door compares this column with the account's household as it
-- stands and refuses a link that no longer matches.
--
-- Among several live links the EARLIEST wins: spending one burns every
-- link minted at or after it, so the second change a hijacker makes
-- (whose link goes to an inbox they read) cannot undo the owner's revert.
-- A link re-minted to the same address keeps the place of the one it
-- replaces, so a third change cannot push the owner's link behind the
-- hijacker's. A password reset or change burns nothing, and a link is
-- minted only when the address being left was verified.
--
-- Backfill: a link minted before this column existed takes its account's
-- household as it stands now — the best record there is, and a link
-- lives three days.

ALTER TABLE email_change_reverts
    ADD COLUMN IF NOT EXISTS tenant_id UUID
        REFERENCES tenants(id) ON DELETE CASCADE;

UPDATE email_change_reverts r SET tenant_id = u.tenant_id
  FROM users u
 WHERE u.id = r.user_id AND r.tenant_id IS NULL;

ALTER TABLE email_change_reverts ALTER COLUMN tenant_id SET NOT NULL;
