-- Rename "Recurring" → "Bills" all the way down (a hard rename, no
-- compat shims): recurring → bills,
-- recurring_proposals → bill_proposals.
--
-- Ordering gotcha: migrate.run applies schema.sql BEFORE migrations, so on
-- an existing install the updated schema.sql has already created EMPTY
-- bills / bill_proposals tables by the time this runs. Those empties are
-- dropped (nothing has ever written to them — migrate runs before the app
-- starts) so the data-carrying tables can take the names. PK constraints
-- are renamed too so migrated installs converge on the exact names a fresh
-- schema.sql install gets.
DO $$ BEGIN
    IF to_regclass('recurring') IS NOT NULL THEN
        DROP TABLE IF EXISTS bills;
        ALTER TABLE recurring RENAME TO bills;
        ALTER TABLE bills RENAME CONSTRAINT recurring_pkey TO bills_pkey;
    END IF;
    IF to_regclass('recurring_proposals') IS NOT NULL THEN
        DROP TABLE IF EXISTS bill_proposals;
        ALTER TABLE recurring_proposals RENAME TO bill_proposals;
        ALTER TABLE bill_proposals
            RENAME CONSTRAINT recurring_proposals_pkey TO bill_proposals_pkey;
    END IF;
END $$;
