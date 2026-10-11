-- A 1099 vendor mark follows the merchant, not the name it was made under.
--
-- Marks were keyed by display name alone, and a display name changes: a
-- hand rename or merge, the nightly string clean, an automatic split
-- repair. The year-end list then stopped finding the mark, and a
-- contractor owed a 1099-NEC dropped off it without a word. The mark now
-- carries the merchant it was made for; the name stays for the screens
-- and for marks made on a payee no merchant row stands behind.
--
-- merged_from records the marks a merge folded into this one, so undoing
-- the merge can put them back as they were.
--
-- Backfill: a mark whose name is the name of exactly one live merchant
-- takes that merchant. Where two marks of one business would land on one
-- merchant, the oldest takes it and the other keeps its name alone (the
-- read side still finds it by name). Re-runnable.

ALTER TABLE vendor_1099 ADD COLUMN IF NOT EXISTS merchant_id UUID;
ALTER TABLE vendor_1099 ADD COLUMN IF NOT EXISTS merged_from JSONB
    NOT NULL DEFAULT '[]'::jsonb;

UPDATE vendor_1099 v
   SET merchant_id = c.mid
  FROM (SELECT v2.tenant_id, v2.id, m.id AS mid,
               row_number() OVER (PARTITION BY v2.tenant_id, v2.entity_id, m.id
                                  ORDER BY v2.created_at, v2.id) AS n
          FROM vendor_1099 v2
          JOIN merchants m
            ON m.tenant_id = v2.tenant_id AND m.merged_into IS NULL
           AND m.name = v2.merchant
         WHERE v2.merchant_id IS NULL
           AND (SELECT count(*) FROM merchants o
                 WHERE o.tenant_id = v2.tenant_id AND o.merged_into IS NULL
                   AND o.name = v2.merchant) = 1) c
 WHERE v.tenant_id = c.tenant_id AND v.id = c.id AND c.n = 1
   AND NOT EXISTS (SELECT 1 FROM vendor_1099 x
                    WHERE x.tenant_id = v.tenant_id
                      AND x.entity_id = v.entity_id
                      AND x.merchant_id = c.mid);

CREATE UNIQUE INDEX IF NOT EXISTS vendor_1099_one_mark_per_merchant
    ON vendor_1099 (tenant_id, entity_id, merchant_id)
 WHERE merchant_id IS NOT NULL;
