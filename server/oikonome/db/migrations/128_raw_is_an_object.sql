-- `raw` on a bill and on a connection is a DOCUMENT: every writer extends
-- it with `raw || '{...}'` or `jsonb_set(raw, ...)`, and both of those are
-- defined only when the value on the left is an object. Hand either one a
-- scalar and Postgres raises; hand `||` an array and it silently APPENDS
-- the patch as an element, so the next reader finds none of the keys that
-- were just written and the row quietly stops carrying its own state.
--
-- Neither failure is recoverable from inside the app. A bill whose raw is
-- the wrong shape takes down the write that touches it — a due-date roll,
-- a drift correction, a merchant backfill — and those run in the nightly
-- pass over every bill in the household, so one row stops the pass for all
-- of them, identically every night. A connection whose raw is the wrong
-- shape can never record a product issue, which is the very state the
-- reconnect banner reads.
--
-- Guarding each writer has been tried on the item-list columns and it only
-- ever covers the writers that existed when the guard was written; the next
-- one copies the idiom. The column itself can be made incapable of holding
-- anything but a document, and then every writer — the ones here today, the
-- collectors' push paths, the next importer — inherits that for free.
--
-- A row that ALREADY holds the wrong shape holds nothing readable: every
-- key this codebase looks for lives under an object, so a scalar or an
-- array there is garbage from a legacy import and is replaced with an empty
-- document. The row keeps everything else it says about itself (a bill's
-- payee, amount, cadence and due date are columns, not document keys; a
-- connection's institution, token and cursor likewise) and loses only the
-- part nothing could read. NULL is left alone — it already means "no
-- document" to every reader, and the writers COALESCE it.
DO $$
DECLARE
    t TEXT;
    n BIGINT;
BEGIN
    FOREACH t IN ARRAY ARRAY['bills', 'items']
    LOOP
        CONTINUE WHEN to_regclass(t) IS NULL;
        EXECUTE format(
            'UPDATE %I SET raw = ''{}''::jsonb '
            'WHERE raw IS NOT NULL AND jsonb_typeof(raw) <> ''object''', t);
        GET DIAGNOSTICS n = ROW_COUNT;
        IF n > 0 THEN
            RAISE NOTICE 'repaired % non-document raw row(s) in %', n, t;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
                        WHERE conrelid = to_regclass(t)
                          AND conname = t || '_raw_is_an_object') THEN
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I '
                'CHECK (raw IS NULL OR jsonb_typeof(raw) = ''object'')',
                t, t || '_raw_is_an_object');
        END IF;
    END LOOP;
END $$;
