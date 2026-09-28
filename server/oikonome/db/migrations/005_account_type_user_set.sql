-- user-pinned account classification: /accounts/classify sets this flag and
-- sync (base.upsert_accounts) must then never overwrite type/subtype —
-- without the flag the hourly worker reverts the user's fix within the hour,
-- and the SimpleFIN credit balance sign flip keys off the name heuristic
-- instead of the user's persisted type.
ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS type_user_set BOOLEAN NOT NULL DEFAULT FALSE;
