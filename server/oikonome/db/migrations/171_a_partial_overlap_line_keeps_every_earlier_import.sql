-- An import-overlap log line that ends '; +N more' (see migration 169)
-- lists only the first 25 rows its run set aside, in the order the feed
-- delivered them, so the listed dates say nothing about when the unlisted
-- rows fell: a feed page is not sorted by date, and a pull that walks
-- account by account lists one account's rows first. Migration 169 bounded
-- such a line by the span of its listed dates, which leaves the file rows
-- standing in for the unlisted ones unnoted, and undoing their import
-- deletes the only copy of those charges.
--
-- The guard compared each feed row against every file-imported row in the
-- household, on any account and any date, so for a partial line this
-- notes every live file-imported row imported before the line was
-- written. The note names no account, like 169's. That keeps many more
-- rows than the guard set aside; such a row is still released when the
-- household has no bank feed left, and the owner may delete an account
-- holding only rows like these once told how many (sync/base.py
-- relied_on_by_feed, kept_without_account). One kept too many is
-- recoverable; one lost is not.
--
-- Same reach as 169 otherwise: only lines written before migration 161,
-- a row whose first import came after the line is left alone, and a row
-- that already carries a note is not touched. The id prefixes are
-- sync/base.FILE_IMPORT_ID_PREFIXES; a test holds the two lists equal.

DO $$
DECLARE
    n integer;
BEGIN
    WITH upto AS (
        SELECT COALESCE(
                   (SELECT applied_at FROM schema_migrations
                     WHERE name = '161_imported_rows_note_what_the_feed_relies_on.sql'),
                   now()) AS t),
    partial AS (
        SELECT s.tenant_id, max(s.ran_at) AS ran_at
          FROM sync_log s, upto
         WHERE s.item_id = 'import-overlap'
           AND s.error LIKE 'suppressed as covered by file-imported rows: %'
           AND s.error ~ '; \+[0-9]+ more$'
           AND s.ran_at < upto.t
         GROUP BY s.tenant_id),
    hit AS (
        SELECT t.tenant_id, t.id
          FROM transactions t
          JOIN partial p ON p.tenant_id = t.tenant_id
         WHERE t.removed = 0
           AND t.id LIKE ANY (ARRAY[
               'pdf:%', 'csv:%', 'ofx:%', 'qif:%', 'mint:%',
               'ynab:%', 'monarch:%', 'copilot:%', 'simplifi:%'])
           AND (t.raw IS NULL OR jsonb_typeof(t.raw) = 'object')
           AND jsonb_typeof(t.raw->'_stood_in') IS DISTINCT FROM 'object'
           -- the row's first import came after the latest such line
           AND NOT EXISTS (
               SELECT 1 FROM import_batches b
                WHERE b.tenant_id = t.tenant_id
                  AND b.id = COALESCE(
                        CASE WHEN jsonb_typeof(t.raw->'_batches') = 'array'
                             THEN t.raw->'_batches'->>0 END,
                        t.raw->>'_batch')
                  AND b.created_at > p.ran_at))
    UPDATE transactions t
       SET raw = COALESCE(t.raw, '{}'::jsonb) || jsonb_build_object(
             '_stood_in', jsonb_build_object('source', 'feed',
                                             'accounts', '[]'::jsonb))
      FROM hit
     WHERE t.tenant_id = hit.tenant_id AND t.id = hit.id;
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN
        RAISE NOTICE 'imported rows: % noted as standing in for a bank '
                     'feed''s row, from a partial import-overlap log line', n;
    END IF;
END $$;
