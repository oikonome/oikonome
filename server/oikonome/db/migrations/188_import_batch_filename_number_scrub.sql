-- An import's recorded filename keeps only the last four of any run of
-- eight or more digits — sync/batches.py create applies base.py
-- mask_long_digits to every new record, and restore to every record an
-- archive brings. Banks name their exports after the full account
-- number ("Checking1234567890123.csv"), and import_batches.filename is
-- plaintext that leaves the instance through /export and the
-- portability archive. Same cut, once, to the records already stored.
--
-- Bounded and idempotent: only filenames holding a run of eight or more
-- digits are written, and a masked run is "…" plus four digits, which
-- no longer matches. The greedy \d{4,} leaves exactly the run's last
-- four in the group, the same result as the Python rule.

UPDATE import_batches
   SET filename = regexp_replace(filename, '\d{4,}(\d{4})', '…\1', 'g')
 WHERE filename ~ '\d{8}';
