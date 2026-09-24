-- `transactions.raw` is a DOCUMENT too: the import-batch rollback patches
-- it with `jsonb_set(raw, '{_batches}', ...)` and `|| jsonb_build_object`,
-- any writer that merges keys into it does the same, and the settlement
-- carry reads keys out of it — every one of which is defined only over an
-- object (see 128_raw_is_an_object.sql for what a scalar or an array does
-- to those writers). Bills and connections got the constraint in 128;
-- this extends it to the ledger, the same way: garbage shapes become an
-- empty document (nothing could read them), NULL is left alone, and the
-- column is made incapable of holding anything else from here on.
DO $$
DECLARE
    n BIGINT;
BEGIN
    UPDATE transactions SET raw = '{}'::jsonb
     WHERE raw IS NOT NULL AND jsonb_typeof(raw) <> 'object';
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN
        RAISE NOTICE 'repaired % non-document raw row(s) in transactions', n;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = to_regclass('transactions')
                      AND conname = 'transactions_raw_is_an_object') THEN
        ALTER TABLE transactions ADD CONSTRAINT transactions_raw_is_an_object
            CHECK (raw IS NULL OR jsonb_typeof(raw) = 'object');
    END IF;
END $$;
