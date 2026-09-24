-- The aggregator's merchant name for one row, MOVED off the row's key.
--
-- A row's identity key is its outlet, else the merchant name the
-- aggregator gave it, else the bank's own descriptor. When a bank line and
-- the aggregator have agreed on one payee several times over and the
-- aggregator then calls a single charge on that line something the line
-- does not say, the resolver files that charge under the merchant the line
-- means -- and moves the aggregator's name into this column, leaving
-- merchant_name NULL. The row is then, for every reader in the codebase,
-- exactly a row the aggregator never named: COALESCE(merchant_name, name)
-- reads the bank line by construction.
--
-- That is why the name MOVES rather than being flagged where it stands.
-- The boolean this migration replaces asked every reader to go through one
-- SQL fragment that NULLed the name when the flag was set; a couple of
-- dozen readers spell COALESCE(merchant_name, name) by hand instead -- the
-- budget, the bills, the savings and anomaly passes, the ledger web reads,
-- the category refiner, the re-anchor -- and each went on answering to a
-- name the ledger had already judged a misreading of the line. A flag
-- every reader must remember is the wrong shape for an identity key.
--
-- A re-sync that restates either string lapses the judgement, because it
-- was about the strings as the row then carried them: the name goes back
-- into merchant_name, this column is cleared, and the row is resolved
-- afresh. An entity id arriving for the row takes it back the same way.
--
-- Search: transactions.search_text is a STORED generated column over
-- merchant_name, so a name set aside leaves the search index with it -- the
-- row is still found by its bank line, which is what the statement shows,
-- but not by the one-off name. Adding this column to that expression would
-- rewrite the whole ledger and every index on it, which is the outage
-- migration 111 built itself around avoiding, so the trade is made the
-- other way: one charge's misreading is not worth a table rewrite on every
-- install.
ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS merchant_name_set_aside TEXT;

-- Rows the previous build flagged carry their set-aside name in place;
-- move it, so a ledger that upgrades across this migration means on the
-- new column exactly what it meant on the old one. Guarded because the
-- boolean may never have existed here at all: a fresh install loads
-- schema.sql -- which now creates only the TEXT column -- and then runs
-- every numbered migration over it.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_attribute
                WHERE attrelid = 'transactions'::regclass
                  AND attname = 'merchant_name_overruled'
                  AND NOT attisdropped) THEN
        EXECUTE 'UPDATE transactions
                    SET merchant_name_set_aside = merchant_name,
                        merchant_name = NULL
                  WHERE merchant_name_overruled
                    AND merchant_name IS NOT NULL';
    END IF;
END $$;

ALTER TABLE transactions DROP COLUMN IF EXISTS merchant_name_overruled;
