-- The fuel-arm rule stops caring how a brand is spelled.
--
-- A brand is often spelled with punctuation — hyphens, apostrophes,
-- ampersands — and a statement line may spell the same brand with or
-- without it. A hyphenated brand's pump is as plain a pump as any other:
-- "A-B-C GAS" and "ABC GAS" are the same fuel arm.
--
-- So brand and text are compared on their ALPHANUMERICS only, character by
-- character with any punctuation allowed between: "A-B-C" matches both
-- "A-B-C GAS" and "ABC GAS". Because only the brand's alphanumerics survive
-- into the pattern, a merchant name cannot carry regex syntax into it at
-- all — there is nothing to escape.
--
-- The rule stays conservative:
--   * the fuel word must be the NEXT token. "ACME WHSE" and "ACME AUTO FUEL"
--     both fail on what follows the brand, and a brand "Vega" cannot match
--     "VEGAS BUFFET" because the S sits where a separator is required;
--   * a merchant already named for fuel is skipped, which is also what
--     keeps a natural-gas utility from becoming "Pinewood Natural Gas Gas";
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
