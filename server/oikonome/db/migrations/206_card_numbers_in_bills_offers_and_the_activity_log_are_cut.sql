-- Migrations 203 and 204 cut a card number out of file rows and merchant
-- names with a frozen copy of an early cut, and two places kept the number
-- whole: the bills and offers detection named from those rows before the
-- cut, and the activity log's label and sentence, which quote a payee or a
-- row as it read on the day. The bill pass and the log now cut what they
-- write (bills._offer_text, activity.record); this cuts what they stored,
-- and runs 203's and 204's statements again with the cut sync.card_number
-- now defines, which also reads the layouts the frozen cut missed (dots,
-- slashes, doubled and mixed separators, a run that shows once an
-- unbroken run in front of it is cut). cut_numbers below is
-- card_number.sql_cut_function() pasted whole and may_hold_number is
-- card_number.sql_selector(); tests/test_card_number_sql_matches_python.py
-- fails when the module moves away from them.
--
-- A bill is cut unless a person made it (raw.source 'manual'): its id is
-- the slug of its payee, so the id moves with the name, and what names the
-- bill by id or by payee moves with it, as bills.rename_bill moves them —
-- the bill's category marks, the offers that name it (their payee, or the
-- evidence's bill_payee for a fee or a merchant to join it), and the two
-- settings keyed on its payee (disabled_bills, occurrence_caps). A bill
-- whose cut name or id another bill already holds is left as it is: two
-- bills are never made one here. The month snapshots keep the schedule
-- they froze, names included; they are read only against the config
-- frozen beside them.
--
-- An offer's own label is cut where it is the ledger's spelling of a
-- series (a bill to add, a fee to attach) and no bill is named by it, as
-- is the Plaid stream's description in its evidence. An offer to join a
-- merchant to a bill names the merchant by its stored name, and follows
-- it only where a live merchant now holds the cut name and none the whole
-- one (204 cut the machine's names and left a person's).
--
-- Bounded and idempotent: only values the cut changes are written, and a
-- cut number is "…" plus four digits, which no cut changes again.

CREATE OR REPLACE FUNCTION pg_temp.cut_numbers(v text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT regexp_replace(regexp_replace(regexp_replace(regexp_replace(v, '…?(?<!\d)(?:\d{4}[ ./-]{1,3}\d{4}[ ./-]{1,3}\d{4}([ ./-]{1,3})(?:\d{4}\1)*(\d{4})|\d{4}[ ./-]{1,3}\d{6}[ ./-]{1,3}\d(\d{4}))(?!\d)', '…\2\3', 'g'), '\d{4,}(\d{4})', '…\1', 'g'), '…?(?<!\d)(?:\d{4}[ ./-]{1,3}\d{4}[ ./-]{1,3}\d{4}([ ./-]{1,3})(?:\d{4}\1)*(\d{4})|\d{4}[ ./-]{1,3}\d{6}[ ./-]{1,3}\d(\d{4}))(?!\d)', '…\2\3', 'g'), '\d{4,}(\d{4})', '…\1', 'g')
$$;

-- cheap, and wider than the cut; what it selects is compared with the
-- real cut before anything is written
CREATE OR REPLACE FUNCTION pg_temp.may_hold_number(v text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT v ~ '\d{8}|\d{4}[ ./-]{1,3}\d{4}[ ./-]{1,3}\d{4}[ ./-]{1,3}\d{4}|\d{4}[ ./-]{1,3}\d{6}[ ./-]{1,3}\d{5}'
$$;

CREATE OR REPLACE FUNCTION pg_temp.is_cut(v text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT pg_temp.may_hold_number(v) AND pg_temp.cut_numbers(v) <> v
$$;

-- bills._slug of a payee: what a bill's id is
CREATE OR REPLACE FUNCTION pg_temp.bill_id(payee text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT 'rec:' || btrim(regexp_replace(lower(COALESCE(payee, '')),
                                          '[^a-z0-9]+', '-', 'g'), '-')
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

-- a person named this merchant, on the row or through an alias
CREATE OR REPLACE FUNCTION pg_temp.persons_name(tid uuid, src text, nm text)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT src = 'manual' OR EXISTS (
        SELECT 1 FROM merchant_canonical a
         WHERE a.tenant_id = tid AND a.method = 'manual'
           AND lower(a.canonical) = lower(nm))
$$;

-- A rule reaches its rows through its key's alias, or else through a live
-- merchant of that name, and the second kind stops matching the moment the
-- merchant's name changes: moved first, as _rekey_rules moves them. A key with
-- an alias stays with its alias, and a person's merchant keeps its rule.
CREATE OR REPLACE FUNCTION pg_temp.rule_follows(tid uuid, key text)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT NOT EXISTS (SELECT 1 FROM merchant_canonical a
                        WHERE a.tenant_id = tid AND a.raw_merchant = key)
       AND EXISTS (SELECT 1 FROM merchants m
                    WHERE m.tenant_id = tid AND m.merged_into IS NULL
                      AND lower(m.name) = lower(key))
       AND NOT EXISTS (SELECT 1 FROM merchants m
                        WHERE m.tenant_id = tid AND m.merged_into IS NULL
                          AND lower(m.name) = lower(key)
                          AND pg_temp.persons_name(m.tenant_id, m.name_source,
                                                   m.name))
$$;

-- ---- 203 again, with the cut above: file rows, their imports, and the
-- ---- aliases, journal and rules keyed on their names

-- raw first: a cell is matched to the row's name as it stood when it was read
UPDATE transactions t
   SET raw = pg_temp.cut_file_raw(t.raw, t.name, t.merchant_name)
 WHERE split_part(t.id, ':', 1) IN ('pdf', 'csv', 'ofx', 'qif', 'mint', 'ynab',
                                    'monarch', 'copilot', 'simplifi')
   AND jsonb_typeof(t.raw) = 'object'
   AND pg_temp.may_hold_number(t.raw::text)
   AND pg_temp.cut_file_raw(t.raw, t.name, t.merchant_name) <> t.raw;

UPDATE transactions
   SET name = pg_temp.cut_numbers(name),
       merchant_name = pg_temp.cut_numbers(merchant_name),
       merchant_name_set_aside = pg_temp.cut_numbers(merchant_name_set_aside),
       merchant_outlet = pg_temp.cut_numbers(merchant_outlet)
 WHERE split_part(id, ':', 1) IN ('pdf', 'csv', 'ofx', 'qif', 'mint', 'ynab',
                                  'monarch', 'copilot', 'simplifi')
   AND (pg_temp.may_hold_number(name)
        OR pg_temp.may_hold_number(merchant_name)
        OR pg_temp.may_hold_number(merchant_name_set_aside)
        OR pg_temp.may_hold_number(merchant_outlet))
   AND (pg_temp.cut_numbers(name), pg_temp.cut_numbers(merchant_name),
        pg_temp.cut_numbers(merchant_name_set_aside),
        pg_temp.cut_numbers(merchant_outlet))
       IS DISTINCT FROM (name, merchant_name, merchant_name_set_aside,
                         merchant_outlet);

UPDATE import_batches
   SET filename = pg_temp.cut_numbers(filename)
 WHERE pg_temp.may_hold_number(filename)
   AND pg_temp.cut_numbers(filename) <> filename;

INSERT INTO merchant_canonical (tenant_id, raw_merchant, canonical, method,
                                as_of, merchant_id)
SELECT DISTINCT ON (s.tenant_id, s.masked)
       s.tenant_id, s.masked, s.canonical, s.method, s.as_of, s.merchant_id
  FROM (SELECT mc.*, pg_temp.cut_numbers(mc.raw_merchant) AS masked
          FROM merchant_canonical mc
         WHERE pg_temp.may_hold_number(mc.raw_merchant)) s
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
 WHERE pg_temp.may_hold_number(r.raw_merchant)
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
 WHERE pg_temp.may_hold_number(mc.raw_merchant)
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
         WHERE pg_temp.may_hold_number(m.merchant)) s
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
 WHERE pg_temp.may_hold_number(m.merchant)
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

-- ---- 204 again: merchant names, their rules, 1099 marks, the journal

INSERT INTO merchant_categories (tenant_id, merchant, category_primary, source,
                                 disabled, classified_at)
SELECT DISTINCT ON (s.tenant_id, s.masked)
       s.tenant_id, s.masked, s.category_primary, s.source, s.disabled,
       s.classified_at
  FROM (SELECT r.*, pg_temp.cut_numbers(m.name) AS masked
          FROM merchant_categories r
          JOIN merchants m
            ON m.tenant_id = r.tenant_id AND m.merged_into IS NULL
           AND lower(m.name) = lower(r.merchant)
         WHERE pg_temp.is_cut(r.merchant)
           AND pg_temp.rule_follows(r.tenant_id, r.merchant)) s
 ORDER BY s.tenant_id, s.masked, s.disabled, (s.source <> 'user'),
          s.classified_at DESC
ON CONFLICT (tenant_id, merchant) DO UPDATE
   SET category_primary = EXCLUDED.category_primary,
       source = EXCLUDED.source, disabled = EXCLUDED.disabled,
       classified_at = EXCLUDED.classified_at
 WHERE merchant_categories.source <> 'user' AND EXCLUDED.source = 'user';

DELETE FROM merchant_categories r
 WHERE pg_temp.is_cut(r.merchant)
   AND pg_temp.rule_follows(r.tenant_id, r.merchant);

-- A 1099 mark's name follows its merchant's; a name no merchant carries is what
-- a person typed on the mark.
WITH follows AS (
    SELECT DISTINCT ON (v.tenant_id, v.entity_id, pg_temp.cut_numbers(v.merchant))
           v.tenant_id, v.id, pg_temp.cut_numbers(v.merchant) AS masked
      FROM vendor_1099 v
     WHERE pg_temp.is_cut(v.merchant)
       AND EXISTS (
            SELECT 1 FROM merchants m
             WHERE m.tenant_id = v.tenant_id
               AND NOT pg_temp.persons_name(m.tenant_id, m.name_source, m.name)
               AND (m.id = v.merchant_id
                    OR (v.merchant_id IS NULL AND m.merged_into IS NULL
                        AND lower(m.name) = lower(v.merchant))))
       AND NOT EXISTS (
            SELECT 1 FROM vendor_1099 o
             WHERE o.tenant_id = v.tenant_id AND o.entity_id = v.entity_id
               AND o.merchant = pg_temp.cut_numbers(v.merchant))
     ORDER BY v.tenant_id, v.entity_id, pg_temp.cut_numbers(v.merchant),
              v.created_at, v.id)
UPDATE vendor_1099 v
   SET merchant = f.masked
  FROM follows f
 WHERE v.tenant_id = f.tenant_id AND v.id = f.id;

-- The journal, whose from_canonical is what an undo restores. A re-clean's split
-- journals a machine's name as the result, and undo compares that with the live
-- alias, so it is cut while the alias still reads the same. Then a `from` a
-- journal row does not name as somebody's result is a machine's.
UPDATE merchant_renames r
   SET to_canonical = pg_temp.cut_numbers(r.to_canonical)
 WHERE pg_temp.is_cut(r.to_canonical)
   AND EXISTS (
        SELECT 1 FROM merchant_canonical a
         WHERE a.tenant_id = r.tenant_id AND a.raw_merchant = r.raw_merchant
           AND a.method <> 'manual' AND a.canonical = r.to_canonical);

UPDATE merchant_renames r
   SET from_canonical = pg_temp.cut_numbers(r.from_canonical)
 WHERE pg_temp.is_cut(r.from_canonical)
   AND NOT EXISTS (
        SELECT 1 FROM merchant_renames o
         WHERE o.tenant_id = r.tenant_id
           AND o.to_canonical = r.from_canonical);

UPDATE merchants m
   SET name = pg_temp.cut_numbers(m.name), updated_at = now()
 WHERE pg_temp.is_cut(m.name)
   AND NOT pg_temp.persons_name(m.tenant_id, m.name_source, m.name);

UPDATE merchant_canonical a
   SET canonical = pg_temp.cut_numbers(a.canonical)
 WHERE a.method <> 'manual'
   AND pg_temp.is_cut(a.canonical);

-- ---- bills, and what names them

DROP TABLE IF EXISTS pg_temp.bill_moves;
CREATE TEMP TABLE bill_moves AS
SELECT DISTINCT ON (s.tenant_id, s.new_id)
       s.tenant_id, s.id AS old_id, s.payee AS old_payee, s.new_payee, s.new_id
  FROM (SELECT b.tenant_id, b.id, b.payee,
               pg_temp.cut_numbers(b.payee) AS new_payee,
               pg_temp.bill_id(pg_temp.cut_numbers(b.payee)) AS new_id
          FROM bills b
         WHERE b.raw->>'source' IS DISTINCT FROM 'manual'
           AND pg_temp.is_cut(b.payee)) s
 WHERE NOT EXISTS (
        SELECT 1 FROM bills o
         WHERE o.tenant_id = s.tenant_id AND o.id <> s.id
           AND (o.id = s.new_id OR o.payee = s.new_payee))
 ORDER BY s.tenant_id, s.new_id, s.id;

UPDATE bills b
   SET id = m.new_id, payee = m.new_payee, synced_at = now()
  FROM bill_moves m
 WHERE b.tenant_id = m.tenant_id AND b.id = m.old_id;

UPDATE manual_categories c
   SET bill_id = m.new_id
  FROM bill_moves m
 WHERE c.tenant_id = m.tenant_id AND c.bill_id = m.old_id;

-- every offer, decided ones too: a decided offer's name is read against
-- its bill's (an approved removal is what tells a bill the pass retired
-- from one the household declined)
UPDATE bill_proposals p
   SET payee = m.new_payee
  FROM bill_moves m
 WHERE p.tenant_id = m.tenant_id AND p.payee = m.old_payee
   AND COALESCE(p.kind, '') NOT IN ('attach', 'merchant');

UPDATE bill_proposals p
   SET evidence = p.evidence || jsonb_build_object('bill_payee', m.new_payee)
  FROM bill_moves m
 WHERE p.tenant_id = m.tenant_id
   AND p.kind IN ('attach', 'merchant')
   AND jsonb_typeof(p.evidence) = 'object'
   AND p.evidence->>'bill_payee' = m.old_payee;

UPDATE tenant_settings s
   SET config = s.config || jsonb_build_object('disabled_bills', (
           SELECT jsonb_agg(COALESCE(to_jsonb(m.new_payee), d.v) ORDER BY d.n)
             FROM jsonb_array_elements(s.config->'disabled_bills')
                  WITH ORDINALITY AS d(v, n)
             LEFT JOIN bill_moves m
               ON m.tenant_id = s.tenant_id AND to_jsonb(m.old_payee) = d.v)),
       updated_at = now()
 WHERE jsonb_typeof(s.config->'disabled_bills') = 'array'
   AND EXISTS (SELECT 1 FROM bill_moves m
                WHERE m.tenant_id = s.tenant_id
                  AND s.config->'disabled_bills' @> jsonb_build_array(m.old_payee));

UPDATE tenant_settings s
   SET config = s.config || jsonb_build_object('occurrence_caps', (
           SELECT jsonb_object_agg(COALESCE(m.new_payee, e.key), e.value)
             FROM jsonb_each(s.config->'occurrence_caps') e
             LEFT JOIN bill_moves m
               ON m.tenant_id = s.tenant_id AND m.old_payee = e.key)),
       updated_at = now()
 WHERE jsonb_typeof(s.config->'occurrence_caps') = 'object'
   AND EXISTS (SELECT 1 FROM bill_moves m
                WHERE m.tenant_id = s.tenant_id
                  AND s.config->'occurrence_caps' ? m.old_payee);

DROP TABLE pg_temp.bill_moves;

-- ---- offers' own labels

-- the ledger's spelling of a series, and a payee no bill holds any more:
-- nothing finds a bill by it
UPDATE bill_proposals p
   SET payee = pg_temp.cut_numbers(p.payee)
 WHERE COALESCE(p.kind, '') <> 'merchant'
   AND pg_temp.is_cut(p.payee)
   AND NOT EXISTS (SELECT 1 FROM bills b
                    WHERE b.tenant_id = p.tenant_id AND b.payee = p.payee);

UPDATE bill_proposals p
   SET evidence = p.evidence || jsonb_build_object(
           'bill_payee', pg_temp.cut_numbers(p.evidence->>'bill_payee'))
 WHERE p.kind IN ('attach', 'merchant')
   AND jsonb_typeof(p.evidence) = 'object'
   AND pg_temp.is_cut(p.evidence->>'bill_payee')
   AND NOT EXISTS (SELECT 1 FROM bills b
                    WHERE b.tenant_id = p.tenant_id
                      AND b.payee = p.evidence->>'bill_payee');

UPDATE bill_proposals p
   SET evidence = p.evidence || jsonb_build_object(
           'description', pg_temp.cut_numbers(p.evidence->>'description'))
 WHERE jsonb_typeof(p.evidence) = 'object'
   AND jsonb_typeof(p.evidence->'description') = 'string'
   AND pg_temp.is_cut(p.evidence->>'description');

UPDATE bill_proposals p
   SET payee = pg_temp.cut_numbers(p.payee),
       evidence = CASE
           WHEN jsonb_typeof(p.evidence) = 'object'
            AND jsonb_typeof(p.evidence->'merchant_name') = 'string'
           THEN p.evidence || jsonb_build_object(
                    'merchant_name',
                    pg_temp.cut_numbers(p.evidence->>'merchant_name'))
           ELSE p.evidence END
 WHERE p.kind = 'merchant'
   AND pg_temp.is_cut(p.payee)
   AND EXISTS (SELECT 1 FROM merchants m
                WHERE m.tenant_id = p.tenant_id AND m.merged_into IS NULL
                  AND lower(m.name) = lower(pg_temp.cut_numbers(p.payee)))
   AND NOT EXISTS (SELECT 1 FROM merchants m
                    WHERE m.tenant_id = p.tenant_id AND m.merged_into IS NULL
                      AND lower(m.name) = lower(p.payee));

-- ---- the activity log: its label and its sentence, never its ids

UPDATE activity_log a
   SET label = pg_temp.cut_numbers(a.label),
       summary = pg_temp.cut_numbers(a.summary)
 WHERE (pg_temp.may_hold_number(a.label)
        OR pg_temp.may_hold_number(a.summary))
   AND (pg_temp.cut_numbers(a.label), pg_temp.cut_numbers(a.summary))
       IS DISTINCT FROM (a.label, a.summary);
