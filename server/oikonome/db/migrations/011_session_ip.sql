-- richer session info — record the signing-in IP so the
-- sessions list can show where each session came from. (Mirrored in
-- schema.sql's idempotent ALTER block for fresh installs.)
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS ip TEXT;
