-- A pending charge a person retired by hand ("Remove stuck pending") is
-- removed=1 like any soft retirement; the stamp says it was the person's,
-- so the hourly sync re-reporting the row (its upsert un-removes what the
-- feed still shows) leaves it retired.
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ;
