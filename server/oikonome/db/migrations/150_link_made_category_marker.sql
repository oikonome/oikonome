-- Which per-row category pins a reimbursement link wrote, and what a
-- person had chosen before the link replaced it.
--
-- A full link gives both sides a transfer category through the same pin a
-- person's edit writes, so every automatic writer leaves it alone. Unlink
-- has to take back only what the link wrote. Telling the two apart by
-- WHEN the pin was set could not work both ways: a person who re-picked
-- the transfer category after linking had their answer cleared by the
-- unlink, and a person's own category that the link replaced was lost
-- for good, because nothing remembered it.
--
-- link_made: this pin was written by a link. Every person-facing writer
-- resets it, so a category chosen after the link survives an unlink.
-- link_prior: the person's own pin the link replaced (the empty string is
-- their "no category" answer); NULL when there was none. Unlink puts it
-- back.
--
-- Existing pins are classified by the rule unlink used until now: a
-- transfer pin a person did not set before the row's first link.
ALTER TABLE manual_categories
    ADD COLUMN IF NOT EXISTS link_made BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE manual_categories ADD COLUMN IF NOT EXISTS link_prior TEXT;

UPDATE manual_categories m
   SET link_made = TRUE
 WHERE m.bill_id IS NULL AND NOT m.link_made
   AND ((m.category = 'TRANSFER_OUT' AND m.set_at >= (
            SELECT MIN(r.created_at) FROM reimbursements r
             WHERE r.tenant_id = m.tenant_id
               AND (r.expense_id = m.transaction_id
                    OR r.reimburse_id = m.transaction_id))
         AND EXISTS (SELECT 1 FROM reimbursements r
                      WHERE r.tenant_id = m.tenant_id
                        AND r.expense_id = m.transaction_id))
     OR (m.category = 'TRANSFER_IN' AND m.set_at >= (
            SELECT MIN(r.created_at) FROM reimbursements r
             WHERE r.tenant_id = m.tenant_id
               AND (r.expense_id = m.transaction_id
                    OR r.reimburse_id = m.transaction_id))
         AND EXISTS (SELECT 1 FROM reimbursements r
                      WHERE r.tenant_id = m.tenant_id
                        AND r.reimburse_id = m.transaction_id)));
