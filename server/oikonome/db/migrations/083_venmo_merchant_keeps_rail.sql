-- Venmo counterparty merchants keep the rail visible: "Venmo — Casey
-- Example", not bare "Casey Example".
--
-- The same person paid via Venmo and via Zelle are different things to a
-- reader and to grouping, so the merchant keeps the channel (082 writes
-- the prefixed form directly). sync/base.py derives the prefixed form on
-- ingest; this re-derives the counterparty exactly as 081 does and
-- prefixes the rows whose merchant is still the bare parse. Rows where the aggregator
-- supplied a real merchant never match the parse and stay untouched;
-- already-prefixed rows no longer equal the bare parse, so re-running is
-- a no-op.
UPDATE transactions t
   SET merchant_name = 'Venmo — ' || x.counterparty
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
             WHERE raw->>'original_description' LIKE 'VENMO *%'
          ) s1
      ) s2
  ) x
 WHERE t.tenant_id = x.tenant_id AND t.id = x.id
   AND x.counterparty IS NOT NULL
   AND t.merchant_name = x.counterparty;
