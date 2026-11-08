-- A per-household secret that file-row ids hashed over a full account
-- number are keyed with.
--
-- A file importer gives each row the hash of "<account id>|date|cents|
-- name|n" (sync/base.FileImportIds), unkeyed. A split account an earlier
-- release named by its full account number had that number in its id, so
-- every row it took was hashed over it. Migration 199 moved the account
-- onto an id that keeps only the last four, but left the rows' ids alone,
-- and every other input of their hash rides /export beside the row: with
-- the last four shown in the new account id, the rest of the number is a
-- short search away from anyone holding an export. A row imported before
-- names were masked is the same case by another road: its id was hashed
-- over the name while the name still held the full number, and the name
-- that rides the export beside it now keeps the last four.
--
-- Those rows are re-id'd as "<prefix>:k" plus the HMAC of their old id
-- under this secret (base.key_file_import_ids, which moves each row with
-- adopt._rewrite_txn_id so every reference to it follows). That needs
-- Python, so it is not done here: `oikonome migrate` runs it for each
-- household that has such rows once the migrations and grants are in
-- place (migrate.key_numbered_file_rows), and a restore runs it after an
-- older archive's rows land. A re-import of the same file recomputes the
-- old id, which the file spells again, and finds the row under its keyed
-- form, so it adds nothing.
--
-- The secret never leaves the instance: the export and the portability
-- archive leave this table out, and the column is named so their shared
-- secret-column filter drops it besides. It is stored through the
-- household's own key (crypto.encrypt) where a master key exists. A
-- restore into another household therefore keys an older archive's rows
-- under that household's own secret, and a later re-import there is
-- matched by content alone. Erasure removes it with the household.
-- Mirrored in schema.sql.

CREATE TABLE IF NOT EXISTS txn_id_keys (
    tenant_id   UUID NOT NULL DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid,
    secret      TEXT NOT NULL,
    PRIMARY KEY (tenant_id)
);

ALTER TABLE txn_id_keys ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON txn_id_keys;
CREATE POLICY tenant_isolation ON txn_id_keys
    USING (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid))
    WITH CHECK (tenant_id = (SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid));
