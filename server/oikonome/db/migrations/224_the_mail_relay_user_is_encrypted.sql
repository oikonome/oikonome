-- The mail relay's login (smtp_user) is a credential: Postmark takes the
-- server API token as both the SMTP username and the password. It is now
-- stored encrypted like smtp_password, and a month's budget snapshot no
-- longer freezes it (budget._clean_snapshot_config drops it on every
-- freeze and read).
--
-- This file takes it out of the snapshots already stored. A row without
-- the key is not written, so a second run changes nothing.
--
-- The live value in tenant_settings is encrypted by the worker's startup
-- secret sweep (db/migrate.reencrypt_tenant_secrets, which reads the
-- connections bundle's secret list, smtp_user now among them), not here:
-- encrypting needs each household's own data key on a tenant-scoped
-- connection, and this one-shot runs as the superuser, where an
-- unqualified tenant_keys read would take another household's key. The
-- sweep leaves an already-encrypted value alone.

UPDATE budget_snapshots SET config = config - 'smtp_user'
 WHERE config ? 'smtp_user';
