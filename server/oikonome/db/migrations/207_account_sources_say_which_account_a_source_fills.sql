-- Which account a source name fills, kept apart from the account's id.
--
-- A hand-made account's id used to be the slug of its name, so the name
-- carried both what the account is called and which account it is. A
-- door making or finding one re-derived the id from the name: cutting a
-- card number out of the name cut it out of the identity too, two cards
-- sharing their last four became one account, and a taken slug was
-- disambiguated with a "-N" the card-number grammar then had to tell from
-- the number's own last group.
--
-- New accounts get an opaque id, minted once (sync/account_sources.py).
-- This table maps each (door, source) to the account it fills: an
-- "s:" row, a keyed HMAC of the source under the household's own secret
-- (txn_id_keys); beside it, when the source spells a full number, a "d:"
-- row keyed over the number's digits alone, so a card is told from
-- another sharing its last four and the number is stored nowhere; and
-- for every hand-made account a "c:" row, a plain digest of its stored
-- name, which is already cut and rides every export beside the row.
--
-- Existing accounts keep their ids: a file row's id is hashed over its
-- account id, so a re-key would re-insert every re-imported line. They
-- are backfilled here with their name's c-row, which is what a later
-- source finds them by; the first import that reaches one adds its keyed
-- row. The digest is NAME_DIGEST_SQL in sync/account_sources.py, whose
-- `backfill` runs this same statement after a restore. Idempotent.
-- Mirrored in schema.sql.

CREATE TABLE IF NOT EXISTS account_sources (
    tenant_id   UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    door        TEXT NOT NULL,
    source_hmac TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, door, source_hmac, account_id)
);
-- a source fills one account; a card or a stored name may be shared
CREATE UNIQUE INDEX IF NOT EXISTS account_sources_one_account_per_source
    ON account_sources (tenant_id, door, source_hmac)
    WHERE left(source_hmac, 2) = 's:';
CREATE INDEX IF NOT EXISTS account_sources_by_account
    ON account_sources (tenant_id, account_id);

ALTER TABLE account_sources ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON account_sources;
CREATE POLICY tenant_isolation ON account_sources
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));

INSERT INTO account_sources (tenant_id, door, source_hmac, account_id)
SELECT a.tenant_id, 'manual',
       'c:' || encode(sha256(convert_to(btrim(regexp_replace(
           a.name, '[ \t\n\r\f\v]+', ' ', 'g'), ' '), 'UTF8')), 'hex'),
       a.id
  FROM accounts a
 WHERE a.item_id = 'manual' AND a.id LIKE 'manual:%'
   AND a.id !~ '^manual:(qif|mint|monarch|copilot|simplifi|ofx)-'
   AND btrim(coalesce(a.name, '')) <> ''
ON CONFLICT DO NOTHING;
