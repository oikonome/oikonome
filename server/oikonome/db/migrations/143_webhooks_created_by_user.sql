-- Who made a webhook, by account id rather than by address.
--
-- A credential rotation pauses the webhooks the rotated user made, and it
-- used to find them by the creator's email as it stood when the hook was
-- made. An address changes: a hook made under the old one, then switched
-- back on after the email change, no longer matched anything, so every
-- later rotation (password change, reset, operator factor clear) left it
-- posting the household's rows. The id does not change.
--
-- Backfilled from the address where it still names a member of the same
-- household. A hook whose maker's address has since moved stays NULL, and
-- the rotation keeps matching those by address as before.

ALTER TABLE webhooks ADD COLUMN IF NOT EXISTS created_by_user UUID
    REFERENCES users(id) ON DELETE SET NULL;

UPDATE webhooks w SET created_by_user = u.id
  FROM users u
 WHERE w.created_by_user IS NULL
   AND w.created_by <> ''
   AND u.tenant_id = w.tenant_id
   AND lower(u.email) = lower(w.created_by);
