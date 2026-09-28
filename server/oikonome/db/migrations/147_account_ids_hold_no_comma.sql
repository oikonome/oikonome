-- An account id may not contain a comma.
--
-- Two per-connection settings carry sets of account ids as one
-- comma-joined string — the accounts every money figure ignores (linked
-- duplicates and hidden accounts) and the accounts that belong to a
-- business entity — and the SQL that reads them splits on ','. An id with
-- a comma in it splits into pieces that match nothing, so a hidden
-- account, a linked duplicate or a business account would quietly count
-- in the household's figures again. No source mints such an id today (the
-- aggregators' ids are opaque tokens, manual accounts are slugged), but
-- nothing refused one either: a bridge or an archive could supply it.
--
-- NOT VALID first, so a row that somehow already breaks the rule cannot
-- fail the upgrade; then validated when no row does. The accounts table is
-- one row per account, so the scan is short.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = to_regclass('accounts')
                      AND conname = 'accounts_id_has_no_comma') THEN
        ALTER TABLE accounts ADD CONSTRAINT accounts_id_has_no_comma
            CHECK (strpos(id, ',') = 0) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM accounts WHERE strpos(id, ',') > 0) THEN
        ALTER TABLE accounts VALIDATE CONSTRAINT accounts_id_has_no_comma;
    ELSE
        RAISE WARNING 'accounts hold ids with a comma; the no-comma rule '
                      'applies to new ids only until they are renamed';
    END IF;
END $$;
