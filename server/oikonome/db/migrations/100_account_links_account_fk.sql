-- account_links rows go with the accounts they name.
--
-- A link row says "this account is the same real account as that one", and
-- the shadow set built from it excludes every non-home member from all money
-- aggregates. Without a foreign key, deleting an account leaves its link row behind: `purge_account_data`
-- remembers to clean up, but the collector unlink paths (coinbase.rollback,
-- plan_csv.rollback) issue a bare `DELETE FROM accounts` and do not. It
-- matters because collector account ids are DERIVED, not
-- random (`coinbase:<label>`, the plan-CSV account slug), so an unlink followed by
-- a re-link reproduces the SAME id. The stale row then re-attaches, and the
-- freshly imported account is silently shadow-excluded from every balance and
-- every total — money missing from the app with nothing to explain it.
--
-- A link is meaningless without its account, so CASCADE: the surviving member
-- of the group keeps its own row (a group of one is inert), and the Accounts
-- page shows the account unlinked, which is what it now is.
--
-- Orphans go first — the constraint cannot be created over them, and a failed
-- CREATE rolls back the file, never stamps schema_migrations, and re-fails on
-- every boot.

DELETE FROM account_links l
 WHERE NOT EXISTS (
     SELECT 1 FROM accounts a
      WHERE a.tenant_id = l.tenant_id
        AND a.id        = l.account_id);

ALTER TABLE account_links
    DROP CONSTRAINT IF EXISTS account_links_account_fk;
ALTER TABLE account_links
    ADD CONSTRAINT account_links_account_fk
    FOREIGN KEY (tenant_id, account_id)
    REFERENCES accounts (tenant_id, id)
    ON DELETE CASCADE;
