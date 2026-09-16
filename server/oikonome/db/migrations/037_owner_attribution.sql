-- household ownership attribution ("yours, mine, ours"). A
-- REPORTING / filter dimension, NOT a budget dimension — the verdict,
-- money-map, and spend-exclusion math never read these columns; only the
-- viewing surfaces (transactions list, net worth, reports) filter by owner.
-- accounts.owner is the account's default owner label; a transaction inherits
-- it, and owner_override is the per-transaction exception. Effective owner =
-- COALESCE(t.owner_override, a.owner). NULL = unattributed (shows in the
-- household/all view). This is distinct from owner|viewer, which is
-- access control (who may see/write), not attribution (whose money is this).
ALTER TABLE accounts      ADD COLUMN IF NOT EXISTS owner TEXT;
ALTER TABLE transactions  ADD COLUMN IF NOT EXISTS owner_override TEXT;
