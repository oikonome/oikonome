-- A 1099 vendor mark names its merchant by id (migration 164), but nothing
-- held that id to a merchant row. The nightly twin fold left a folded
-- merchant's marks naming it, and the prune then deleted the merchant: the
-- mark named nothing, a later rename of the payee could not find it, and
-- the contractor dropped off the year-end list. The fold now moves marks
-- to the survivor and the prune holds a merchant a mark names
-- (engine/merchant_identity.py); this repairs the marks already left
-- behind and lets the database refuse the next one.
--
-- A mark naming a merchant that no longer exists takes the one live
-- merchant of its name, as 164's backfill did, unless that merchant
-- already has a mark for the same business; otherwise it keeps its name
-- alone, which the read side still matches. Then the foreign key: a
-- merchant deleted from under a mark leaves the mark by name, never
-- pointing at nothing. Re-runnable.

DO $$
DECLARE
    n integer;
BEGIN
    CREATE TEMP TABLE vendor_1099_dangling ON COMMIT DROP AS
        SELECT v.tenant_id, v.id
          FROM vendor_1099 v
         WHERE v.merchant_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM merchants m
                            WHERE m.tenant_id = v.tenant_id
                              AND m.id = v.merchant_id);
    GET DIAGNOSTICS n = ROW_COUNT;

    UPDATE vendor_1099 v SET merchant_id = NULL
      FROM vendor_1099_dangling d
     WHERE v.tenant_id = d.tenant_id AND v.id = d.id;

    UPDATE vendor_1099 v
       SET merchant_id = c.mid
      FROM (SELECT v2.tenant_id, v2.id, m.id AS mid,
                   row_number() OVER (PARTITION BY v2.tenant_id, v2.entity_id,
                                                   m.id
                                      ORDER BY v2.created_at, v2.id) AS k
              FROM vendor_1099 v2
              JOIN vendor_1099_dangling d
                ON d.tenant_id = v2.tenant_id AND d.id = v2.id
              JOIN merchants m
                ON m.tenant_id = v2.tenant_id AND m.merged_into IS NULL
               AND m.name = v2.merchant
             WHERE (SELECT count(*) FROM merchants o
                     WHERE o.tenant_id = v2.tenant_id
                       AND o.merged_into IS NULL
                       AND o.name = v2.merchant) = 1) c
     WHERE v.tenant_id = c.tenant_id AND v.id = c.id AND c.k = 1
       AND NOT EXISTS (SELECT 1 FROM vendor_1099 x
                        WHERE x.tenant_id = v.tenant_id
                          AND x.entity_id = v.entity_id
                          AND x.merchant_id = c.mid);

    IF n > 0 THEN
        RAISE NOTICE '1099 vendor marks: % named a merchant that no longer '
                     'exists and were re-pointed or kept by name', n;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'vendor_1099_merchant_fk') THEN
        ALTER TABLE vendor_1099 ADD CONSTRAINT vendor_1099_merchant_fk
            FOREIGN KEY (tenant_id, merchant_id)
            REFERENCES merchants (tenant_id, id)
            ON DELETE SET NULL (merchant_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS vendor_1099_merchant
    ON vendor_1099 (tenant_id, merchant_id) WHERE merchant_id IS NOT NULL;
