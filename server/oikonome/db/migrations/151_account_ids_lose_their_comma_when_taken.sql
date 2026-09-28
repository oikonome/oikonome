-- Rename the comma account ids 149 had to leave behind, and enforce the
-- no-comma rule again.
--
-- 149 skips an id whose plain rename (every ',' becomes '-') already
-- belongs to a different account in the same household, and drops the
-- rule. Leaving such an id in place does not keep it syncing: every
-- ingest path renames an incoming id before it is stored, so the next
-- pull of `a,b` lands on the unrelated `a-b`, and the comma row itself is
-- never written again.
--
-- Such an id now moves to `replace(id, ',', '-') || '-' || left(md5(id), 8)`
-- (sync/adopt.py account_id_when_taken), a name derived from the original
-- id alone, so the ingest paths compute it too and look it up before the
-- plain rename (adopt.stored_account_id): the next pull reaches the moved
-- account, not its neighbour. An id whose plain name has since come free
-- takes the plain name, as 149 would have given it. The move itself is
-- 149's: copy the row, re-point every column that names it, rewrite the
-- settings keys in the live settings and every closed month, drop the old
-- row.
--
-- Idempotent: with no comma ids left it only validates the constraint.

CREATE OR REPLACE FUNCTION pg_temp.acct_cfg_rename(cfg jsonb, old text,
                                                   new text)
RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    out jsonb := cfg;
    debts jsonb;
BEGIN
    IF jsonb_typeof(cfg) IS DISTINCT FROM 'object' THEN
        RETURN cfg;
    END IF;
    -- the Accounts page's exclusion list: renamed and de-duplicated in
    -- order, as the Python rename does
    IF jsonb_typeof(out -> 'excluded_accounts') = 'array'
       AND out -> 'excluded_accounts' @> to_jsonb(ARRAY[old]) THEN
        out := jsonb_set(out, '{excluded_accounts}', (
            SELECT COALESCE(jsonb_agg(v ORDER BY first_at), '[]'::jsonb)
              FROM (SELECT v, min(n) AS first_at
                      FROM (SELECT CASE WHEN e = to_jsonb(old)
                                        THEN to_jsonb(new) ELSE e END AS v,
                                   n
                              FROM jsonb_array_elements(
                                       out -> 'excluded_accounts')
                                   WITH ORDINALITY x(e, n)) y
                     GROUP BY v) z));
    END IF;
    IF out ->> 'checking_account_id' = old THEN
        out := jsonb_set(out, '{checking_account_id}', to_jsonb(new));
    END IF;
    IF jsonb_typeof(out -> 'savings_goals') = 'array' THEN
        out := jsonb_set(out, '{savings_goals}', (
            SELECT COALESCE(jsonb_agg(
                       CASE WHEN jsonb_typeof(g) = 'object'
                                 AND g ->> 'account_id' = old
                            THEN jsonb_set(g, '{account_id}', to_jsonb(new))
                            ELSE g END ORDER BY n), '[]'::jsonb)
              FROM jsonb_array_elements(out -> 'savings_goals')
                   WITH ORDINALITY x(g, n)));
    END IF;
    -- "these two are not the same account": each entry is the pair sorted
    -- and joined on '|', rebuilt the same way or the suggester would not
    -- find it
    IF jsonb_typeof(out -> 'link_dismissed') = 'array'
       AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(
                                     out -> 'link_dismissed') k
                    WHERE old = ANY(string_to_array(k, '|'))) THEN
        out := jsonb_set(out, '{link_dismissed}', (
            SELECT COALESCE(jsonb_agg(DISTINCT pair COLLATE "C"
                                      ORDER BY pair COLLATE "C"),
                            '[]'::jsonb)
              FROM (SELECT (SELECT string_agg(p, '|' ORDER BY p COLLATE "C")
                              FROM unnest(array_replace(
                                       string_to_array(k, '|'), old, new)) p)
                           AS pair
                      FROM jsonb_array_elements_text(
                               out -> 'link_dismissed') k) s));
    END IF;
    -- the debt planner's overrides, keyed by account id; one already set
    -- on the new id is the newer word and is kept
    debts := out #> '{debt_plan,debts}';
    IF jsonb_typeof(debts) = 'object' AND debts ? old THEN
        IF NOT debts ? new THEN
            debts := debts || jsonb_build_object(new, debts -> old);
        END IF;
        out := jsonb_set(out, '{debt_plan,debts}', debts - old);
    END IF;
    RETURN out;
END $$;

DO $$
DECLARE
    r record;
    new_id text;
    left_over integer;
BEGIN
    FOR r IN SELECT tenant_id, id FROM accounts
              WHERE strpos(id, ',') > 0 ORDER BY tenant_id, id LOOP
        new_id := replace(r.id, ',', '-');
        IF EXISTS (SELECT 1 FROM accounts
                    WHERE tenant_id = r.tenant_id AND id = new_id) THEN
            new_id := new_id || '-' || left(md5(r.id), 8);
        END IF;
        IF EXISTS (SELECT 1 FROM accounts
                    WHERE tenant_id = r.tenant_id AND id = new_id) THEN
            CONTINUE;
        END IF;
        INSERT INTO accounts
             SELECT (jsonb_populate_record(NULL::accounts,
                         to_jsonb(a) || jsonb_build_object('id', new_id))).*
               FROM accounts a
              WHERE a.tenant_id = r.tenant_id AND a.id = r.id;
        UPDATE transactions SET account_id = new_id
         WHERE tenant_id = r.tenant_id AND account_id = r.id;
        UPDATE account_adoptions SET account_id = new_id
         WHERE tenant_id = r.tenant_id AND account_id = r.id;
        UPDATE liabilities SET account_id = new_id
         WHERE tenant_id = r.tenant_id AND account_id = r.id;
        UPDATE account_links SET account_id = new_id
         WHERE tenant_id = r.tenant_id AND account_id = r.id;
        UPDATE bills SET account_id = new_id
         WHERE tenant_id = r.tenant_id AND account_id = r.id;
        UPDATE holdings SET account_id = new_id
         WHERE tenant_id = r.tenant_id AND account_id = r.id;
        UPDATE crypto_holdings SET account_id = new_id
         WHERE tenant_id = r.tenant_id AND account_id = r.id;
        UPDATE import_batches SET account_id = new_id
         WHERE tenant_id = r.tenant_id AND account_id = r.id;
        UPDATE business_entity SET tax_reserve_account_id = new_id
         WHERE tenant_id = r.tenant_id AND tax_reserve_account_id = r.id;
        UPDATE tenant_settings
           SET config = pg_temp.acct_cfg_rename(config, r.id, new_id)
         WHERE tenant_id = r.tenant_id
           AND config IS DISTINCT FROM
               pg_temp.acct_cfg_rename(config, r.id, new_id);
        UPDATE budget_snapshots
           SET config = pg_temp.acct_cfg_rename(config, r.id, new_id)
         WHERE tenant_id = r.tenant_id
           AND config IS DISTINCT FROM
               pg_temp.acct_cfg_rename(config, r.id, new_id);
        DELETE FROM accounts WHERE tenant_id = r.tenant_id AND id = r.id;
    END LOOP;

    SELECT count(*) INTO left_over FROM accounts WHERE strpos(id, ',') > 0;
    IF left_over = 0 THEN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
                        WHERE conrelid = to_regclass('accounts')
                          AND conname = 'accounts_id_has_no_comma') THEN
            ALTER TABLE accounts ADD CONSTRAINT accounts_id_has_no_comma
                CHECK (strpos(id, ',') = 0);
        ELSE
            ALTER TABLE accounts
                VALIDATE CONSTRAINT accounts_id_has_no_comma;
        END IF;
    ELSE
        ALTER TABLE accounts DROP CONSTRAINT IF EXISTS
            accounts_id_has_no_comma;
        RAISE WARNING '% account id(s) with a comma could not be renamed '
                      '(both comma-free ids are already taken); the no-comma '
                      'rule is NOT enforced until they are resolved by hand',
                      left_over;
    END IF;
END $$;
