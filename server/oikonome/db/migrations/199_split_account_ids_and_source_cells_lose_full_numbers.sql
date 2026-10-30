-- Split accounts whose id spells a full account number move onto a masked
-- id, and the source-account cell of every file row keeps only the last
-- four of a number in it.
--
-- A multi-account file (QIF, Mint, Monarch, Copilot, Simplifi) made one
-- manual account per source account, with the slug of the source name as
-- its id. An account named by its number ("1234567890123") therefore kept
-- the whole number in its id, which every transaction row, setting and
-- export names, and in the `account` / `Account Name` cell of each row's
-- raw, which leaves the instance verbatim through /export. The name was
-- cut by migration 189; this cuts the rest. sync/base.py applies the same
-- rule to new rows (mask_file_raw, _FREE_TEXT_KEYS) and new ids
-- (split_account_id); restore applies this move to an older archive
-- (base.rekey_numbered_split_accounts, whose renumbered_split_id spells
-- the new id exactly as below). Keep the three in step.
--
-- The move is the one migration 149 makes: copy the row under the new id,
-- re-point every column that names it (adopt.ACCOUNT_REFS), rewrite the
-- settings keys that hold an account id in the live settings and every
-- closed month's frozen copy, then drop the old row. It also records an
-- adoption onto the new id, reconciled at once so no reconnect guard arms,
-- from the old id with its number cut the same way ("manual:qif-…0123",
-- base.split_lineage_id): account_adoptions rides /export and the
-- portability archive, so it must not keep the number either, nor any
-- digest of it (the last four are in the new id, and the rest is a short
-- search for an unkeyed hash). Transaction ids are left as they are (they
-- are hashes, the number is not readable from them); a re-import of the
-- same file follows that record to the moved account and recomputes its
-- ids under the old account id, which the file spells again
-- (base.split_legacy_id, FileImportIds `former`), so it finds the rows it
-- made before instead of adding them a second time. The cut id is not
-- unique: two cards sharing their last four leave one key naming two
-- accounts, and the re-import then picks the one holding rows whose ids
-- recompute under the full id the file spells, or follows the key nowhere
-- (base._lineage_by_rows). An id whose masked form is taken gets the
-- first free of 32 suffixes, each eight hex digits of the md5 of the cut
-- old id and an ordinal ("…#1", "…#2", base.renumbered_split_suffix),
-- every decimal digit spelled as a letter (g-p, base.letter_digest) so
-- the suffix can never read as a number. The ordinal, not the number,
-- keeps three or more such cards apart, so every one of them moves.
-- Accounts are taken in the order of the md5 of their old id: the md5
-- orders and is never stored, and ordering by the id itself would let the
-- suffix tell which card has the smaller number. An account an earlier
-- slug gave several sources stays one account: splitting it is a
-- person's call, not a migration's.
--
-- Idempotent: a moved id no longer holds eight digits in a row (its
-- suffix has no digits at all), and a masked cell is "…" plus four
-- digits.

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

UPDATE transactions t
   SET raw = (
       SELECT jsonb_object_agg(
                e.key,
                CASE WHEN jsonb_typeof(e.value) = 'string'
                      AND lower(btrim(e.key, E' \t\r\n'))
                          IN ('account', 'account name')
                     THEN to_jsonb(regexp_replace(
                              e.value #>> '{}', '\d{4,}(\d{4})', '…\1', 'g'))
                     ELSE e.value END)
         FROM jsonb_each(t.raw) e)
 WHERE split_part(t.id, ':', 1) IN ('pdf', 'csv', 'ofx', 'qif', 'mint', 'ynab',
                                    'monarch', 'copilot', 'simplifi')
   AND jsonb_typeof(t.raw) = 'object'
   AND t.raw::text ~ '\d{8}'
   AND EXISTS (
       SELECT 1 FROM jsonb_each(t.raw) e
        WHERE jsonb_typeof(e.value) = 'string'
          AND (e.value #>> '{}') ~ '\d{8}'
          AND lower(btrim(e.key, E' \t\r\n')) IN ('account', 'account name'));

DO $$
DECLARE
    r record;
    tail text;
    base_id text;
    new_id text;
    cut_id text;
    n int;
BEGIN
    FOR r IN SELECT tenant_id, id FROM accounts
              WHERE id ~ '^manual:(qif|mint|monarch|copilot|simplifi)-.*\d{8,}'
              ORDER BY tenant_id, md5(id) COLLATE "C" LOOP
        tail := substr(r.id, strpos(r.id, '-') + 1);
        cut_id := regexp_replace(r.id, '\d{4,}(\d{4})', '…\1', 'g');
        base_id := split_part(r.id, '-', 1) || '-' || COALESCE(NULLIF(left(
            btrim(regexp_replace(regexp_replace(tail, '\d{4,}(\d{4})', '-\1',
                                                'g'),
                                 '-+', '-', 'g'), '-'), 40), ''), 'x');
        new_id := base_id;
        n := 0;
        WHILE EXISTS (SELECT 1 FROM accounts
                       WHERE tenant_id = r.tenant_id AND id = new_id) LOOP
            n := n + 1;
            IF n > 32 THEN
                new_id := NULL;
                EXIT;
            END IF;
            new_id := base_id || '-' || translate(
                left(md5(cut_id || '#' || n), 8),
                '0123456789', 'ghijklmnop');
        END LOOP;
        CONTINUE WHEN new_id IS NULL;
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
        INSERT INTO account_adoptions (tenant_id, account_id, old_account_id,
                                       item_id, reconciled_at)
             VALUES (r.tenant_id, new_id, cut_id, 'manual', now());
        DELETE FROM accounts WHERE tenant_id = r.tenant_id AND id = r.id;
    END LOOP;
END $$;
