-- The outlet is recorded beside the merchant, never on top of it.
--
-- Supersedes 091, which wrote the fuel arm INTO merchant_name. That was the
-- wrong shelf. merchant_dedup's doctrine — the one the whole display layer
-- already follows — is that `name` and `merchant_name` are the SOURCE's
-- words, written only by sync, and that our own identity for a payee lives
-- somewhere else so the fix stays reversible and a re-sync cannot fight it.
-- Overwriting merchant_name broke both: it discarded what Plaid actually
-- said, unrecoverably short of a re-sync.
--
-- The Venmo and Zelle rules do write merchant_name, and the difference is
-- real: those fire only when the aggregator supplied nothing usable, so
-- they RECOVER a merchant rather than override one. Here Plaid's
-- merchant_name is correct and we only want to be more specific.
--
-- Why a column and not a merchant_canonical row: that map is keyed by the
-- merchant STRING, and a split has no single answer per string —
-- "Costco" resolves to the pump on one row and the warehouse on the next.
-- The decision is per transaction, so it is stored per transaction.
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS merchant_outlet TEXT;

-- Put back what 091 overwrote, from the payload it was derived from. Only
-- rows whose stored merchant_name disagrees with the source's, and only
-- where the difference is exactly the fuel suffix 091 appended — a row a
-- user or another rule renamed is left alone.
UPDATE transactions t
   SET merchant_name = t.raw->>'merchant_name'
 WHERE t.removed = 0
   AND t.raw->>'merchant_name' IS NOT NULL
   AND t.merchant_name IS DISTINCT FROM t.raw->>'merchant_name'
   AND LOWER(t.merchant_name) IN (LOWER(t.raw->>'merchant_name') || ' gas',
                                  LOWER(t.raw->>'merchant_name') || ' fuel');

-- Derive the outlet the same way sync/base.py (fuel_arm) now does, over the
-- whole history: the BRAND followed by a fuel word, in either Plaid's name
-- or the bank's statement line. Never from the category — fuel is the
-- category most often wrong, and an identity derived from a category lets
-- one bad row rename a payee.
UPDATE transactions t
   SET merchant_outlet = x.outlet
  FROM (
    SELECT tenant_id, id,
           merchant_name || ' ' || INITCAP(hit[1]) AS outlet
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
   AND t.merchant_outlet IS DISTINCT FROM x.outlet;
