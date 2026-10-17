-- A hand-made account's name keeps only the last four of any run of
-- eight or more digits. A folder import names a new account after its
-- file, and banks name their exports after the full account number
-- ("Checking1234567890123.csv"); the import cuts that number from the
-- name now, but accounts named before it did still show the whole number
-- on every page and carry it out through /export.
-- Same cut as base.py mask_long_digits, once, to the names already stored;
-- restore applies it to every name an archive brings.
--
-- The account's id is left alone: re-keying an account means following
-- every lineage and adoption record that names it. The restore notes list
-- the ids that still hold such a number, and the importing guide says how
-- to be rid of one.
--
-- Bounded and idempotent: only names holding a run of eight or more
-- digits are written, and a masked run is "…" plus four digits, which no
-- longer matches. The greedy \d{4,} leaves exactly the run's last four in
-- the group, the same result as the Python rule.

UPDATE accounts
   SET name = regexp_replace(name, '\d{4,}(\d{4})', '…\1', 'g')
 WHERE item_id = 'manual' AND name ~ '\d{8}';
