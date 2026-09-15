-- A JSONB column that declares '[]' as its default is declaring itself a
-- list, and Postgres does not hold anyone to that: jsonb_array_elements
-- over a row whose value is an object or a number RAISES, and the raise
-- aborts the whole statement rather than skipping the row. So a single
-- order or receipt whose line items landed as the wrong shape can blank a
-- query that reads every row — a household's entire ledger search — on
-- every attempt, with no way back except editing the row by hand.
--
-- Guarding each reader has been tried, and it only ever covers the readers
-- that existed when the guard was written; the next table with an item
-- list copies the write idiom and starts the cycle again. The column
-- itself can be made incapable of holding the wrong shape, and then every
-- reader of these tables — the ones here today and the ones nobody has
-- written yet — inherits that for free, in both clients and in the
-- collectors' push paths.
--
-- A row that ALREADY holds the wrong shape keeps everything else it says
-- about itself (amount, date, payee, category, summary) and loses only the
-- item list nothing could read anyway; it reads as "no items" until the
-- collector pushes that order again, which rewrites items_json. Refusing
-- to migrate such a household was never an option — a constraint that
-- cannot be applied to the data that exists is not a fix.
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['amazon_orders', 'costco_receipts']
    LOOP
        CONTINUE WHEN to_regclass(t) IS NULL;
        EXECUTE format(
            'UPDATE %I SET items_json = ''[]''::jsonb '
            'WHERE jsonb_typeof(items_json) IS DISTINCT FROM ''array''', t);
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
                        WHERE conrelid = to_regclass(t)
                          AND conname = t || '_items_json_is_array') THEN
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I '
                'CHECK (jsonb_typeof(items_json) = ''array'')',
                t, t || '_items_json_is_array');
        END IF;
    END LOOP;
END $$;
