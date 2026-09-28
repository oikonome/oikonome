-- The merchant alias (merchant_canonical) and the rename journal
-- (merchant_renames) are keyed on a row's identity string
-- COALESCE(merchant_outlet, merchant_name, name) (engine/merchant_sql.py
-- RAW_KEY). Migration 153 cut the full account and card numbers out of
-- file-imported names, so the alias and the journal are cut in step here:
-- otherwise the full number survives in two exported tables and the alias
-- stops joining the rows it was made for. An alias moves to its masked key
-- only when a file-import row now carries that key, and its full-number
-- spelling is dropped only when no row still keys on it (an aggregator row
-- keeps its name, and so keeps its alias). Two raws that collapse onto one
-- masked key keep one alias: manual over llm over layer1 over none, then
-- the newest — an alias already on the masked key is replaced only by one
-- that ranks better, so a person's rename is never lost to a guess.
--
-- Own file, not appended to 153: an instance that had recorded 153 before
-- these statements existed would never run them. Idempotent: a second
-- run finds nothing with eight digits left to move.

INSERT INTO merchant_canonical (tenant_id, raw_merchant, canonical, method,
                                as_of, merchant_id)
SELECT DISTINCT ON (s.tenant_id, s.masked)
       s.tenant_id, s.masked, s.canonical, s.method, s.as_of, s.merchant_id
  FROM (SELECT mc.*,
               regexp_replace(mc.raw_merchant, '\d{4,}(\d{4})', '…\1',
                              'g') AS masked
          FROM merchant_canonical mc
         WHERE mc.raw_merchant ~ '\d{8}') s
 WHERE EXISTS (
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
   SET raw_merchant = regexp_replace(r.raw_merchant, '\d{4,}(\d{4})',
                                     '…\1', 'g')
 WHERE r.raw_merchant ~ '\d{8}'
   AND EXISTS (
        SELECT 1 FROM merchant_canonical mc
         WHERE mc.tenant_id = r.tenant_id
           AND mc.raw_merchant = regexp_replace(r.raw_merchant,
                                    '\d{4,}(\d{4})', '…\1', 'g'))
   AND NOT EXISTS (
        SELECT 1 FROM transactions t
         WHERE t.tenant_id = r.tenant_id
           AND COALESCE(t.merchant_outlet, t.merchant_name, t.name)
               = r.raw_merchant);

DELETE FROM merchant_canonical mc
 WHERE mc.raw_merchant ~ '\d{8}'
   AND EXISTS (
        SELECT 1 FROM merchant_canonical m2
         WHERE m2.tenant_id = mc.tenant_id
           AND m2.raw_merchant = regexp_replace(mc.raw_merchant,
                                    '\d{4,}(\d{4})', '…\1', 'g'))
   AND NOT EXISTS (
        SELECT 1 FROM transactions t
         WHERE t.tenant_id = mc.tenant_id
           AND COALESCE(t.merchant_outlet, t.merchant_name, t.name)
               = mc.raw_merchant);
