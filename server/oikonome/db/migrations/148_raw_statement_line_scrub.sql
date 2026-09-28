-- The statement (PDF) importer masks any run of eight or more digits in the
-- line it keeps under raw.line to its last four (sync/pdfimport.py), so a
-- full account or card number printed on a statement never sits in
-- plaintext raw. Rows written before that rule still carry the full run;
-- migration 146 covered the other importers' identifier columns but not
-- this one. Same cut, once, to the rows already stored; restore applies it
-- to older archives (sync/restore.py scrub_transaction_raw). Keep the
-- three in step.
--
-- Idempotent: a masked run is "…" plus four digits, which no longer
-- matches, and only rows that change are written.

UPDATE transactions
   SET raw = jsonb_set(raw, '{line}',
                       to_jsonb(regexp_replace(raw ->> 'line',
                                               '\d{4,}(\d{4})', '…\1', 'g')))
 WHERE jsonb_typeof(raw) = 'object'
   AND jsonb_typeof(raw -> 'line') = 'string'
   AND (raw ->> 'line') ~ '\d{8}';
