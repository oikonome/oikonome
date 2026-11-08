-- An imported file's name is kept in import_batches.filename for years:
-- Recent imports lists it, /export and the portability archive carry it.
-- Migrations 188, 203 and 206 cut the card numbers it spelled with the
-- grammar's separators (space, dot, slash, dash) and left a name whose
-- groups an underscore or a comma separates ("Visa_4000_0000_0000_0002.csv")
-- whole. batches.create and the restore now cut a file name the way an
-- account id's slug reads it (card_number.cut_as_slug_reads); this cuts
-- what earlier builds stored.
--
-- The cut runs in Python, as db/migrate.py's PYTHON_STEPS step for this
-- file, in this file's transaction: the slug reading is the Python
-- grammar's, and a migration hands no stored text to the SQL cut
-- (sync/card_number.py). So this file changes no schema and no row; it is
-- the record that the step ran. Idempotent: a cut number is "…" and four
-- digits, which no cut changes again, and a row is written only if it
-- still reads as read.

SELECT 1;
