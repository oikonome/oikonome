-- A folder import's outcomes settled at submit (an archive's restore, a file
-- the plan skipped) are a list, and the job's status reads them as one. Say
-- so to the database, like every other jsonb column that defaults to '[]',
-- so that any SQL that later walks the column with jsonb_array_elements can
-- rely on the shape instead of aborting its whole statement on one bad row.
--
-- A new file rather than an edit of the migration that made the table:
-- instances that already ran that one would never see the constraint.
-- Dropped first so a re-run is a no-op.
ALTER TABLE bulk_import_jobs
    DROP CONSTRAINT IF EXISTS bulk_import_jobs_notes_is_a_list;
ALTER TABLE bulk_import_jobs
    ADD CONSTRAINT bulk_import_jobs_notes_is_a_list
    CHECK (jsonb_typeof(notes) = 'array');
