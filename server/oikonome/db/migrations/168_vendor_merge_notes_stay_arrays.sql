-- The merge notes on a vendor mark are read as a list by vendor_totals and
-- by undo; a document of another shape would abort those queries, so the
-- column is pinned to an array the same way every other list column is.
ALTER TABLE vendor_1099
    DROP CONSTRAINT IF EXISTS vendor_1099_merged_from_is_array;
UPDATE vendor_1099 SET merged_from = '[]'::jsonb
 WHERE merged_from IS NULL OR jsonb_typeof(merged_from) IS DISTINCT FROM 'array';
ALTER TABLE vendor_1099
    ADD CONSTRAINT vendor_1099_merged_from_is_array
    CHECK (jsonb_typeof(merged_from) = 'array');
