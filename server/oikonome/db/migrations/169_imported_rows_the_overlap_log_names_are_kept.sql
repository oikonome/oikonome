-- An imported row that a bank feed set its own copy of a charge aside for
-- carries a note of that fact (raw._stood_in, see sync/base.py
-- STOOD_IN_KEY), and undoing the import or deleting its account keeps it.
-- Before the note existed the import-overlap guard recorded the same fact
-- only in the activity log, as one sync_log line per run:
--
--   suppressed as covered by file-imported rows: <feed id> <date>; ...
--
-- listing at most 25 rows and ending '; +N more' when it set aside more.
-- Those rows' imports would now be undone in full, and the feed will not
-- send what it set aside again, so the charge would be gone for good.
--
-- This reads those lines back and notes every live file-imported row the
-- guard could have matched: a row within the guard's three-day reach of a
-- listed date, or, for a line that names only the first rows of its run,
-- anywhere in the span its listed dates cover (with the same reach at each
-- edge). The line names neither the file row nor the feed's account, so
-- the note names no account, which relied_on_by_feed treats as "keep".
-- That keeps more rows than the guard set aside, and a row kept too many
-- is deleted by hand; one lost is not recoverable.
--
-- Only lines written before migration 161 are read: from then on the
-- guard writes the note itself. A row imported after the line was written
-- cannot have been matched by it and is left alone. The id prefixes are
-- sync/base.FILE_IMPORT_ID_PREFIXES; a test holds the two lists equal.
-- Re-runnable: a row that already carries the note is not touched.

DO $$
DECLARE
    n integer;
BEGIN
    WITH upto AS (
        SELECT COALESCE(
                   (SELECT applied_at FROM schema_migrations
                     WHERE name = '161_imported_rows_note_what_the_feed_relies_on.sql'),
                   now()) AS t),
    lines AS (
        SELECT s.tenant_id, s.id, s.ran_at, s.error,
               s.error ~ '; \+[0-9]+ more$' AS partial
          FROM sync_log s, upto
         WHERE s.item_id = 'import-overlap'
           AND s.error LIKE 'suppressed as covered by file-imported rows: %'
           AND s.ran_at < upto.t),
    entries AS (
        SELECT l.tenant_id, l.id, l.ran_at, l.partial, m[1]::date AS d
          FROM lines l,
               regexp_matches(l.error,
                              '(?:: |; )[^ ;]+ ([0-9]{4}-[0-9]{2}-[0-9]{2})',
                              'g') AS m
         WHERE pg_input_is_valid(m[1], 'date')),
    spans AS (
        SELECT tenant_id, ran_at, d - 3 AS lo, d + 3 AS hi
          FROM entries WHERE NOT partial
        UNION ALL
        SELECT tenant_id, max(ran_at), min(d) - 3, max(d) + 3
          FROM entries WHERE partial
         GROUP BY tenant_id, id),
    hit AS (
        SELECT DISTINCT t.tenant_id, t.id
          FROM transactions t
          JOIN spans s ON s.tenant_id = t.tenant_id
                      AND t.date BETWEEN s.lo AND s.hi
         WHERE t.removed = 0
           AND t.id LIKE ANY (ARRAY[
               'pdf:%', 'csv:%', 'ofx:%', 'qif:%', 'mint:%',
               'ynab:%', 'monarch:%', 'copilot:%', 'simplifi:%'])
           AND (t.raw IS NULL OR jsonb_typeof(t.raw) = 'object')
           AND jsonb_typeof(t.raw->'_stood_in') IS DISTINCT FROM 'object'
           -- the row's first import came after the line was written
           AND NOT EXISTS (
               SELECT 1 FROM import_batches b
                WHERE b.tenant_id = t.tenant_id
                  AND b.id = COALESCE(
                        CASE WHEN jsonb_typeof(t.raw->'_batches') = 'array'
                             THEN t.raw->'_batches'->>0 END,
                        t.raw->>'_batch')
                  AND b.created_at > s.ran_at))
    UPDATE transactions t
       SET raw = COALESCE(t.raw, '{}'::jsonb) || jsonb_build_object(
             '_stood_in', jsonb_build_object('source', 'feed',
                                             'accounts', '[]'::jsonb))
      FROM hit
     WHERE t.tenant_id = hit.tenant_id AND t.id = hit.id;
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN
        RAISE NOTICE 'imported rows: % noted as standing in for a bank '
                     'feed''s row, from the import-overlap log', n;
    END IF;
END $$;
