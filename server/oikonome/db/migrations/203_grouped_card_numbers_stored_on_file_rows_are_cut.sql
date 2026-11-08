-- A file row keeps only the last four of a card number printed in groups
-- ("AUTOPAY 4111 1111 1111 1111"), as it does of an unbroken run. sync/base.py
-- mask_account_name applies that cut to new rows and restore to older
-- archives; migrations 153, 154, 178, 181 and 188 only knew unbroken runs, so
-- rows already stored kept the whole number in their name, merchant and raw
-- cells, and it left the instance through /export, the portability archive,
-- the categorizer's prompt and every member's ledger. Same cut, once, to what
-- is already stored. Keep the three in step: the prefixes are
-- FILE_IMPORT_ID_PREFIXES, the raw keys _FREE_TEXT_KEYS, _OVERFLOW_KEYS,
-- _SPLIT_SOURCE_KEYS and _FREE_TEXT_WORDS, the pattern GROUPED_DIGITS_RE
-- followed by LONG_DIGITS_RE, in that order.
--
-- Aggregator rows are not touched: their name and raw are the feed's own
-- record and every sync restates them.
--
-- Row ids are not touched either. A row whose id was hashed over the whole
-- number no longer has a name that explains it once the name is cut, so the
-- keying pass that runs after every migration (migrate.key_numbered_file_rows,
-- base.key_file_import_ids) finds it by the cut mark and re-ids it with every
-- reference carried; a re-import of the same file recomputes the old id and
-- finds the row under its keyed form.
--
-- The merchant alias, the rename journal and a category rule are keyed on the
-- row's identity string COALESCE(merchant_outlet, merchant_name, name), so they
-- follow it as in migration 154: a key moves to its cut spelling only when a
-- file row now carries that spelling, and the whole spelling goes only when no
-- row still keys on it (an aggregator row keeps its name, and so its alias).
-- Two whole keys that collapse onto one cut key keep one row, a person's over
-- a machine's, so a person's rename or rule is never lost to a guess. The
-- `canonical` values are left: the resolver's own never hold digits, and a
-- person's are what they typed.
--
-- Bounded and idempotent: only values the cut changes are written, and a cut
-- number is "…" plus four digits, which matches nothing again.

CREATE OR REPLACE FUNCTION pg_temp.cut_numbers(v text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT regexp_replace(
        regexp_replace(
            v,
            '(?<!\d)(?:\d{4}([ -])(?:\d{4}\1){2,}(\d{4})|\d{4}([ -])\d{6}\3\d(\d{4}))(?!\d)',
            '…\2\4', 'g'),
        '\d{4,}(\d{4})', '…\1', 'g')
$$;

-- cheap, and blind to a mismatched separator; what it selects is compared with
-- the real cut before anything is written
CREATE OR REPLACE FUNCTION pg_temp.may_hold_grouped(v text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT v ~ '\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}|\d{4}[ -]\d{6}[ -]\d{5}'
$$;

CREATE OR REPLACE FUNCTION pg_temp.is_free_text_key(k text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT lower(btrim(k, E' \t\r\n'))
               IN ('name', 'memo', 'note', 'notes', 'payee', 'description',
                   'original description', 'original statement', '', 'null',
                   'account', 'account name')
        OR lower(k) ~ '(memo|note|description|details|payee|narrative|particulars)'
$$;

CREATE OR REPLACE FUNCTION pg_temp.is_row_text(v text, name text, merchant text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT lower(pg_temp.cut_numbers(btrim(v, E' \t\r\n')))
           IN (lower(pg_temp.cut_numbers(btrim(name, E' \t\r\n'))),
               lower(pg_temp.cut_numbers(btrim(merchant, E' \t\r\n'))))
$$;

CREATE OR REPLACE FUNCTION pg_temp.cut_file_raw(raw jsonb, name text,
                                                merchant text)
RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
    SELECT jsonb_object_agg(
             e.key,
             CASE
               WHEN jsonb_typeof(e.value) = 'string'
                AND (e.key = 'line' OR pg_temp.is_free_text_key(e.key)
                     OR pg_temp.is_row_text(e.value #>> '{}', name, merchant))
               THEN to_jsonb(pg_temp.cut_numbers(e.value #>> '{}'))
               WHEN jsonb_typeof(e.value) = 'array'
               THEN COALESCE((
                    SELECT jsonb_agg(
                             CASE
                               WHEN jsonb_typeof(a.v) = 'string'
                                AND (pg_temp.is_free_text_key(e.key)
                                     OR pg_temp.is_row_text(a.v #>> '{}', name,
                                                            merchant))
                               THEN to_jsonb(pg_temp.cut_numbers(a.v #>> '{}'))
                               ELSE a.v END
                             ORDER BY a.n)
                      FROM jsonb_array_elements(e.value)
                           WITH ORDINALITY AS a(v, n)), '[]'::jsonb)
               ELSE e.value END)
      FROM jsonb_each(raw) e
$$;

-- raw first: a cell is matched to the row's name as it stood when it was read
UPDATE transactions t
   SET raw = pg_temp.cut_file_raw(t.raw, t.name, t.merchant_name)
 WHERE split_part(t.id, ':', 1) IN ('pdf', 'csv', 'ofx', 'qif', 'mint', 'ynab',
                                    'monarch', 'copilot', 'simplifi')
   AND jsonb_typeof(t.raw) = 'object'
   AND pg_temp.may_hold_grouped(t.raw::text)
   AND pg_temp.cut_file_raw(t.raw, t.name, t.merchant_name) <> t.raw;

UPDATE transactions
   SET name = pg_temp.cut_numbers(name),
       merchant_name = pg_temp.cut_numbers(merchant_name),
       merchant_name_set_aside = pg_temp.cut_numbers(merchant_name_set_aside),
       merchant_outlet = pg_temp.cut_numbers(merchant_outlet)
 WHERE split_part(id, ':', 1) IN ('pdf', 'csv', 'ofx', 'qif', 'mint', 'ynab',
                                  'monarch', 'copilot', 'simplifi')
   AND (pg_temp.may_hold_grouped(name)
        OR pg_temp.may_hold_grouped(merchant_name)
        OR pg_temp.may_hold_grouped(merchant_name_set_aside)
        OR pg_temp.may_hold_grouped(merchant_outlet))
   AND (pg_temp.cut_numbers(name), pg_temp.cut_numbers(merchant_name),
        pg_temp.cut_numbers(merchant_name_set_aside),
        pg_temp.cut_numbers(merchant_outlet))
       IS DISTINCT FROM (name, merchant_name, merchant_name_set_aside,
                         merchant_outlet);

UPDATE import_batches
   SET filename = pg_temp.cut_numbers(filename)
 WHERE pg_temp.may_hold_grouped(filename)
   AND pg_temp.cut_numbers(filename) <> filename;

INSERT INTO merchant_canonical (tenant_id, raw_merchant, canonical, method,
                                as_of, merchant_id)
SELECT DISTINCT ON (s.tenant_id, s.masked)
       s.tenant_id, s.masked, s.canonical, s.method, s.as_of, s.merchant_id
  FROM (SELECT mc.*, pg_temp.cut_numbers(mc.raw_merchant) AS masked
          FROM merchant_canonical mc
         WHERE pg_temp.may_hold_grouped(mc.raw_merchant)) s
 WHERE s.masked <> s.raw_merchant
   AND EXISTS (
        SELECT 1 FROM transactions t
         WHERE t.tenant_id = s.tenant_id
           AND split_part(t.id, ':', 1) IN ('pdf', 'csv', 'ofx', 'qif',
                    'mint', 'ynab', 'monarch', 'copilot', 'simplifi')
           AND COALESCE(t.merchant_outlet, t.merchant_name, t.name)
               = s.masked)
 ORDER BY s.tenant_id, s.masked,
          CASE s.method WHEN 'manual' THEN 0 WHEN 'llm' THEN 1
                        WHEN 'layer1' THEN 2 ELSE 3 END,
          s.as_of DESC
ON CONFLICT (tenant_id, raw_merchant) DO UPDATE
   SET canonical = EXCLUDED.canonical, method = EXCLUDED.method,
       as_of = EXCLUDED.as_of, merchant_id = EXCLUDED.merchant_id
 WHERE (CASE merchant_canonical.method WHEN 'manual' THEN 0 WHEN 'llm' THEN 1
                                       WHEN 'layer1' THEN 2 ELSE 3 END)
     > (CASE EXCLUDED.method WHEN 'manual' THEN 0 WHEN 'llm' THEN 1
                             WHEN 'layer1' THEN 2 ELSE 3 END);

UPDATE merchant_renames r
   SET raw_merchant = pg_temp.cut_numbers(r.raw_merchant)
 WHERE pg_temp.may_hold_grouped(r.raw_merchant)
   AND pg_temp.cut_numbers(r.raw_merchant) <> r.raw_merchant
   AND EXISTS (
        SELECT 1 FROM merchant_canonical mc
         WHERE mc.tenant_id = r.tenant_id
           AND mc.raw_merchant = pg_temp.cut_numbers(r.raw_merchant))
   AND NOT EXISTS (
        SELECT 1 FROM transactions t
         WHERE t.tenant_id = r.tenant_id
           AND COALESCE(t.merchant_outlet, t.merchant_name, t.name)
               = r.raw_merchant);

DELETE FROM merchant_canonical mc
 WHERE pg_temp.may_hold_grouped(mc.raw_merchant)
   AND pg_temp.cut_numbers(mc.raw_merchant) <> mc.raw_merchant
   AND EXISTS (
        SELECT 1 FROM merchant_canonical m2
         WHERE m2.tenant_id = mc.tenant_id
           AND m2.raw_merchant = pg_temp.cut_numbers(mc.raw_merchant))
   AND NOT EXISTS (
        SELECT 1 FROM transactions t
         WHERE t.tenant_id = mc.tenant_id
           AND COALESCE(t.merchant_outlet, t.merchant_name, t.name)
               = mc.raw_merchant);

INSERT INTO merchant_categories (tenant_id, merchant, category_primary, source,
                                 disabled, classified_at)
SELECT DISTINCT ON (s.tenant_id, s.masked)
       s.tenant_id, s.masked, s.category_primary, s.source, s.disabled,
       s.classified_at
  FROM (SELECT m.*, pg_temp.cut_numbers(m.merchant) AS masked
          FROM merchant_categories m
         WHERE pg_temp.may_hold_grouped(m.merchant)) s
 WHERE s.masked <> s.merchant
   AND EXISTS (
        SELECT 1 FROM transactions t
         WHERE t.tenant_id = s.tenant_id
           AND split_part(t.id, ':', 1) IN ('pdf', 'csv', 'ofx', 'qif',
                    'mint', 'ynab', 'monarch', 'copilot', 'simplifi')
           AND COALESCE(t.merchant_outlet, t.merchant_name, t.name)
               = s.masked)
 ORDER BY s.tenant_id, s.masked, (s.source <> 'user'), s.classified_at DESC
ON CONFLICT (tenant_id, merchant) DO UPDATE
   SET category_primary = EXCLUDED.category_primary,
       source = EXCLUDED.source, disabled = EXCLUDED.disabled,
       classified_at = EXCLUDED.classified_at
 WHERE merchant_categories.source <> 'user' AND EXCLUDED.source = 'user';

DELETE FROM merchant_categories m
 WHERE pg_temp.may_hold_grouped(m.merchant)
   AND pg_temp.cut_numbers(m.merchant) <> m.merchant
   AND EXISTS (
        SELECT 1 FROM merchant_categories m2
         WHERE m2.tenant_id = m.tenant_id
           AND m2.merchant = pg_temp.cut_numbers(m.merchant))
   AND NOT EXISTS (
        SELECT 1 FROM transactions t
         WHERE t.tenant_id = m.tenant_id
           AND COALESCE(t.merchant_outlet, t.merchant_name, t.name)
               = m.merchant);
