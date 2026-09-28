-- A full account, loan or routing number never belongs in a plaintext
-- `raw` column: raw leaves the instance verbatim through the export and the
-- portability archive. The ingest paths have kept them out for a while —
-- the MX account pull drops account_number and routing_number, the Plaid
-- liabilities pull drops the loan's account_number, the CSV importers cut
-- identifier-named columns to their last 4 and the OFX importer cuts its
-- statement account hint the same way — but each rule only covered rows
-- written after it. This applies the same cuts, once, to the rows already
-- stored. A restore applies them to archives taken before (sync/restore.py
-- scrub_*_raw); keep the three in step.
--
-- Idempotent: every cut leaves a value it would not cut again, and only
-- rows that actually change are written.

UPDATE accounts
   SET raw = raw - 'account_number' - 'routing_number'
 WHERE jsonb_typeof(raw) = 'object'
   AND raw ?| ARRAY['account_number', 'routing_number'];

UPDATE liabilities
   SET raw = raw - 'account_number'
 WHERE jsonb_typeof(raw) = 'object'
   AND raw ? 'account_number';

-- A source column counts as an identifier by its HEADER, compared the way
-- csvimport._norm_header compares it: lower-cased, '#' spelled out as
-- "number", every run of punctuation flattened to one space. The list is
-- csvimport._ID_HEADERS verbatim.
CREATE OR REPLACE FUNCTION pg_temp.raw_id_header(k text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM unnest(ARRAY[
            'account number', 'account no', 'account num', 'account nbr',
            'acct number', 'acct no', 'acct num', 'acct nbr', 'acctno',
            'acctnum', 'accountnumber', 'bank account', 'routing',
            'aba number', 'iban', 'sort code', 'card number', 'card no',
            'card num', 'cardnumber', 'member number', 'member no']) t
         WHERE position(t IN btrim(regexp_replace(
                   replace(lower(k), '#', ' number '),
                   '[^a-z0-9]+', ' ', 'g'))) > 0)
$$;

CREATE OR REPLACE FUNCTION pg_temp.raw_id_scrub(r jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
    SELECT COALESCE(jsonb_object_agg(e.key, CASE
        WHEN jsonb_typeof(e.value) IN ('string', 'number')
             AND e.value #>> '{}' <> ''
             AND pg_temp.raw_id_header(e.key)
        THEN to_jsonb(right(regexp_replace(e.value #>> '{}',
                                           '^\s+|\s+$', '', 'g'), 4))
        WHEN e.key = 'acct'
             AND jsonb_typeof(e.value) IN ('string', 'number')
             AND length(e.value #>> '{}') > 4
        THEN to_jsonb(right(e.value #>> '{}', 4))
        ELSE e.value END), '{}'::jsonb)
      FROM jsonb_each(r) e
$$;

UPDATE transactions
   SET raw = pg_temp.raw_id_scrub(raw)
 WHERE jsonb_typeof(raw) = 'object'
   AND EXISTS (SELECT 1 FROM jsonb_object_keys(raw) k
                WHERE k = 'acct' OR pg_temp.raw_id_header(k))
   AND raw IS DISTINCT FROM pg_temp.raw_id_scrub(raw);
