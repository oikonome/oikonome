-- Venmo counterparties become their own merchants.
--
-- Aggregators normalize every Venmo row to name "Venmo" with no
-- merchant_name, while the bank's statement line — kept verbatim in
-- raw.original_description — names the actual counterparty:
--   VENMO *TAYLOR SAMPLE 7700 EASTPORT PARKWAY 8558124430, NY, US
--   VENMO *Taylor Sample NEW YORK, NY, US
-- So the ledger showed a wall of identical "Venmo" rows and every person
-- was invisible to grouping, search, and merchant totals.
--
-- sync/base.py (venmo_counterparty) now derives the merchant on ingest for
-- every future row; this applies the same transform to what is already
-- stored, whole history, all tenants. The steps mirror the Python parser:
--   1. take the text after "VENMO *",
--   2. cut the tail from the first digit run (Venmo's processing street
--      number / phone),
--   3. strip a trailing "<ST>, US" geo suffix,
--   4. when the remaining name is mixed-case, drop trailing ALL-CAPS city
--      words ("Taylor Sample NEW YORK" -> "Taylor Sample"),
--   5. title-case a name that is still ALL CAPS.
-- Deliberately narrow: only rows the aggregator left merchantless, only
-- "VENMO *" descriptors (bare "VENMO PAYMENT"/"VENMO CASHOUT" name nobody).
-- category fields and category_override untouched.
UPDATE transactions t
   SET merchant_name = x.counterparty
  FROM (
    SELECT tenant_id, id,
           CASE WHEN c ~ '[a-z]' THEN c ELSE INITCAP(c) END AS counterparty
      FROM (
        SELECT tenant_id, id,
               NULLIF(TRIM(BOTH ' ,' FROM
                 CASE WHEN base ~ '[a-z]'
                      THEN REGEXP_REPLACE(base, '(\s+[A-Z][A-Z]+)+\s*$', '')
                      ELSE base END), '') AS c
          FROM (
            SELECT tenant_id, id,
                   REGEXP_REPLACE(REGEXP_REPLACE(
                       SUBSTRING(raw->>'original_description' FROM 8),
                       '\s+\d.*$', ''),
                     ',?\s+[A-Z]{2},?\s+US\s*$', '') AS base
              FROM transactions
             WHERE merchant_name IS NULL
               AND raw->>'original_description' LIKE 'VENMO *%'
          ) s1
      ) s2
  ) x
 WHERE t.tenant_id = x.tenant_id AND t.id = x.id
   AND x.counterparty IS NOT NULL;
