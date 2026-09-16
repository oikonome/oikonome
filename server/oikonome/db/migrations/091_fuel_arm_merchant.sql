-- A chain's fuel arm becomes its own merchant.
--
-- A warehouse club or grocery chain that also sells fuel is two merchants
-- wearing one name: the pump and the store have different prices, different
-- rhythms, and different answers to "what does a visit cost". Blended under
-- one payee, merchant totals, search averages and recurring detection all
-- describe a merchant that does not exist.
--
-- Plaid already knows: it sends merchant_name "Northwind Club" (the brand)
-- beside name "Northwind Club Gas" (the outlet), and the bank's statement
-- line survives verbatim in raw.original_description:
--   NORTHWIND CLUB GAS #1234    -> the pump
--   NORTHWIND CLUB WHSE #1234   -> the warehouse
--   WWW NORTHWIND CLUB COM      -> online
-- The ledger drops the distinction because payee is
-- COALESCE(merchant_name, name) — the brand wins. Promoting `name`
-- wholesale would be worse: it is usually the shouted statement string,
-- which is why merchant_name is preferred. So the narrow fuel signal is
-- read out of EITHER text.
-- sync/base.py (fuel_arm) derives the merchant on ingest for every future
-- row; this applies the same transform to what is already stored, whole
-- history, all tenants.
--
-- Deliberately narrow, and the narrowness is the point:
--   * the descriptor must name the BRAND and then the fuel word, which is
--     what a pump's own descriptor looks like. A row is never split because
--     of its category — deriving a merchant from a category would let one
--     miscategorized row rename a payee, and categories are the thing most
--     likely to be wrong.
--   * merchants already named for fuel are left alone (nothing to promote).
--   * brands with regex metacharacters in them are skipped rather than
--     escaped; a merchant named "A+ Market" is not worth a quoting bug.
--   * category fields and category_override are untouched. Renaming the
--     merchant is a statement about WHO was paid, never about what for.
UPDATE transactions t
   SET merchant_name = x.arm
  FROM (
    SELECT tenant_id, id,
           merchant_name || ' ' || INITCAP(hit[1]) AS arm
      FROM (
        SELECT tenant_id, id, merchant_name,
               REGEXP_MATCH(
                 COALESCE(name, '') || ' | '
                   || COALESCE(raw->>'original_description', ''),
                 '\m' || SPLIT_PART(merchant_name, ' ', 1)
                       || '\M[^A-Za-z]*\m(gas|fuel)\M',
                 'i') AS hit
          FROM transactions
         WHERE removed = 0
           AND merchant_name IS NOT NULL
           -- a plain word-and-space brand, so the concatenation above is a
           -- literal and not an accidental pattern
           AND SPLIT_PART(merchant_name, ' ', 1) ~ '^[A-Za-z0-9]+$'
           AND merchant_name !~* '\m(gas|fuel)\M'
      ) m
     WHERE hit IS NOT NULL
  ) x
 WHERE t.tenant_id = x.tenant_id AND t.id = x.id
   AND t.merchant_name IS DISTINCT FROM x.arm;
