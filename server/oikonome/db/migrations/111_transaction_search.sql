-- oikonome: no-transaction
-- Transaction search reads one precomputed column, not thirteen LIKEs.
--
-- A search ran LOWER(...) LIKE '%q%' over eight columns per row, parsed the
-- raw JSONB for the bank's description on every row, and ran four
-- correlated EXISTS sub-selects (canonical map, merchant row, receipt
-- items, Amazon order items via jsonb_array_elements) per row — seconds
-- on a ledger of tens of thousands of rows. Everything that belongs to the row itself now lives in
-- one generated, lower-cased text column; the cross-table matches become
-- set-membership tests evaluated once per query; and a trigram index lets
-- '%q%' use an index at all.
--
-- pg_trgm is a trusted extension (PG13+): a database owner can create it
-- without superuser. Where it cannot be created the column still exists and
-- the search still works — only the index is skipped — so self-hosts on a
-- locked-down Postgres lose speed, never results.
--
-- The three GIN builds are CONCURRENT, and that is why the file is marked
-- no-transaction: three ordinary builds in one transaction hold ACCESS
-- EXCLUSIVE on the ledger for as long as all three take, which on a large
-- self-hosted ledger upgrading across this migration is an outage. A
-- concurrent build takes only SHARE UPDATE EXCLUSIVE, so the household
-- keeps reading and writing while it runs. The column add still rewrites
-- the table once under ACCESS EXCLUSIVE — a STORED generated column has to
-- be materialized — but that is one pass over the rows instead of one pass
-- plus three index builds. Every statement below is idempotent, because a
-- no-transaction migration that fails part-way is re-run, not rolled back.

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS search_text TEXT GENERATED ALWAYS AS (
        lower(
            coalesce(merchant_name, '') || ' ' ||
            coalesce(name, '') || ' ' ||
            coalesce(merchant_outlet, '') || ' ' ||
            replace(coalesce(category_override, category_primary, ''), '_', ' ') || ' ' ||
            coalesce(category_override, category_primary, '') || ' ' ||
            coalesce(raw->>'original_description', '') || ' ' ||
            coalesce(check_number, '') || ' ' ||
            coalesce(location_city, '') || ' ' ||
            coalesce(payment_processor, '')
        )
    ) STORED;

DO $$
BEGIN
    BEGIN
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'pg_trgm unavailable (%): search stays unindexed', SQLERRM;
    END;
END $$;

-- A concurrent build that was interrupted (a cancelled upgrade, a killed
-- container) leaves an INVALID index behind, and IF NOT EXISTS would then
-- adopt it forever — a name that satisfies the migration while no query
-- can use it. Clear those first so the re-run really rebuilds.
DO $$ DECLARE n text; BEGIN
    FOREACH n IN ARRAY ARRAY['transactions_search_trgm',
                             'merchant_canonical_canon_trgm',
                             'merchants_name_trgm']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_index i
                   JOIN pg_class c ON c.oid = i.indexrelid
                   WHERE c.relname = n AND NOT i.indisvalid) THEN
            EXECUTE format('DROP INDEX %I', n);
        END IF;
    END LOOP;
END $$;

-- oikonome: skip-unless SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'
CREATE INDEX CONCURRENTLY IF NOT EXISTS transactions_search_trgm
    ON transactions USING gin (search_text gin_trgm_ops);

-- oikonome: skip-unless SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'
CREATE INDEX CONCURRENTLY IF NOT EXISTS merchant_canonical_canon_trgm
    ON merchant_canonical USING gin (lower(canonical) gin_trgm_ops);

-- oikonome: skip-unless SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'
CREATE INDEX CONCURRENTLY IF NOT EXISTS merchants_name_trgm
    ON merchants USING gin (lower(name) gin_trgm_ops);
