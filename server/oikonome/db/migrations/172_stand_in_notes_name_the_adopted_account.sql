-- A file row's stand-in note (raw->'_stood_in'->'accounts', see migration
-- 161) names the bank-feed account whose copy of the charge was set aside.
-- When a re-link later adopted that account under a new id, the note kept
-- the old one and rested on the account_adoptions record to stay
-- protected. That record is not part of an export, so after a restore the
-- note named an account the household never had there, and undoing the
-- import or deleting its account deleted the only copy of the charge.
--
-- Adoption now renames the id inside the note as well
-- (sync/base.carry_stood_in_accounts). This brings the notes written
-- before that to the same place: every noted id an adoption moved is
-- replaced by the account it moved to. account_adoptions.account_id
-- follows later renames, so one hop reaches the current account.

DO $$
DECLARE
    n integer;
BEGIN
    WITH hit AS (
        SELECT t.tenant_id, t.id,
               (SELECT COALESCE(jsonb_agg(DISTINCT COALESCE(
                           (SELECT ad.account_id FROM account_adoptions ad
                             WHERE ad.tenant_id = t.tenant_id
                               AND ad.old_account_id = e.a
                             ORDER BY ad.account_id LIMIT 1),
                           e.a)), '[]'::jsonb)
                  FROM jsonb_array_elements_text(t.raw->'_stood_in'->'accounts')
                       AS e(a)) AS accts
          FROM transactions t
         WHERE jsonb_typeof(t.raw->'_stood_in'->'accounts') = 'array'
           AND EXISTS (
               SELECT 1 FROM account_adoptions ad,
                      jsonb_array_elements_text(t.raw->'_stood_in'->'accounts')
                      AS e(a)
                WHERE ad.tenant_id = t.tenant_id AND ad.old_account_id = e.a))
    UPDATE transactions t
       SET raw = jsonb_set(t.raw, '{_stood_in,accounts}', hit.accts)
      FROM hit
     WHERE t.tenant_id = hit.tenant_id AND t.id = hit.id;
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN
        RAISE NOTICE 'imported rows: % stand-in notes now name the account '
                     'a re-link adopted', n;
    END IF;
END $$;
