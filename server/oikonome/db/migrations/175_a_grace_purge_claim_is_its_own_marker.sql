-- The nightly grace purge releases a household's outside links (bank
-- connections, the billing add-on's record) before it erases the rows,
-- without holding the tenants row across those calls. It marks the
-- household while it does so in a column of its own rather than by
-- rewriting status / status_before_delete: those two say whether the
-- deletion was asked for, and a payment or gift that lands mid-purge must
-- still be able to lift a lockout that nobody asked for. The marker is
-- also what the console's restore door refuses on, since a household
-- whose links are being released cannot be handed back as it was.
-- NULL = no purge in flight. Re-runnable.

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS purge_claimed_at TIMESTAMPTZ;
