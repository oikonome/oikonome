-- Migration 203 cuts card numbers from file rows; this cuts them from the
-- resolver's own `canonical` values. When the string clean finds no name in a
-- descriptor ("4111-1111-1111-1234 VISA", "ONLINE PMT 3782 822463 10005") it
-- keeps the descriptor, whole number included, so a row's name can be cut while
-- the same number stays on merchants.name and merchant_canonical.canonical,
-- which is what every screen displays. merchant_dedup.canonical_merchant and the mint door in
-- merchant_identity no longer write one; this cuts the ones already stored.
-- The cut is 203's, base.GROUPED_DIGITS_RE then LONG_DIGITS_RE, in that order;
-- keep the three in step.
--
-- What a person typed is theirs and stays, number included: a merchant with
-- name_source 'manual', or named by a manual alias, a manual `canonical`, and
-- what a rename journalled as its result. The resolver would put such a name
-- straight back in any case.
--
-- This writes the stored spelling and nothing cleverer. A feed row's key still
-- holds its number (the feed's own word, as 203 left it), and the nightly
-- merchant_dedup.apply reads that key anew: "ACH 4111 1111 1111 1234 ACME" is
-- "ACH …1234 ACME" here and "Acme" by morning, through the path that moves the
-- rows and leaves a pointer behind.
--
-- Two merchants can cut to one name. No unique index covers a name, so nothing
-- here can fail on that, and merchant_identity.fold_twins, nightly, folds them
-- with their rows, aliases, children, 1099 marks and a pointer for the bills
-- that hold the loser's id; two rows that each carry a Plaid entity stay two
-- businesses. Until then the pair reads as one label, on surfaces that group by
-- name. The two things below that are keyed on the name do have a unique index:
-- a category rule keeps one row, a person's over a machine's, and of two 1099
-- marks cutting to one name the oldest takes it while the other waits for the
-- fold, which folds the marks too.
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
CREATE OR REPLACE FUNCTION pg_temp.may_hold_number(v text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT v ~ '\d{8}|\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}|\d{4}[ -]\d{6}[ -]\d{5}'
$$;

CREATE OR REPLACE FUNCTION pg_temp.is_cut(v text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT pg_temp.may_hold_number(v) AND pg_temp.cut_numbers(v) <> v
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
