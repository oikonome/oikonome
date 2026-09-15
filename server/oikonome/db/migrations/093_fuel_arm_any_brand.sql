-- The fuel-arm rule stops caring how a brand is spelled.
--
-- 092 built its pattern by pasting the merchant's first word straight into
-- a regex, so it had to refuse any brand carrying punctuation — otherwise a
-- name could inject regex syntax. That guard threw out every hyphenated or
-- possessive brand: "H-E-B GAS/CARWASH #0118" is as plain a
-- pump as "COSTCO GAS #0007" and was invisible to it, and so would
-- "LOWES FUEL" be.
--
-- Now brand and text are compared on their ALPHANUMERICS only, character by
-- character with any punctuation allowed between: "H-E-B" matches both
-- "H-E-B GAS" and "HEB GAS". Because only the brand's alphanumerics survive
-- into the pattern, a merchant name cannot carry regex syntax into it at
-- all — the escaping problem is removed rather than guarded against.
--
-- Everything that made the rule conservative is unchanged:
--   * the fuel word must be the NEXT token. "COSTCO WHSE", "WWW COSTCO COM"
--     and "SHELL OIL … AUTO FUEL DISPEN" all fail on what follows the brand,
--     and a brand "Vega" cannot match "VEGAS BUFFET" because the S sits
--     where a separator is required;
--   * a merchant already named for fuel is skipped, which is also what
--     keeps a natural-gas utility from becoming "Wisconsin Public Gas Gas";
--   * brands under three alphanumerics are skipped — two letters are not
--     evidence of anything;
--   * merchant_name is never written, only merchant_outlet;
--   * no category is read or changed. A row is fuel here because the bank
--     said so, never because something classified it that way.
UPDATE transactions t
   SET merchant_outlet = x.outlet
  FROM (
    SELECT tenant_id, id, merchant_name || ' ' || INITCAP(hit[2]) AS outlet
      FROM (
        SELECT tenant_id, id, merchant_name,
               REGEXP_MATCH(
                 COALESCE(name, '') || ' | '
                   || COALESCE(raw->>'original_description', ''),
                 '(^|[^A-Za-z0-9])'
                   || ARRAY_TO_STRING(
                        REGEXP_SPLIT_TO_ARRAY(
                          REGEXP_REPLACE(merchant_name, '[^A-Za-z0-9]', '', 'g'),
                          ''),
                        '[^A-Za-z0-9]*')
                   || '[^A-Za-z0-9]+(gas|fuel)([^A-Za-z0-9]|$)',
                 'i') AS hit
          FROM transactions
         WHERE removed = 0
           AND merchant_name IS NOT NULL
           AND LENGTH(REGEXP_REPLACE(merchant_name, '[^A-Za-z0-9]', '', 'g')) >= 3
           AND merchant_name !~* '\m(gas|fuel)\M'
      ) m
     WHERE hit IS NOT NULL
  ) x
 WHERE t.tenant_id = x.tenant_id AND t.id = x.id
   AND t.merchant_outlet IS DISTINCT FROM x.outlet;
